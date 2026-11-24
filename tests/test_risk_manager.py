import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from data_store import DataStore
from risk_manager import RiskLimits, RiskManager


def make_limits(**overrides) -> RiskLimits:
    base = dict(
        position_size_ars=75000.0,
        max_concurrent_positions=1,
        max_daily_loss_pct=1.5,
        max_daily_trades=6,
        cooldown_minutes_after_trade=15,
        maker_fee_pct=0.45,
        taker_fee_pct=0.60,
        min_net_profit_after_fees_pct=1.0,
    )
    base.update(overrides)
    return RiskLimits(**base)


class TestRiskManager(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.store = DataStore(str(Path(self._tmp.name) / "test.db"))

    def tearDown(self):
        self._tmp.cleanup()

    def test_validate_take_profit_rejects_tp_below_fees(self):
        rm = RiskManager(make_limits(maker_fee_pct=0.45, min_net_profit_after_fees_pct=1.0), self.store)
        # round trip = 0.9%, + 1.0% margen = 1.9% minimo requerido
        ok, _ = rm.validate_take_profit(1.5)
        self.assertFalse(ok)
        ok, _ = rm.validate_take_profit(2.5)
        self.assertTrue(ok)

    def test_position_size_major(self):
        rm = RiskManager(make_limits(position_size_ars=75000.0), self.store)
        self.assertAlmostEqual(rm.position_size_major(1500.0), 50.0)

    def test_can_open_new_trade_respects_max_concurrent(self):
        rm = RiskManager(make_limits(max_concurrent_positions=1), self.store)
        today = datetime.now(timezone.utc).date().isoformat()
        ok, _ = rm.can_open_new_trade(today)
        self.assertTrue(ok)

        self.store.log_trade_open(book="usdt_ars", mode="dry_run", buy_oid="SIM-1",
                                   entry_price=1500.0, amount_major=50.0,
                                   opened_at=datetime.now(timezone.utc).isoformat())
        ok, reason = rm.can_open_new_trade(today)
        self.assertFalse(ok)
        self.assertIn("posicion abierta", reason.lower())

    def test_can_open_new_trade_respects_daily_loss_kill_switch(self):
        rm = RiskManager(make_limits(max_daily_loss_pct=1.0, position_size_ars=100000.0, max_daily_trades=10), self.store)
        today = datetime.now(timezone.utc).date().isoformat()

        trade_id = self.store.log_trade_open(book="usdt_ars", mode="dry_run", buy_oid="SIM-1",
                                              entry_price=1500.0, amount_major=50.0,
                                              opened_at=datetime.now(timezone.utc).isoformat())
        # Perdida grande y realizada hoy, deberia disparar el kill switch.
        self.store.update_trade(trade_id, status="closed",
                                 closed_at=datetime.now(timezone.utc).isoformat(),
                                 pnl_ars=-50000.0)

        ok, reason = rm.can_open_new_trade(today)
        self.assertFalse(ok)
        self.assertIn("kill switch", reason.lower())


if __name__ == "__main__":
    unittest.main()
