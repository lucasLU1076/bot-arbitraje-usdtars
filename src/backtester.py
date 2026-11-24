"""
Backtest a nivel de trade-a-trade (no de velas), para evitar el sesgo de
"look-ahead" de usar el low de una vela ya cerrada como precio de entrada.

Logica de simulacion:
- Se recorren los trades publicos historicos en orden cronologico.
- Para cada trade se mantiene un precio de referencia = VWAP de los trades de
  los ultimos 'reference_window_minutes' ANTERIORES a ese trade (sliding window
  de dos punteros, O(n) total).
- Si no hay posicion abierta y el precio del trade cae >= wick_drop_threshold_pct
  por debajo de la referencia (y pasa el filtro horario y los limites de riesgo),
  se simula colocar una orden limite de compra en entry_price = precio*(1+offset%).
- Esa orden se considera llena si, dentro de 'buy_order_timeout_seconds', aparece
  un trade subsiguiente a precio <= entry_price (es decir, alguien vendio a ese
  nivel o mas bajo, cruzando nuestra orden). Si no, se cancela (sin costo).
- Una vez abierta la posicion, se vende en take_profit_price (si un trade
  subsiguiente llega a ese nivel o mas arriba) o en hard_stop_price (si el precio
  cae tanto, simulando una venta a mercado/taker de emergencia). No hay cierre
  forzado por timeout de holding (ver strategy.py): se deja correr hasta TP o stop.
- Las comisiones se aplican segun el lado: se asume maker+maker para el camino
  normal (ambas ordenes son limite y quedan paradas en el libro) y taker en la
  pata de venta cuando se activa el hard stop (venta de emergencia).

Limitaciones honestas (léelas antes de confiar en los resultados):
- No modela profundidad real del libro ni el hecho de que OTRAS ordenes por
  delante de la nuestra en el mismo precio se lleven el fill primero.
- No modela latencia de red ni rate limits.
- Los datos historicos de Bitso vienen de ir grabando /v3/trades en vivo
  (no hay API de velas historicas), asi que el backtest solo cubre el periodo
  que vos mismo dejaste corriendo scripts/collect_data.py.
- Que un patron se haya repetido en el pasado no garantiza que se repita en el
  futuro. Este resultado es evidencia para decidir, no una promesa de retorno.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from strategy import ARS_TZ, StrategyConfig, hour_filter_passes, take_profit_price, hard_stop_price


@dataclass
class BacktestConfig:
    strategy: StrategyConfig
    position_size_ars: float
    maker_fee_pct: float
    taker_fee_pct: float
    max_daily_trades: int
    max_daily_loss_pct: float
    cooldown_minutes_after_trade: int


@dataclass
class ClosedTrade:
    opened_at: datetime
    filled_at: datetime
    closed_at: datetime
    entry_price: float
    exit_price: float
    amount_major: float
    pnl_ars: float
    pnl_pct: float
    exit_reason: str  # 'take_profit' | 'hard_stop'
    hour_ars: int


@dataclass
class BacktestReport:
    closed_trades: list[ClosedTrade] = field(default_factory=list)
    signals_detected: int = 0
    signals_filled: int = 0
    signals_timed_out_unfilled: int = 0

    @property
    def n_closed(self) -> int:
        return len(self.closed_trades)

    @property
    def wins(self) -> int:
        return sum(1 for t in self.closed_trades if t.pnl_ars > 0)

    @property
    def losses(self) -> int:
        return sum(1 for t in self.closed_trades if t.pnl_ars <= 0)

    @property
    def win_rate(self) -> float | None:
        return self.wins / self.n_closed if self.n_closed else None

    @property
    def total_pnl_ars(self) -> float:
        return sum(t.pnl_ars for t in self.closed_trades)

    @property
    def avg_holding_minutes(self) -> float | None:
        if not self.closed_trades:
            return None
        return sum((t.closed_at - t.filled_at).total_seconds() / 60 for t in self.closed_trades) / len(self.closed_trades)

    @property
    def max_drawdown_ars(self) -> float:
        cum = 0.0
        peak = 0.0
        max_dd = 0.0
        for t in self.closed_trades:
            cum += t.pnl_ars
            peak = max(peak, cum)
            max_dd = min(max_dd, cum - peak)
        return max_dd

    def summary_text(self, capital_ars: float) -> str:
        wr = f"{self.win_rate * 100:.1f}%" if self.win_rate is not None else "n/a"
        avg_hold = f"{self.avg_holding_minutes:.1f} min" if self.avg_holding_minutes is not None else "n/a"
        roi_pct = (self.total_pnl_ars / capital_ars * 100) if capital_ars else 0.0
        return (
            f"Señales detectadas: {self.signals_detected} | llenadas: {self.signals_filled} | "
            f"sin llenar (timeout): {self.signals_timed_out_unfilled}\n"
            f"Operaciones cerradas: {self.n_closed} (ganadoras: {self.wins}, perdedoras: {self.losses}, win rate: {wr})\n"
            f"PnL total: {self.total_pnl_ars:.2f} ARS ({roi_pct:.2f}% sobre {capital_ars:.0f} ARS de capital por operacion)\n"
            f"Drawdown maximo (racha de perdidas acumuladas): {self.max_drawdown_ars:.2f} ARS\n"
            f"Tiempo promedio de holding: {avg_hold}\n"
        )


def _parse_iso(ts: str) -> datetime:
    ts = ts.replace("Z", "+00:00")
    dt = datetime.fromisoformat(ts)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def run_backtest(trades: list[dict], cfg: BacktestConfig) -> BacktestReport:
    """trades: lista ordenada ascendentemente por tiempo, con 'created_at' (iso) y 'price' (float)."""
    scfg = cfg.strategy
    report = BacktestReport()

    parsed = [(_parse_iso(t["created_at"]), float(t["price"]), float(t.get("amount", 0.0))) for t in trades]

    window = timedelta(minutes=scfg.reference_window_minutes)
    lo = 0
    sum_pv = 0.0
    sum_v = 0.0

    state = "idle"
    pending_entry_price = None
    pending_since = None
    position = None  # dict con entry_price, amount_major, target, stop, filled_at

    last_close_time: datetime | None = None
    daily_trade_count: dict[str, int] = {}
    daily_pnl: dict[str, float] = {}

    max_loss_ars = -(cfg.max_daily_loss_pct / 100) * cfg.position_size_ars * cfg.max_daily_trades

    for i, (now, price, amount) in enumerate(parsed):
        # Avanzar la ventana deslizante para excluir trades fuera de rango.
        while lo < i and parsed[lo][0] < now - window:
            _, p_lo, v_lo = parsed[lo]
            sum_pv -= p_lo * v_lo
            sum_v -= v_lo
            lo += 1

        reference_price = (sum_pv / sum_v) if sum_v > 0 else None
        date_key = now.date().isoformat()

        if state == "pending_buy":
            if price <= pending_entry_price:
                amount_major = cfg.position_size_ars / pending_entry_price
                position = {
                    "entry_price": pending_entry_price,
                    "amount_major": amount_major,
                    "target": take_profit_price(scfg, pending_entry_price),
                    "stop": hard_stop_price(scfg, pending_entry_price),
                    "filled_at": now,
                    "opened_at": pending_since,
                }
                report.signals_filled += 1
                state = "open"
                pending_entry_price = None
                pending_since = None
            elif now - pending_since > timedelta(seconds=scfg.buy_order_timeout_seconds):
                report.signals_timed_out_unfilled += 1
                state = "idle"
                pending_entry_price = None
                pending_since = None

        elif state == "open":
            if price >= position["target"]:
                exit_price = position["target"]
                exit_reason = "take_profit"
            elif price <= position["stop"]:
                exit_price = position["stop"]
                exit_reason = "hard_stop"
            else:
                exit_price = None
                exit_reason = None

            if exit_price is not None:
                amount_major = position["amount_major"]
                buy_cost = amount_major * position["entry_price"] * (1 + cfg.maker_fee_pct / 100)
                sell_fee_pct = cfg.maker_fee_pct if exit_reason == "take_profit" else cfg.taker_fee_pct
                sell_proceeds = amount_major * exit_price * (1 - sell_fee_pct / 100)
                pnl_ars = sell_proceeds - buy_cost
                pnl_pct = pnl_ars / buy_cost * 100

                closed = ClosedTrade(
                    opened_at=position["opened_at"],
                    filled_at=position["filled_at"],
                    closed_at=now,
                    entry_price=position["entry_price"],
                    exit_price=exit_price,
                    amount_major=amount_major,
                    pnl_ars=pnl_ars,
                    pnl_pct=pnl_pct,
                    exit_reason=exit_reason,
                    hour_ars=position["opened_at"].astimezone(ARS_TZ).hour,
                )
                report.closed_trades.append(closed)

                daily_trade_count[date_key] = daily_trade_count.get(date_key, 0) + 1
                daily_pnl[date_key] = daily_pnl.get(date_key, 0.0) + pnl_ars

                last_close_time = now
                state = "idle"
                position = None

        if state == "idle":
            can_trade = True
            if daily_trade_count.get(date_key, 0) >= cfg.max_daily_trades:
                can_trade = False
            if daily_pnl.get(date_key, 0.0) <= max_loss_ars:
                can_trade = False
            if last_close_time is not None and (now - last_close_time) < timedelta(minutes=cfg.cooldown_minutes_after_trade):
                can_trade = False

            if can_trade and reference_price and reference_price > 0 and hour_filter_passes(scfg, now):
                drop_pct = (reference_price - price) / reference_price * 100
                if drop_pct >= scfg.wick_drop_threshold_pct:
                    report.signals_detected += 1
                    pending_entry_price = price * (1 + scfg.entry_offset_pct / 100)
                    pending_since = now
                    state = "pending_buy"

        # Incorporar el trade actual a la ventana para referencias futuras.
        sum_pv += price * amount
        sum_v += amount

    return report
