"""Agrega trades individuales en velas OHLC de un intervalo fijo (en segundos)."""

from __future__ import annotations

from datetime import datetime, timezone


def _parse_iso(ts: str) -> datetime:
    # Bitso devuelve timestamps ISO 8601, normalmente con offset o 'Z'.
    ts = ts.replace("Z", "+00:00")
    dt = datetime.fromisoformat(ts)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _bucket_start(dt: datetime, interval_s: int) -> datetime:
    epoch = dt.timestamp()
    bucket_epoch = (int(epoch) // interval_s) * interval_s
    return datetime.fromtimestamp(bucket_epoch, tz=timezone.utc)


def build_candles(trades: list[dict], interval_s: int) -> list[dict]:
    """
    trades: lista de dicts con al menos 'created_at' (ISO) y 'price' (float),
    ordenados ascendentemente por tiempo.
    """
    buckets: dict[datetime, dict] = {}
    for t in trades:
        dt = _parse_iso(t["created_at"])
        bucket = _bucket_start(dt, interval_s)
        price = float(t["price"])
        amount = float(t.get("amount", 0.0))

        c = buckets.get(bucket)
        if c is None:
            buckets[bucket] = {
                "bucket_start": bucket.isoformat(),
                "open": price,
                "high": price,
                "low": price,
                "close": price,
                "volume": amount,
                "trade_count": 1,
            }
        else:
            c["high"] = max(c["high"], price)
            c["low"] = min(c["low"], price)
            c["close"] = price
            c["volume"] += amount
            c["trade_count"] += 1

    return [buckets[k] for k in sorted(buckets.keys())]
