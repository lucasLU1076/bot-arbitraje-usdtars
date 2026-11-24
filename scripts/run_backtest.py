"""
Corre el backtest trade-a-trade sobre los datos historicos guardados en SQLite.

Uso:
    python scripts/run_backtest.py

Lee todos los parametros desde config.yaml (misma fuente de verdad que el bot
en vivo), asi que para comparar variantes alcanza con editar config.yaml y
volver a correr este script. Antes de arriesgar capital real, el resultado
de este backtest deberia mostrar PnL neto positivo de forma consistente
(no solo en el mejor sub-periodo elegido a dedo).
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import yaml

from backtester import BacktestConfig, run_backtest
from data_store import DataStore
from risk_manager import RiskLimits, RiskManager
from strategy import StrategyConfig


def load_config() -> dict:
    config_path = Path(__file__).resolve().parent.parent / "config.yaml"
    with open(config_path, encoding="utf-8") as f:
        return yaml.safe_load(f)


def main() -> None:
    cfg = load_config()
    book = cfg["exchange"]["book"]
    scfg = cfg["strategy"]
    fcfg = cfg["fees"]
    ccfg = cfg["capital"]

    store = DataStore(cfg["data"]["sqlite_path"])
    trades = store.fetch_trades(book)

    if len(trades) < 500:
        print(f"Muy pocos trades guardados ({len(trades)}). Corré scripts/collect_data.py mas tiempo primero.")
        return

    strategy_cfg = StrategyConfig(
        reference_window_minutes=scfg["reference_window_minutes"],
        wick_drop_threshold_pct=scfg["wick_drop_threshold_pct"],
        entry_offset_pct=scfg["entry_offset_pct"],
        take_profit_pct=scfg["take_profit_pct"],
        buy_order_timeout_seconds=scfg["buy_order_timeout_seconds"],
        max_holding_minutes=scfg["max_holding_minutes"],
        hard_stop_loss_pct=scfg["hard_stop_loss_pct"],
        allowed_hours_ars_tz=scfg["allowed_hours_ars_tz"],
        min_hourly_sample_size=scfg["min_hourly_sample_size"],
    )

    risk_limits = RiskLimits(
        position_size_ars=ccfg["position_size_ars"],
        max_concurrent_positions=ccfg["max_concurrent_positions"],
        max_daily_loss_pct=ccfg["max_daily_loss_pct"],
        max_daily_trades=ccfg["max_daily_trades"],
        cooldown_minutes_after_trade=scfg["cooldown_minutes_after_trade"],
        maker_fee_pct=fcfg["maker_pct"],
        taker_fee_pct=fcfg["taker_pct"],
        min_net_profit_after_fees_pct=scfg["min_net_profit_after_fees_pct"],
    )
    risk_manager = RiskManager(risk_limits, store)

    ok, msg = risk_manager.validate_take_profit(scfg["take_profit_pct"])
    if not ok:
        print(f"CONFIGURACION INVALIDA: {msg}")
        print("Ajustá take_profit_pct o min_net_profit_after_fees_pct en config.yaml antes de backtestear.")
        return

    bt_cfg = BacktestConfig(
        strategy=strategy_cfg,
        position_size_ars=ccfg["position_size_ars"],
        maker_fee_pct=fcfg["maker_pct"],
        taker_fee_pct=fcfg["taker_pct"],
        max_daily_trades=ccfg["max_daily_trades"],
        max_daily_loss_pct=ccfg["max_daily_loss_pct"],
        cooldown_minutes_after_trade=scfg["cooldown_minutes_after_trade"],
    )

    print(f"Backtesteando {len(trades)} trades de '{book}'...")
    report = run_backtest(trades, bt_cfg)

    print("\n--- Resultado del backtest ---")
    print(report.summary_text(ccfg["position_size_ars"]))

    if report.n_closed < 20:
        print(
            "Menos de 20 operaciones cerradas en el periodo analizado: la muestra es "
            "chica para sacar conclusiones solidas. Segui recolectando datos."
        )

    print(
        "Recordatorio: este resultado no modela profundidad real del libro, otras "
        "ordenes compitiendo por el mismo precio, ni latencia. Es una cota optimista, "
        "no una promesa de retorno."
    )


if __name__ == "__main__":
    main()
