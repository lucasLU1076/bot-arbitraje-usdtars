"""
Almacenamiento local (SQLite) de trades publicos y velas agregadas.

Bitso no ofrece un endpoint REST de velas/OHLC historicas (confirmado contra
su documentacion oficial), asi que la unica forma de construir un historial
propio es ir guardando los trades publicos a medida que ocurren (scripts/collect_data.py)
y agregarlos nosotros mismos en velas (candle_builder.py). Esto implica que el
analisis de patrones por horario sera tan bueno como la cantidad de dias que se
haya dejado corriendo el recolector antes de confiar en el filtro horario.
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path


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

CREATE TABLE IF NOT EXISTS candles (
    book TEXT NOT NULL,
    interval_s INTEGER NOT NULL,
    bucket_start TEXT NOT NULL,
    open REAL NOT NULL,
    high REAL NOT NULL,
    low REAL NOT NULL,
    close REAL NOT NULL,
    volume REAL NOT NULL,
    trade_count INTEGER NOT NULL,
    PRIMARY KEY (book, interval_s, bucket_start)
);

CREATE TABLE IF NOT EXISTS wick_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    book TEXT NOT NULL,
    detected_at TEXT NOT NULL,
    reference_price REAL NOT NULL,
    low_price REAL NOT NULL,
    drop_pct REAL NOT NULL,
    reverted_within_minutes REAL,
    max_reversion_pct REAL
);

CREATE TABLE IF NOT EXISTS trade_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    opened_at TEXT NOT NULL,
    closed_at TEXT,
    book TEXT NOT NULL,
    mode TEXT NOT NULL,              -- 'dry_run' o 'live'
    buy_oid TEXT,
    sell_oid TEXT,
    entry_price REAL,
    exit_price REAL,
    amount_major REAL,
    pnl_ars REAL,
    pnl_pct REAL,
    exit_reason TEXT,                -- 'take_profit' | 'hard_stop' | 'timeout' | 'manual'
    status TEXT NOT NULL,              -- 'pending_buy' | 'open' | 'closed' | 'cancelled'
    sheet_row INTEGER                 -- fila en el Google Sheet (si el registro esta activado)
);
"""


class DataStore:
    def __init__(self, path: str):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._path = path
        with self._connect() as conn:
            conn.executescript(SCHEMA)
            try:
                conn.execute("ALTER TABLE trade_log ADD COLUMN sheet_row INTEGER")
            except sqlite3.OperationalError:
                pass  # ya existia (DB creada antes de agregar esta columna)

    @contextmanager
    def _connect(self):
        conn = sqlite3.connect(self._path)
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def insert_trades(self, book: str, trades: list[dict]) -> int:
        """Inserta trades publicos de Bitso (formato /v3/trades). Ignora duplicados por tid."""
        rows = [
            (int(t["tid"]), book, t["created_at"], float(t["price"]), float(t["amount"]), t["maker_side"])
            for t in trades
        ]
        with self._connect() as conn:
            cur = conn.executemany(
                "INSERT OR IGNORE INTO trades(tid, book, created_at, price, amount, maker_side) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                rows,
            )
            return cur.rowcount

    def last_trade_tid(self, book: str) -> int | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT MAX(tid) FROM trades WHERE book = ?", (book,)
            ).fetchone()
            return row[0]

    def fetch_trades(self, book: str, since_iso: str | None = None) -> list[dict]:
        query = "SELECT tid, created_at, price, amount, maker_side FROM trades WHERE book = ?"
        params: list = [book]
        if since_iso:
            query += " AND created_at >= ?"
            params.append(since_iso)
        query += " ORDER BY created_at ASC"
        with self._connect() as conn:
            cur = conn.execute(query, params)
            cols = [d[0] for d in cur.description]
            return [dict(zip(cols, row)) for row in cur.fetchall()]

    def upsert_candles(self, book: str, interval_s: int, candles: list[dict]) -> None:
        rows = [
            (book, interval_s, c["bucket_start"], c["open"], c["high"], c["low"],
             c["close"], c["volume"], c["trade_count"])
            for c in candles
        ]
        with self._connect() as conn:
            conn.executemany(
                "INSERT INTO candles(book, interval_s, bucket_start, open, high, low, close, volume, trade_count) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(book, interval_s, bucket_start) DO UPDATE SET "
                "open=excluded.open, high=excluded.high, low=excluded.low, close=excluded.close, "
                "volume=excluded.volume, trade_count=excluded.trade_count",
                rows,
            )

    def fetch_candles(self, book: str, interval_s: int) -> list[dict]:
        with self._connect() as conn:
            cur = conn.execute(
                "SELECT bucket_start, open, high, low, close, volume, trade_count "
                "FROM candles WHERE book = ? AND interval_s = ? ORDER BY bucket_start ASC",
                (book, interval_s),
            )
            cols = [d[0] for d in cur.description]
            return [dict(zip(cols, row)) for row in cur.fetchall()]

    def insert_wick_event(self, event: dict) -> int:
        with self._connect() as conn:
            cur = conn.execute(
                "INSERT INTO wick_events(book, detected_at, reference_price, low_price, drop_pct, "
                "reverted_within_minutes, max_reversion_pct) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (event["book"], event["detected_at"], event["reference_price"], event["low_price"],
                 event["drop_pct"], event.get("reverted_within_minutes"), event.get("max_reversion_pct")),
            )
            return cur.lastrowid

    def fetch_wick_events(self, book: str) -> list[dict]:
        with self._connect() as conn:
            cur = conn.execute(
                "SELECT * FROM wick_events WHERE book = ? ORDER BY detected_at ASC", (book,)
            )
            cols = [d[0] for d in cur.description]
            return [dict(zip(cols, row)) for row in cur.fetchall()]

    def log_trade_open(self, book: str, mode: str, buy_oid: str | None, entry_price: float,
                        amount_major: float, opened_at: str) -> int:
        with self._connect() as conn:
            cur = conn.execute(
                "INSERT INTO trade_log(opened_at, book, mode, buy_oid, entry_price, amount_major, status) "
                "VALUES (?, ?, ?, ?, ?, ?, 'pending_buy')",
                (opened_at, book, mode, buy_oid, entry_price, amount_major),
            )
            return cur.lastrowid

    def update_trade(self, trade_id: int, **fields) -> None:
        if not fields:
            return
        set_clause = ", ".join(f"{k} = ?" for k in fields)
        with self._connect() as conn:
            conn.execute(
                f"UPDATE trade_log SET {set_clause} WHERE id = ?",
                (*fields.values(), trade_id),
            )

    def fetch_trade_log(self, status: str | None = None) -> list[dict]:
        query = "SELECT * FROM trade_log"
        params: list = []
        if status:
            query += " WHERE status = ?"
            params.append(status)
        query += " ORDER BY opened_at ASC"
        with self._connect() as conn:
            cur = conn.execute(query, params)
            cols = [d[0] for d in cur.description]
            return [dict(zip(cols, row)) for row in cur.fetchall()]

    def daily_realized_pnl(self, date_iso: str) -> float:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT COALESCE(SUM(pnl_ars), 0) FROM trade_log "
                "WHERE status = 'closed' AND substr(closed_at, 1, 10) = ?",
                (date_iso,),
            ).fetchone()
            return row[0] or 0.0

    def daily_trade_count(self, date_iso: str) -> int:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) FROM trade_log WHERE substr(opened_at, 1, 10) = ?",
                (date_iso,),
            ).fetchone()
            return row[0] or 0
