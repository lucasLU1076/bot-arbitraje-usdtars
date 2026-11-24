import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from candle_builder import build_candles


class TestCandleBuilder(unittest.TestCase):
    def test_aggregates_trades_into_one_minute_buckets(self):
        trades = [
            {"created_at": "2026-01-01T10:00:05+00:00", "price": "100.0", "amount": "1.0"},
            {"created_at": "2026-01-01T10:00:30+00:00", "price": "102.0", "amount": "2.0"},
            {"created_at": "2026-01-01T10:00:59+00:00", "price": "99.0", "amount": "1.0"},
            {"created_at": "2026-01-01T10:01:01+00:00", "price": "101.0", "amount": "1.0"},
        ]
        candles = build_candles(trades, interval_s=60)
        self.assertEqual(len(candles), 2)

        first = candles[0]
        self.assertEqual(first["open"], 100.0)
        self.assertEqual(first["high"], 102.0)
        self.assertEqual(first["low"], 99.0)
        self.assertEqual(first["close"], 99.0)
        self.assertEqual(first["volume"], 4.0)
        self.assertEqual(first["trade_count"], 3)

        second = candles[1]
        self.assertEqual(second["open"], 101.0)
        self.assertEqual(second["trade_count"], 1)


if __name__ == "__main__":
    unittest.main()
