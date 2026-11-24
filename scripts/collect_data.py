"""
Recolector continuo de trades publicos de Bitso para construir historial propio
(Bitso no expone velas historicas via REST). Dejalo corriendo en background:

    python scripts/collect_data.py

Hace polling cada 'trades_poll_seconds' (config.yaml) sobre /v3/trades, pagina
hacia adelante con 'marker' a partir del ultimo tid visto, y guarda todo en
SQLite (data/market_data.db). Cuanto mas tiempo lo dejes corriendo, mas confiable
sera el analisis de patrones por horario (scripts/analyze_patterns.py).

Se puede cortar con Ctrl+C en cualquier momento; al reiniciar retoma desde el
ultimo tid guardado (no pierde ni duplica datos).
"""

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import yaml

from bitso_client import BitsoClient
from data_store import DataStore


def load_config() -> dict:
    config_path = Path(__file__).resolve().parent.parent / "config.yaml"
    with open(config_path, encoding="utf-8") as f:
        return yaml.safe_load(f)


def main() -> None:
    cfg = load_config()
    book = cfg["exchange"]["book"]
    poll_s = cfg["data"]["trades_poll_seconds"]

    client = BitsoClient(base_url=cfg["exchange"]["base_url"])
    store = DataStore(cfg["data"]["sqlite_path"])

    print(f"Recolectando trades publicos de '{book}' cada {poll_s}s. Ctrl+C para detener.")

    marker = store.last_trade_tid(book)
    if marker:
        print(f"Retomando desde tid {marker}.")
    else:
        print("Sin historial previo, arrancando desde los trades mas recientes.")

    while True:
        try:
            if marker is None:
                batch = client.trades(book, limit=100, sort="desc")
                batch = list(reversed(batch))
            else:
                batch = client.trades(book, limit=100, marker=str(marker), sort="asc")

            if batch:
                inserted = store.insert_trades(book, batch)
                marker = max(int(t["tid"]) for t in batch)
                if inserted:
                    print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] +{inserted} trades nuevos (ultimo tid={marker})")
        except Exception as exc:  # noqa: BLE001 - loop de larga duracion, nunca debe morir por un error transitorio
            print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] error de red/API, reintento en {poll_s}s: {exc}")

        time.sleep(poll_s)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nDetenido por el usuario.")
