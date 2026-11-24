"""
Analiza los trades historicos guardados en SQLite, arma velas de 1 minuto,
detecta eventos de "mechazo" segun los parametros de config.yaml, y muestra
un resumen por hora del dia (hora Argentina) de frecuencia y tasa de reversion.

Uso:
    python scripts/analyze_patterns.py

No modifica config.yaml automaticamente: imprime una sugerencia de
'allowed_hours_ars_tz' para que la copies vos mismo si te convence, despues de
revisar tambien el backtest (run_backtest.py) con y sin ese filtro.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import yaml

from candle_builder import build_candles
from data_store import DataStore
from pattern_analyzer import detect_wick_events, recommend_allowed_hours, summarize_by_hour


def load_config() -> dict:
    config_path = Path(__file__).resolve().parent.parent / "config.yaml"
    with open(config_path, encoding="utf-8") as f:
        return yaml.safe_load(f)


def main() -> None:
    cfg = load_config()
    book = cfg["exchange"]["book"]
    scfg = cfg["strategy"]

    store = DataStore(cfg["data"]["sqlite_path"])
    trades = store.fetch_trades(book)

    if len(trades) < 200:
        print(
            f"Solo hay {len(trades)} trades guardados para '{book}'. Dejá corriendo "
            "scripts/collect_data.py un buen tiempo (idealmente varios dias/semanas, "
            "cubriendo distintos horarios) antes de confiar en este analisis."
        )
        if not trades:
            return

    print(f"Trades disponibles: {len(trades)}")
    print("Construyendo velas de 1 minuto...")
    candles = build_candles(trades, interval_s=60)
    print(f"Velas de 1 minuto: {len(candles)}")

    print("Detectando eventos de mechazo historicos...")
    events = detect_wick_events(
        candles,
        reference_window_minutes=scfg["reference_window_minutes"],
        wick_drop_threshold_pct=scfg["wick_drop_threshold_pct"],
        max_holding_minutes=scfg["max_holding_minutes"],
        take_profit_pct=scfg["take_profit_pct"],
    )
    print(f"Eventos de mechazo (caida >= {scfg['wick_drop_threshold_pct']}%): {len(events)}")

    if not events:
        print("No se detectaron mechazos con el umbral actual. Probá bajar wick_drop_threshold_pct "
              "en config.yaml para explorar, o seguí recolectando mas datos.")
        return

    summary = summarize_by_hour(events)
    print("\nResumen por hora del dia (hora Argentina):")
    print(summary.to_string(index=False))

    recommended = recommend_allowed_hours(
        summary,
        min_sample_size=scfg["min_hourly_sample_size"],
        min_revert_rate=0.6,
    )
    print(f"\nHoras sugeridas (muestra >= {scfg['min_hourly_sample_size']} y tasa de reversion >= 60%): {recommended}")
    if not recommended:
        print(
            "Ninguna hora junta todavia suficiente muestra. Es normal al principio: "
            "seguí recolectando datos antes de activar un filtro horario estricto."
        )
    else:
        print(
            "Si te convence, copiá esta lista a 'allowed_hours_ars_tz' en config.yaml. "
            "Despues corré run_backtest.py CON y SIN el filtro para comparar resultados netos de comisiones."
        )


if __name__ == "__main__":
    main()
