"""
Motor en vivo: conecta la logica de strategy.py + risk_manager.py con el
cliente real de Bitso (o con una simulacion identica si dry_run esta activo).

Doble traba de seguridad antes de enviar UNA SOLA orden real:
  1. config.yaml -> mode.dry_run debe ser false.
  2. variable de entorno BITSO_LIVE_CONFIRM debe valer exactamente "YES".
Si falta cualquiera de las dos, TODO se simula en memoria/log, sin pegarle
nunca a los endpoints privados de Bitso (ni siquiera a los de consulta de
balance, para no depender de permisos que no hagan falta en modo simulado).

El estado de la operacion en curso (si hay una) se persiste en SQLite
(trade_log), asi que si el proceso se reinicia no se pierde ni se duplica
una posicion abierta.
"""

from __future__ import annotations

import time
import uuid
from datetime import datetime, timezone

from bitso_client import BitsoClient
from data_store import DataStore
from risk_manager import RiskManager
import strategy as strat


class LiveEngine:
    def __init__(self, book: str, client: BitsoClient, store: DataStore,
                 risk_manager: RiskManager, strategy_cfg: strat.StrategyConfig,
                 is_live: bool, poll_seconds: float = 5.0):
        self.book = book
        self.client = client
        self.store = store
        self.risk = risk_manager
        self.scfg = strategy_cfg
        self.is_live = is_live
        self.poll_seconds = poll_seconds

        self._last_tid: int | None = store.last_trade_tid(book)
        self._window_trades: list[tuple[datetime, float, float]] = []

    # ------------------------------------------------------------- utilidad

    def _log(self, msg: str) -> None:
        mode = "LIVE" if self.is_live else "DRY-RUN"
        print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] [{mode}] {msg}")

    def _parse_iso(self, ts: str) -> datetime:
        ts = ts.replace("Z", "+00:00")
        dt = datetime.fromisoformat(ts)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt

    def _poll_new_trades(self) -> float | None:
        """Trae trades nuevos del publico, los guarda, actualiza la ventana de referencia.
        Devuelve el ultimo precio observado (o None si no hubo trades nuevos ni previos)."""
        try:
            if self._last_tid is None:
                batch = self.client.trades(self.book, limit=100, sort="desc")
                batch = list(reversed(batch))
            else:
                batch = self.client.trades(self.book, limit=100, marker=str(self._last_tid), sort="asc")
        except Exception as exc:  # noqa: BLE001
            self._log(f"Error consultando /trades, se reintenta en el proximo ciclo: {exc}")
            return self._window_trades[-1][1] if self._window_trades else None

        if batch:
            self.store.insert_trades(self.book, batch)
            self._last_tid = max(int(t["tid"]) for t in batch)
            for t in batch:
                self._window_trades.append((self._parse_iso(t["created_at"]), float(t["price"]), float(t["amount"])))

        cutoff = datetime.now(timezone.utc)
        min_keep = cutoff.timestamp() - self.scfg.reference_window_minutes * 60
        self._window_trades = [w for w in self._window_trades if w[0].timestamp() >= min_keep]

        return self._window_trades[-1][1] if self._window_trades else None

    def _reference_price(self, exclude_last_n_seconds: float = 0.0) -> float | None:
        now = datetime.now(timezone.utc)
        rel = [
            w for w in self._window_trades
            if exclude_last_n_seconds == 0 or (now - w[0]).total_seconds() > exclude_last_n_seconds
        ]
        sum_v = sum(w[2] for w in rel)
        if sum_v <= 0:
            return None
        return sum(w[1] * w[2] for w in rel) / sum_v

    # --------------------------------------------------------- ordenes reales/sim

    def _place_buy(self, entry_price: float, amount_major: float) -> str:
        if self.is_live:
            result = self.client.place_order(
                self.book, side="buy", order_type="limit",
                major=f"{amount_major:.8f}", price=f"{entry_price:.2f}",
                time_in_force="goodtillcancelled",
            )
            return result.oid
        return f"SIM-BUY-{uuid.uuid4().hex[:10]}"

    def _place_sell(self, target_price: float, amount_major: float) -> str:
        if self.is_live:
            result = self.client.place_order(
                self.book, side="sell", order_type="limit",
                major=f"{amount_major:.8f}", price=f"{target_price:.2f}",
                time_in_force="goodtillcancelled",
            )
            return result.oid
        return f"SIM-SELL-{uuid.uuid4().hex[:10]}"

    def _place_emergency_market_sell(self, amount_major: float) -> str:
        if self.is_live:
            result = self.client.place_order(
                self.book, side="sell", order_type="market", major=f"{amount_major:.8f}",
            )
            return result.oid
        return f"SIM-STOP-{uuid.uuid4().hex[:10]}"

    def _cancel_order(self, oid: str) -> None:
        if self.is_live and not oid.startswith("SIM-"):
            try:
                self.client.cancel_order(oid)
            except Exception as exc:  # noqa: BLE001
                self._log(f"No se pudo cancelar orden {oid} (puede que ya se haya llenado/cancelado): {exc}")

    def _order_status(self, oid: str) -> dict | None:
        """Devuelve dict con 'status' y 'unfilled_amount' (como float), o None si no se encuentra."""
        if oid.startswith("SIM-"):
            return None  # el modo simulado decide el fill comparando precios, no consultando ordenes
        try:
            rows = self.client.lookup_orders([oid])
        except Exception as exc:  # noqa: BLE001
            self._log(f"Error consultando estado de orden {oid}: {exc}")
            return None
        if not rows:
            return {"status": "completed", "unfilled_amount": 0.0}
        row = rows[0]
        return {"status": row["status"], "unfilled_amount": float(row.get("unfilled_amount", 0.0))}

    # -------------------------------------------------------------- ciclo

    def _manage_pending_buy(self, trade: dict, last_price: float | None) -> None:
        opened_at = self._parse_iso(trade["opened_at"]) if isinstance(trade["opened_at"], str) else trade["opened_at"]
        now = datetime.now(timezone.utc)

        filled = False
        if trade["buy_oid"].startswith("SIM-"):
            filled = last_price is not None and last_price <= trade["entry_price"]
        else:
            status = self._order_status(trade["buy_oid"])
            filled = status is not None and status["status"] == "completed"

        if filled:
            amount_major = trade["amount_major"]
            target = strat.take_profit_price(self.scfg, trade["entry_price"])
            sell_oid = self._place_sell(target, amount_major)
            self.store.update_trade(trade["id"], status="open", sell_oid=sell_oid)
            self._log(
                f"COMPRA LLENADA a ~{trade['entry_price']:.2f}. Vendiendo en {target:.2f} "
                f"(+{self.scfg.take_profit_pct:.2f}%). oid venta={sell_oid}"
            )
            return

        if strat.is_buy_order_stale(opened_at, now, self.scfg):
            self._cancel_order(trade["buy_oid"])
            self.store.update_trade(trade["id"], status="cancelled", closed_at=now.isoformat(),
                                     exit_reason="timeout")
            self._log(f"Compra pendiente #{trade['id']} cancelada por timeout sin llenarse.")

    def _manage_open_position(self, trade: dict, last_price: float | None) -> None:
        if last_price is None:
            return
        entry_price = trade["entry_price"]
        amount_major = trade["amount_major"]
        target = strat.take_profit_price(self.scfg, entry_price)
        stop = strat.hard_stop_price(self.scfg, entry_price)
        now = datetime.now(timezone.utc)

        hard_stop_hit = strat.should_hard_stop(self.scfg, entry_price, last_price)

        sell_filled = False
        if trade["sell_oid"].startswith("SIM-"):
            sell_filled = last_price >= target
        else:
            status = self._order_status(trade["sell_oid"])
            sell_filled = status is not None and status["status"] == "completed"

        if sell_filled and not hard_stop_hit:
            self._close_trade(trade, exit_price=target, exit_reason="take_profit", now=now)
            return

        if hard_stop_hit:
            self._cancel_order(trade["sell_oid"])
            self._place_emergency_market_sell(amount_major)
            self._log(
                f"STOP DURO activado (precio {last_price:.2f} <= stop {stop:.2f}). "
                f"Vendiendo a mercado para cortar la perdida (red de seguridad, no es el plan principal)."
            )
            self._close_trade(trade, exit_price=last_price, exit_reason="hard_stop", now=now,
                               sell_fee_pct=self.risk.limits.taker_fee_pct)
            return

        opened_at = self._parse_iso(trade["opened_at"]) if isinstance(trade["opened_at"], str) else trade["opened_at"]
        if strat.is_holding_timeout(opened_at, now, self.scfg):
            self._log(
                f"Aviso: posicion #{trade['id']} lleva mas de {self.scfg.max_holding_minutes} min abierta "
                f"sin llegar al take-profit. Sigue esperando TP o stop duro (no se fuerza venta en perdida)."
            )

    def _close_trade(self, trade: dict, exit_price: float, exit_reason: str, now: datetime,
                      sell_fee_pct: float | None = None) -> None:
        amount_major = trade["amount_major"]
        buy_fee_pct = self.risk.limits.maker_fee_pct
        sell_fee_pct = sell_fee_pct if sell_fee_pct is not None else self.risk.limits.maker_fee_pct

        buy_cost = amount_major * trade["entry_price"] * (1 + buy_fee_pct / 100)
        sell_proceeds = amount_major * exit_price * (1 - sell_fee_pct / 100)
        pnl_ars = sell_proceeds - buy_cost
        pnl_pct = pnl_ars / buy_cost * 100

        self.store.update_trade(
            trade["id"], status="closed", closed_at=now.isoformat(),
            exit_price=exit_price, pnl_ars=pnl_ars, pnl_pct=pnl_pct, exit_reason=exit_reason,
        )
        self._log(
            f"Posicion #{trade['id']} CERRADA ({exit_reason}): entrada {trade['entry_price']:.2f} -> "
            f"salida {exit_price:.2f} | PnL {pnl_ars:.2f} ARS ({pnl_pct:+.2f}%)"
        )

    def _try_open_new_trade(self, last_price: float) -> None:
        today_iso = datetime.now(timezone.utc).date().isoformat()
        ok, reason = self.risk.can_open_new_trade(today_iso)
        if not ok:
            return

        now = datetime.now(timezone.utc)
        reference_price = self._reference_price()
        if reference_price is None:
            return

        signal = strat.detect_buy_signal_from_reference(
            self.scfg, reference_price=reference_price, current_price=last_price, now=now,
        )
        if signal is None:
            return

        amount_major = self.risk.position_size_major(signal.entry_price)
        buy_oid = self._place_buy(signal.entry_price, amount_major)
        trade_id = self.store.log_trade_open(
            book=self.book,
            mode="live" if self.is_live else "dry_run",
            buy_oid=buy_oid,
            entry_price=signal.entry_price,
            amount_major=amount_major,
            opened_at=now.isoformat(),
        )
        self._log(
            f"MECHAZO detectado: ref={signal.reference_price:.2f} low={signal.low_price:.2f} "
            f"(-{signal.drop_pct:.2f}%). Compra limite #{trade_id} en {signal.entry_price:.2f} "
            f"por {amount_major:.4f} {self.book.split('_')[0].upper()}. oid={buy_oid}"
        )

    def tick(self) -> None:
        last_price = self._poll_new_trades()

        active = [t for t in self.store.fetch_trade_log() if t["status"] in ("pending_buy", "open")]
        if active:
            trade = active[0]
            if trade["status"] == "pending_buy":
                self._manage_pending_buy(trade, last_price)
            else:
                self._manage_open_position(trade, last_price)
        elif last_price is not None:
            self._try_open_new_trade(last_price)

    def run_forever(self) -> None:
        self._log(
            f"Arrancando motor sobre '{self.book}'. Modo: {'REAL' if self.is_live else 'SIMULADO (dry-run)'}."
        )
        while True:
            self.tick()
            time.sleep(self.poll_seconds)
