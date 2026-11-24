"""
Version autonoma (sin dependencias externas, solo libreria estandar de Python)
del recolector de trades, pensada para correr como CRON JOB en el hosting
compartido de Hostinger, donde no se puede instalar pip ni dejar un proceso
en loop infinito.

Cada ejecucion: se conecta, trae los trades nuevos desde el ultimo tid
guardado, los inserta en su propio sqlite local (separado del de tu PC), y
termina. Pensado para dispararse cada 5 minutos via cron.

Esto es la pata de "recoleccion de datos", NO trading en vivo: hosting
compartido no puede sostener un proceso continuo reaccionando en segundos,
asi que esto solo acumula historial. Ver README.md del proyecto para el plan
completo (VPS para cuando se quiera el trading en vivo real).
"""

import json
import sqlite3
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

BOOK = "usdt_ars"
BASE_URL = "https://api.bitso.com/v3"
DB_PATH = Path(__file__).resolve().parent / "collector_state.db"
MAX_PAGES_PER_RUN = 10  # tope de seguridad por si el cron tarda en dispararse y hay que ponerse al dia

SCHEMA = """
CREATE TABLE IF NOT EXISTS trades (
    tid INTEGER PRIMARY KEY,
    book TEXT NOT NULL,
    created_at TEXT NOT NULL,
    price REAL NOT NULL,
    amount REAL NOT NULL,
    maker_side TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_trades_book_time ON trades(book, created_at);
CREATE TABLE IF NOT EXISTS run_log (
    run_at TEXT NOT NULL,
    inserted INTEGER NOT NULL,
    last_tid INTEGER,
    last_price REAL,
    note TEXT
);
"""


def http_get_json(url):
    req = urllib.request.Request(url, headers={"User-Agent": "bot-arbitraje-collector/1.0"})
    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.loads(resp.read().decode("utf-8"))


def fetch_trades(marker, sort):
    url = f"{BASE_URL}/trades?book={BOOK}&limit=100&sort={sort}"
    if marker:
        url += f"&marker={marker}"
    data = http_get_json(url)
    return data.get("payload", [])


def main():
    conn = sqlite3.connect(str(DB_PATH))
    conn.executescript(SCHEMA)

    row = conn.execute("SELECT MAX(tid) FROM trades WHERE book = ?", (BOOK,)).fetchone()
    marker = row[0]

    total_inserted = 0
    last_price = None
    note = "ok"

    try:
        if marker is None:
            batch = fetch_trades(None, sort="desc")
            batch = list(reversed(batch))
            if batch:
                marker = max(int(t["tid"]) for t in batch)
            pages = [batch] if batch else []
        else:
            pages = []
            current_marker = str(marker)
            for _ in range(MAX_PAGES_PER_RUN):
                batch = fetch_trades(current_marker, sort="asc")
                if not batch:
                    break
                pages.append(batch)
                current_marker = str(max(int(t["tid"]) for t in batch))
                marker = int(current_marker)
                if len(batch) < 100:
                    break

        for batch in pages:
            rows = [
                (int(t["tid"]), BOOK, t["created_at"], float(t["price"]), float(t["amount"]), t["maker_side"])
                for t in batch
            ]
            conn.executemany(
                "INSERT OR IGNORE INTO trades(tid, book, created_at, price, amount, maker_side) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                rows,
            )
            total_inserted += len(rows)
            if batch:
                last_price = float(batch[-1]["price"])

    except urllib.error.URLError as exc:
        note = f"error de red: {exc}"
    except Exception as exc:  # noqa: BLE001 - un cron nunca deberia "fallar" ruidosamente, solo loggear
        note = f"error inesperado: {exc}"

    conn.execute(
        "INSERT INTO run_log(run_at, inserted, last_tid, last_price, note) VALUES (datetime('now'), ?, ?, ?, ?)",
        (total_inserted, marker, last_price, note),
    )
    conn.commit()
    conn.close()

    print(f"inserted={total_inserted} last_tid={marker} last_price={last_price} note={note}")


if __name__ == "__main__":
    main()
