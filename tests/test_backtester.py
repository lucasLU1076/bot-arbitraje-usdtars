import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from backtester import BacktestConfig, run_backtest
from strategy import StrategyConfig


def make_strategy_cfg(**overrides) -> StrategyConfig:
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


def iso(dt: datetime) -> str:
    return dt.isoformat()


def build_synthetic_trades() -> list[dict]:
    t0 = datetime(2026, 1, 1, 0, 0, tzinfo=timezone.utc)
    trades = []

    # 35 minutos de precio estable en 1000 para establecer una referencia solida.
    t = t0
    for _ in range(35 * 6):
        trades.append({"created_at": iso(t), "price": "1000.0", "amount": "1.0"})
        t += timedelta(seconds=10)

    # Mechazo: caida brusca a 970 (-3% vs referencia ~1000).
    wick_time = t
    trades.append({"created_at": iso(wick_time), "price": "970.0", "amount": "1.0"})

    # Trade que cruza nuestra orden limite de compra (entry_price = 970 * 1.001 = 970.97).
    fill_time = wick_time + timedelta(seconds=5)
    trades.append({"created_at": iso(fill_time), "price": "970.5", "amount": "1.0"})

    # Reversion: el precio sube hasta el take-profit (970.97 * 1.025 ~= 995.22) 10 min despues.
    tp_time = fill_time + timedelta(minutes=10)
    trades.append({"created_at": iso(tp_time), "price": "1000.0", "amount": "1.0"})

    # 60 minutos mas de precio estable despues del cierre, sin nuevas señales relevantes.
    t = tp_time + timedelta(seconds=10)
    end = tp_time + timedelta(minutes=60)
    while t < end:
        trades.append({"created_at": iso(t), "price": "999.0", "amount": "1.0"})
        t += timedelta(seconds=10)

    return trades


class TestBacktester(unittest.TestCase):
    def test_detects_wick_fills_and_closes_in_profit(self):
        trades = build_synthetic_trades()
        bt_cfg = BacktestConfig(
            strategy=make_strategy_cfg(),
            position_size_ars=75000.0,
            maker_fee_pct=0.45,
            taker_fee_pct=0.60,
            max_daily_trades=6,
            max_daily_loss_pct=1.5,
            cooldown_minutes_after_trade=15,
        )

        report = run_backtest(trades, bt_cfg)

        self.assertGreaterEqual(report.signals_detected, 1)
        self.assertEqual(report.signals_filled, 1)
        self.assertEqual(report.n_closed, 1)

        trade = report.closed_trades[0]
        self.assertEqual(trade.exit_reason, "take_profit")
        self.assertGreater(trade.pnl_ars, 0)
        self.assertAlmostEqual(trade.entry_price, 970.0 * 1.001, places=4)

    def test_hard_stop_cuts_losses_when_price_keeps_falling(self):
        t0 = datetime(2026, 1, 1, 0, 0, tzinfo=timezone.utc)
        trades = []
        t = t0
        for _ in range(35 * 6):
            trades.append({"created_at": iso(t), "price": "1000.0", "amount": "1.0"})
            t += timedelta(seconds=10)

        wick_time = t
        trades.append({"created_at": iso(wick_time), "price": "970.0", "amount": "1.0"})
        fill_time = wick_time + timedelta(seconds=5)
        trades.append({"created_at": iso(fill_time), "price": "970.5", "amount": "1.0"})

        # En vez de revertir, el precio sigue cayendo hasta perforar el hard stop
        # (entry ~970.97 * (1 - 4%) ~= 932.1).
        crash_time = fill_time + timedelta(minutes=5)
        trades.append({"created_at": iso(crash_time), "price": "920.0", "amount": "1.0"})

        bt_cfg = BacktestConfig(
            strategy=make_strategy_cfg(),
            position_size_ars=75000.0,
            maker_fee_pct=0.45,
            taker_fee_pct=0.60,
            max_daily_trades=6,
            max_daily_loss_pct=1.5,
            cooldown_minutes_after_trade=15,
        )
        report = run_backtest(trades, bt_cfg)

        self.assertEqual(report.n_closed, 1)
        trade = report.closed_trades[0]
        self.assertEqual(trade.exit_reason, "hard_stop")
        self.assertLess(trade.pnl_ars, 0)


if __name__ == "__main__":
    unittest.main()
