"""
Logica pura de decision (sin I/O): deteccion de senal de "mechazo" y reglas de
salida. Separada de live_engine.py (que hace I/O real contra Bitso) y de
backtester.py (que reusa estas mismas funciones sobre datos historicos), para
garantizar que el bot en vivo y el backtest corren EXACTAMENTE la misma logica.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

ARS_TZ = ZoneInfo("America/Argentina/Buenos_Aires")


@dataclass
class StrategyConfig:
    reference_window_minutes: int
    wick_drop_threshold_pct: float
    entry_offset_pct: float
    take_profit_pct: float
    buy_order_timeout_seconds: int
    max_holding_minutes: int
    hard_stop_loss_pct: float
    allowed_hours_ars_tz: list[int]
    min_hourly_sample_size: int


@dataclass
class BuySignal:
    reference_price: float
    low_price: float
    drop_pct: float
    entry_price: float


def hour_filter_passes(cfg: StrategyConfig, now: datetime) -> bool:
    """Lista vacia = sin filtro horario (modo exploracion). Si tiene horas, exige coincidir."""
    if not cfg.allowed_hours_ars_tz:
        return True
    local_hour = now.astimezone(ARS_TZ).hour
    return local_hour in cfg.allowed_hours_ars_tz


def vwap(candles: list[dict]) -> float | None:
    total_vol = sum(c["volume"] for c in candles)
    if total_vol <= 0:
        return None
    return sum(c["close"] * c["volume"] for c in candles) / total_vol


def detect_buy_signal(cfg: StrategyConfig, recent_candles: list[dict], current_low: float,
                       now: datetime) -> BuySignal | None:
    """
    recent_candles: velas de 1 minuto de los ultimos 'reference_window_minutes',
    SIN incluir la vela/periodo actual de donde viene 'current_low'.
    """
    if not recent_candles:
        return None
    reference_price = vwap(recent_candles)
    if reference_price is None:
        return None
    return detect_buy_signal_from_reference(cfg, reference_price, current_low, now)


def detect_buy_signal_from_reference(cfg: StrategyConfig, reference_price: float, current_price: float,
                                      now: datetime) -> BuySignal | None:
    """Misma logica que detect_buy_signal pero con el precio de referencia ya calculado
    (usado por live_engine.py, que mantiene su propio VWAP deslizante sobre trades)."""
    if not hour_filter_passes(cfg, now):
        return None
    if reference_price <= 0:
        return None

    drop_pct = (reference_price - current_price) / reference_price * 100
    if drop_pct < cfg.wick_drop_threshold_pct:
        return None

    entry_price = current_price * (1 + cfg.entry_offset_pct / 100)
    return BuySignal(
        reference_price=reference_price,
        low_price=current_price,
        drop_pct=drop_pct,
        entry_price=entry_price,
    )


def take_profit_price(cfg: StrategyConfig, entry_price: float) -> float:
    return entry_price * (1 + cfg.take_profit_pct / 100)


def hard_stop_price(cfg: StrategyConfig, entry_price: float) -> float:
    return entry_price * (1 - cfg.hard_stop_loss_pct / 100)


def is_buy_order_stale(opened_at: datetime, now: datetime, cfg: StrategyConfig) -> bool:
    return (now - opened_at).total_seconds() > cfg.buy_order_timeout_seconds


def holding_minutes(opened_at: datetime, now: datetime) -> float:
    return (now - opened_at).total_seconds() / 60


def is_holding_timeout(opened_at: datetime, now: datetime, cfg: StrategyConfig) -> bool:
    """
    Señal informativa, NO dispara una venta automatica a mercado: la estrategia
    solo vende en ganancia (take-profit) o por el stop duro de seguridad. Un
    timeout de holding simplemente se loggea para revision manual.
    """
    return holding_minutes(opened_at, now) > cfg.max_holding_minutes


def should_hard_stop(cfg: StrategyConfig, entry_price: float, current_price: float) -> bool:
    return current_price <= hard_stop_price(cfg, entry_price)
