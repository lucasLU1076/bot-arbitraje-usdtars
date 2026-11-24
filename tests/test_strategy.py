import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from strategy import (
    StrategyConfig, detect_buy_signal_from_reference, hard_stop_price,
    hour_filter_passes, is_buy_order_stale, is_holding_timeout, should_hard_stop,
    take_profit_price,
)


def make_cfg(**overrides) -> StrategyConfig:
    base = dict(
        reference_window_minutes=30,
        wick_drop_threshold_pct=2.0,
        entry_offset_pct=0.1,
        take_profit_pct=2.5,
        buy_order_timeout_seconds=90,
        max_holding_minutes=240,
        hard_stop_loss_pct=4.0,
        allowed_hours_ars_tz=[],
        min_hourly_sample_size=20,
    )
    base.update(overrides)
    return StrategyConfig(**base)


class TestStrategy(unittest.TestCase):
    def test_detect_buy_signal_triggers_on_big_drop(self):
        cfg = make_cfg()
        now = datetime.now(timezone.utc)
        signal = detect_buy_signal_from_reference(cfg, reference_price=1000.0, current_price=975.0, now=now)
        self.assertIsNotNone(signal)
        self.assertAlmostEqual(signal.drop_pct, 2.5, places=4)
        self.assertAlmostEqual(signal.entry_price, 975.0 * 1.001, places=6)

    def test_detect_buy_signal_ignores_small_drop(self):
        cfg = make_cfg()
        now = datetime.now(timezone.utc)
        signal = detect_buy_signal_from_reference(cfg, reference_price=1000.0, current_price=995.0, now=now)
        self.assertIsNone(signal)

    def test_hour_filter_empty_means_always_allowed(self):
        cfg = make_cfg(allowed_hours_ars_tz=[])
        now = datetime.now(timezone.utc)
        self.assertTrue(hour_filter_passes(cfg, now))

    def test_hour_filter_restricts_hours(self):
        cfg = make_cfg(allowed_hours_ars_tz=[3])
        now = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)  # 09:00 ARS (UTC-3), fuera de [3]
        self.assertFalse(hour_filter_passes(cfg, now))

    def test_take_profit_and_hard_stop_prices(self):
        cfg = make_cfg(take_profit_pct=2.5, hard_stop_loss_pct=4.0)
        self.assertAlmostEqual(take_profit_price(cfg, 100.0), 102.5)
        self.assertAlmostEqual(hard_stop_price(cfg, 100.0), 96.0)

    def test_should_hard_stop(self):
        cfg = make_cfg(hard_stop_loss_pct=4.0)
        self.assertTrue(should_hard_stop(cfg, entry_price=100.0, current_price=95.0))
        self.assertFalse(should_hard_stop(cfg, entry_price=100.0, current_price=97.0))

    def test_buy_order_timeout(self):
        cfg = make_cfg(buy_order_timeout_seconds=90)
        opened = datetime.now(timezone.utc)
        self.assertFalse(is_buy_order_stale(opened, opened + timedelta(seconds=30), cfg))
        self.assertTrue(is_buy_order_stale(opened, opened + timedelta(seconds=120), cfg))

    def test_holding_timeout(self):
        cfg = make_cfg(max_holding_minutes=240)
        opened = datetime.now(timezone.utc)
        self.assertFalse(is_holding_timeout(opened, opened + timedelta(minutes=100), cfg))
        self.assertTrue(is_holding_timeout(opened, opened + timedelta(minutes=300), cfg))


if __name__ == "__main__":
    unittest.main()
