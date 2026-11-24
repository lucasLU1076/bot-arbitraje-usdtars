"""
Punto de entrada del bot. Por defecto corre en modo simulado (dry-run): se
conecta a los datos PUBLICOS reales de Bitso, detecta mechazos, "coloca" y
"llena" ordenes en memoria/SQLite, pero jamas llama a un endpoint privado.

Para operar con dinero real hacen falta las TRES cosas a la vez:
  1. config.yaml -> mode.dry_run: false
  2. .env -> BITSO_LIVE_CONFIRM=YES
  3. Correr este script con el flag --live

Si falta cualquiera de las tres, corre en modo simulado sin excepcion.

Uso:
    python scripts/run_bot.py                 # dry-run (recomendado para empezar)
    python scripts/run_bot.py --live           # real, solo si las 3 trabas estan puestas
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import os

import yaml
from dotenv import load_dotenv

from bitso_client import BitsoClient
from data_store import DataStore
from live_engine import LiveEngine
from risk_manager import RiskLimits, RiskManager
from strategy import StrategyConfig


def load_config() -> dict:
    config_path = Path(__file__).resolve().parent.parent / "config.yaml"
    with open(config_path, encoding="utf-8") as f:
        return yaml.safe_load(f)


def main() -> None:
    parser = argparse.ArgumentParser(description="Bot de mechazos USDT/ARS en Bitso.")
    parser.add_argument("--live", action="store_true",
                         help="Intenta operar con dinero real (requiere ademas dry_run:false en "
                              "config.yaml y BITSO_LIVE_CONFIRM=YES en .env).")
    args = parser.parse_args()

    load_dotenv(Path(__file__).resolve().parent.parent / ".env")
    cfg = load_config()

    wants_live = args.live and cfg["mode"]["dry_run"] is False
    env_confirmed = os.environ.get("BITSO_LIVE_CONFIRM") == "YES"
    is_live = wants_live and env_confirmed

    if args.live and cfg["mode"]["dry_run"] is True:
        print("ADVERTENCIA: pediste --live pero config.yaml tiene mode.dry_run: true. Corriendo en modo simulado.")
    if args.live and not env_confirmed:
        print("ADVERTENCIA: pediste --live pero falta BITSO_LIVE_CONFIRM=YES en .env. Corriendo en modo simulado.")

    book = cfg["exchange"]["book"]
    api_key = os.environ.get("BITSO_API_KEY") or None
    api_secret = os.environ.get("BITSO_API_SECRET") or None

    if is_live and not (api_key and api_secret):
        print("ERROR: modo real pedido pero faltan BITSO_API_KEY / BITSO_API_SECRET en .env. Abortando.")
        sys.exit(1)

    client = BitsoClient(base_url=cfg["exchange"]["base_url"], api_key=api_key, api_secret=api_secret)
    store = DataStore(cfg["data"]["sqlite_path"])

    scfg = cfg["strategy"]
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

    fcfg = cfg["fees"]
    ccfg = cfg["capital"]
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
        print(f"CONFIGURACION INVALIDA, no arranco: {msg}")
        sys.exit(1)

    if is_live:
        print("=" * 70)
        print("MODO REAL ACTIVADO. El bot va a enviar ordenes reales a tu cuenta de Bitso.")
        print(f"Tamano por operacion: {ccfg['position_size_ars']} ARS. Par: {book}.")
        print("Presiona Ctrl+C ahora si no estabas seguro de esto.")
        print("=" * 70)
    else:
        print("Modo DRY-RUN (simulado): no se enviara ninguna orden real.")

    engine = LiveEngine(
        book=book, client=client, store=store, risk_manager=risk_manager,
        strategy_cfg=strategy_cfg, is_live=is_live,
        poll_seconds=cfg["data"]["trades_poll_seconds"],
    )

    try:
        engine.run_forever()
    except KeyboardInterrupt:
        print("\nDetenido por el usuario.")


if __name__ == "__main__":
    main()
