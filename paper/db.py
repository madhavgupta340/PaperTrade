"""SQLite persistence for the simulated account."""
from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from .timeutil import iso_utc

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS holdings (
    symbol     TEXT PRIMARY KEY,
    name       TEXT,
    shares     INTEGER NOT NULL CHECK (shares > 0),
    avg_cost   REAL NOT NULL CHECK (avg_cost >= 0),
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS orders (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol         TEXT NOT NULL,
    name           TEXT,
    side           TEXT NOT NULL CHECK (side IN ('buy', 'sell')),
    order_type     TEXT NOT NULL CHECK (order_type IN ('market', 'limit')),
    quantity       INTEGER NOT NULL CHECK (quantity > 0),
    limit_price    REAL,
    status         TEXT NOT NULL CHECK (status IN ('open', 'filled', 'cancelled')),
    reserved_cents INTEGER NOT NULL DEFAULT 0,
    fill_price     REAL,
    created_at     TEXT NOT NULL,
    updated_at     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS orders_status ON orders (status);
CREATE TABLE IF NOT EXISTS transactions (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id          INTEGER REFERENCES orders (id),
    symbol            TEXT NOT NULL,
    name              TEXT,
    side              TEXT NOT NULL CHECK (side IN ('buy', 'sell')),
    quantity          INTEGER NOT NULL CHECK (quantity > 0),
    price             REAL NOT NULL,
    amount_cents      INTEGER NOT NULL,
    realized_pl_cents INTEGER,
    cash_after_cents  INTEGER NOT NULL,
    data_source       TEXT,
    created_at        TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS transactions_created ON transactions (created_at);
CREATE TABLE IF NOT EXISTS watchlist (
    symbol   TEXT PRIMARY KEY,
    name     TEXT,
    added_at TEXT NOT NULL
);
"""


class Database:
    """One shared connection guarded by a lock; the app is low-traffic and local."""

    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.lock = threading.RLock()
        self.conn = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        with self.lock:
            self.conn.execute("PRAGMA journal_mode=WAL")
            self.conn.execute("PRAGMA foreign_keys=ON")
            self.conn.executescript(SCHEMA)
            self._migrate()

    def _migrate(self) -> None:
        """Add columns introduced after the first release, keeping existing data."""
        for table, column, ddl in (
            ("orders", "checked_at", "TEXT"),        # last time this open order was checked against prices
            ("transactions", "note", "TEXT"),        # e.g. "Filled while you were away"
        ):
            existing = {r["name"] for r in self.conn.execute(f"PRAGMA table_info({table})")}
            if column not in existing:
                self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")

    @contextmanager
    def read(self) -> Iterator[sqlite3.Connection]:
        with self.lock:
            yield self.conn

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        """An atomic write transaction. Do not nest."""
        with self.lock:
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                yield self.conn
            except BaseException:
                self.conn.execute("ROLLBACK")
                raise
            else:
                self.conn.execute("COMMIT")

    def initialize(self, starting_cents: int, default_watchlist: list) -> None:
        with self.tx() as c:
            if get_setting(c, "cash_cents") is not None:
                return
            now = iso_utc()
            set_setting(c, "starting_balance_cents", starting_cents)
            set_setting(c, "cash_cents", starting_cents)
            set_setting(c, "reset_at", now)
            for symbol, name in default_watchlist:
                c.execute(
                    "INSERT OR IGNORE INTO watchlist (symbol, name, added_at) VALUES (?, ?, ?)",
                    (symbol, name, now),
                )


def get_setting(conn: sqlite3.Connection, key: str):
    row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else None


def set_setting(conn: sqlite3.Connection, key: str, value) -> None:
    conn.execute(
        "INSERT INTO settings (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, str(value)),
    )
