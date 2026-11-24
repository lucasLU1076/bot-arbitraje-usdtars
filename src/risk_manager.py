"""
Gestor de riesgo: decide si es seguro abrir una nueva operacion, calcula el
tamano de posicion y valida que el take-profit configurado deje ganancia neta
real despues de comisiones. Es el unico lugar donde se aplican los limites
duros de capital; strategy.py no deberia tomar estas decisiones por su cuenta.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from data_store import DataStore


@dataclass
class RiskLimits:
    position_size_ars: float
    max_concurrent_positions: int
    max_daily_loss_pct: float
    max_daily_trades: int
    cooldown_minutes_after_trade: int
    maker_fee_pct: float
    taker_fee_pct: float
    min_net_profit_after_fees_pct: float


class RiskManager:
    def __init__(self, limits: RiskLimits, store: DataStore):
        self.limits = limits
        self.store = store

    def round_trip_fee_pct(self, buy_is_maker: bool = True, sell_is_maker: bool = True) -> float:
        buy_fee = self.limits.maker_fee_pct if buy_is_maker else self.limits.taker_fee_pct
        sell_fee = self.limits.maker_fee_pct if sell_is_maker else self.limits.taker_fee_pct
        return buy_fee + sell_fee

    def validate_take_profit(self, take_profit_pct: float) -> tuple[bool, str]:
        """El TP bruto configurado tiene que superar comisiones ida+vuelta + margen minimo."""
        round_trip = self.round_trip_fee_pct()
        required = round_trip + self.limits.min_net_profit_after_fees_pct
        if take_profit_pct < required:
            return False, (
                f"take_profit_pct ({take_profit_pct:.2f}%) no cubre comisiones ida+vuelta "
                f"({round_trip:.2f}%) + margen minimo ({self.limits.min_net_profit_after_fees_pct:.2f}%). "
                f"Minimo necesario: {required:.2f}%."
            )
        return True, "ok"

    def position_size_major(self, price_ars: float) -> float:
        """Convierte el tamano de posicion fijo en ARS a cantidad de 'major' (USDT) al precio dado."""
        if price_ars <= 0:
            raise ValueError("price_ars debe ser > 0")
        return round(self.limits.position_size_ars / price_ars, 2)

    def open_positions_count(self) -> int:
        open_statuses = ("pending_buy", "open")
        return sum(
            1 for row in self.store.fetch_trade_log() if row["status"] in open_statuses
        )

    def seconds_since_last_trade_close(self) -> float | None:
        closed = [r for r in self.store.fetch_trade_log(status="closed") if r["closed_at"]]
        if not closed:
            return None
        last_close = max(datetime.fromisoformat(r["closed_at"]) for r in closed)
        if last_close.tzinfo is None:
            last_close = last_close.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - last_close).total_seconds()

    def can_open_new_trade(self, today_iso: str) -> tuple[bool, str]:
        if self.open_positions_count() >= self.limits.max_concurrent_positions:
            return False, "Ya hay una posicion abierta/pendiente (max_concurrent_positions alcanzado)."

        daily_trades = self.store.daily_trade_count(today_iso)
        if daily_trades >= self.limits.max_daily_trades:
            return False, f"Limite diario de operaciones alcanzado ({daily_trades}/{self.limits.max_daily_trades})."

        daily_pnl = self.store.daily_realized_pnl(today_iso)
        max_loss_ars = -(self.limits.max_daily_loss_pct / 100) * self.limits.position_size_ars * self.limits.max_daily_trades
        if daily_pnl <= max_loss_ars:
            return False, (
                f"Kill switch diario activado: PnL realizado hoy {daily_pnl:.2f} ARS <= "
                f"limite {max_loss_ars:.2f} ARS. El bot no opera mas hasta el proximo dia / reinicio manual."
            )

        seconds_since_close = self.seconds_since_last_trade_close()
        cooldown_s = self.limits.cooldown_minutes_after_trade * 60
        if seconds_since_close is not None and seconds_since_close < cooldown_s:
            remaining = cooldown_s - seconds_since_close
            return False, f"En cooldown, faltan {remaining:.0f}s para poder operar de nuevo."

        return True, "ok"
