"""
Analisis de patrones historicos de "mechazos" (caidas bruscas) en el book.

Metodologia:
1. A partir de las velas de 1 minuto guardadas en SQLite, se calcula para cada
   vela un precio de referencia = VWAP de las 'reference_window_minutes' previas
   (sin incluir la vela actual).
2. Si el 'low' de la vela cae >= 'wick_drop_threshold_pct' por debajo de esa
   referencia, se marca como un evento de mechazo.
3. Para cada evento se mide, mirando hacia adelante hasta 'max_holding_minutes':
   - si el precio volvio a tocar (reference_price) o el nivel de take-profit
     configurado, y en cuantos minutos.
   - la reversion maxima alcanzada (% desde el low).
4. Los eventos se agrupan por hora del dia (zona horaria Argentina) y dia de
   semana, para ver en que franjas horarias el patron es mas frecuente y mas
   confiable (mayor tasa de reversion, mayor reversion promedio).

Esto es analisis estadistico sobre datos historicos, no garantiza nada sobre el
futuro. Sirve para calibrar 'allowed_hours_ars_tz' en config.yaml con evidencia
en vez de intuicion, y para decidir si la estrategia tiene edge antes de arriesgar
capital real (ver backtester.py para la validacion real con fees y slippage).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pandas as pd

ARS_TZ = ZoneInfo("America/Argentina/Buenos_Aires")


@dataclass
class WickEvent:
    detected_at: datetime
    reference_price: float
    low_price: float
    drop_pct: float
    reverted_within_minutes: float | None
    max_reversion_pct: float


def _candles_to_df(candles: list[dict]) -> pd.DataFrame:
    df = pd.DataFrame(candles)
    df["bucket_start"] = pd.to_datetime(df["bucket_start"], utc=True)
    df = df.sort_values("bucket_start").reset_index(drop=True)
    return df


def detect_wick_events(candles: list[dict], reference_window_minutes: int,
                        wick_drop_threshold_pct: float, max_holding_minutes: int,
                        take_profit_pct: float) -> list[WickEvent]:
    """Asume velas de 1 minuto, continuas o con huecos razonables."""
    df = _candles_to_df(candles)
    if df.empty:
        return []

    df["vwap_num"] = df["close"] * df["volume"]
    events: list[WickEvent] = []

    for i in range(len(df)):
        window_start = df["bucket_start"].iloc[i] - timedelta(minutes=reference_window_minutes)
        ref_slice = df[(df["bucket_start"] >= window_start) & (df["bucket_start"] < df["bucket_start"].iloc[i])]
        if ref_slice.empty or ref_slice["volume"].sum() == 0:
            continue

        reference_price = ref_slice["vwap_num"].sum() / ref_slice["volume"].sum()
        low = df["low"].iloc[i]
        drop_pct = (reference_price - low) / reference_price * 100

        if drop_pct < wick_drop_threshold_pct:
            continue

        horizon_end = df["bucket_start"].iloc[i] + timedelta(minutes=max_holding_minutes)
        fwd = df[(df["bucket_start"] > df["bucket_start"].iloc[i]) & (df["bucket_start"] <= horizon_end)]

        take_profit_price = low * (1 + take_profit_pct / 100)
        reverted_within_minutes = None
        if not fwd.empty:
            hit = fwd[fwd["high"] >= take_profit_price]
            if not hit.empty:
                first_hit = hit.iloc[0]
                reverted_within_minutes = (first_hit["bucket_start"] - df["bucket_start"].iloc[i]).total_seconds() / 60

        max_future_high = fwd["high"].max() if not fwd.empty else low
        max_reversion_pct = (max_future_high - low) / low * 100

        events.append(WickEvent(
            detected_at=df["bucket_start"].iloc[i].to_pydatetime(),
            reference_price=float(reference_price),
            low_price=float(low),
            drop_pct=float(drop_pct),
            reverted_within_minutes=reverted_within_minutes,
            max_reversion_pct=float(max_reversion_pct),
        ))

    return events


def summarize_by_hour(events: list[WickEvent]) -> pd.DataFrame:
    """Devuelve una tabla por hora del dia (hora Argentina) con frecuencia y tasa de reversion."""
    if not events:
        return pd.DataFrame(columns=[
            "hour_ars", "n_events", "revert_rate", "avg_minutes_to_revert",
            "avg_max_reversion_pct",
        ])

    rows = []
    for e in events:
        local_dt = e.detected_at.astimezone(ARS_TZ)
        rows.append({
            "hour_ars": local_dt.hour,
            "reverted": e.reverted_within_minutes is not None,
            "minutes_to_revert": e.reverted_within_minutes,
            "max_reversion_pct": e.max_reversion_pct,
        })
    df = pd.DataFrame(rows)

    summary = df.groupby("hour_ars").agg(
        n_events=("reverted", "count"),
        revert_rate=("reverted", "mean"),
        avg_minutes_to_revert=("minutes_to_revert", "mean"),
        avg_max_reversion_pct=("max_reversion_pct", "mean"),
    ).reset_index()

    return summary.sort_values("hour_ars").reset_index(drop=True)


def recommend_allowed_hours(summary: pd.DataFrame, min_sample_size: int,
                             min_revert_rate: float = 0.6) -> list[int]:
    """
    Horas candidatas a habilitar en config.yaml: suficiente muestra historica
    Y una tasa de reversion (hacia el take-profit) razonable. Intencionalmente
    conservador: con pocos datos, devuelve lista vacia (= sin filtro horario,
    dejar que el backtester decida) en vez de inventar una ventana horaria.
    """
    if summary.empty:
        return []
    qualifying = summary[
        (summary["n_events"] >= min_sample_size) & (summary["revert_rate"] >= min_revert_rate)
    ]
    return sorted(int(h) for h in qualifying["hour_ars"].tolist())
