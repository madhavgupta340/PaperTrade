"""Persistence for the simulated account.

SQLite (a local file) is the default. When DATABASE_URL points at Postgres, as on Vercel where the
filesystem isn't persistent, the same SQL runs on Postgres. Queries are written in the subset both
understand: `?` placeholders are translated, and inserts use RETURNING / ON CONFLICT.
"""
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


class _BaseDatabase:
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
                    "INSERT INTO watchlist (symbol, name, added_at) VALUES (?, ?, ?) ON CONFLICT DO NOTHING",
                    (symbol, name, now),
                )


class Database(_BaseDatabase):
    """SQLite: one shared connection guarded by a lock; the app is low-traffic."""

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


PG_SCHEMA = [
    "CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)",
    """CREATE TABLE IF NOT EXISTS holdings (
        symbol TEXT PRIMARY KEY, name TEXT,
        shares BIGINT NOT NULL CHECK (shares > 0),
        avg_cost DOUBLE PRECISION NOT NULL CHECK (avg_cost >= 0),
        updated_at TEXT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS orders (
        id BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
        symbol TEXT NOT NULL, name TEXT,
        side TEXT NOT NULL CHECK (side IN ('buy', 'sell')),
        order_type TEXT NOT NULL CHECK (order_type IN ('market', 'limit')),
        quantity BIGINT NOT NULL CHECK (quantity > 0),
        limit_price DOUBLE PRECISION,
        status TEXT NOT NULL CHECK (status IN ('open', 'filled', 'cancelled')),
        reserved_cents BIGINT NOT NULL DEFAULT 0,
        fill_price DOUBLE PRECISION,
        created_at TEXT NOT NULL, updated_at TEXT NOT NULL, checked_at TEXT)""",
    "CREATE INDEX IF NOT EXISTS orders_status ON orders (status)",
    """CREATE TABLE IF NOT EXISTS transactions (
        id BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
        order_id BIGINT REFERENCES orders (id),
        symbol TEXT NOT NULL, name TEXT,
        side TEXT NOT NULL CHECK (side IN ('buy', 'sell')),
        quantity BIGINT NOT NULL CHECK (quantity > 0),
        price DOUBLE PRECISION NOT NULL,
        amount_cents BIGINT NOT NULL,
        realized_pl_cents BIGINT,
        cash_after_cents BIGINT NOT NULL,
        data_source TEXT, note TEXT,
        created_at TEXT NOT NULL)""",
    "CREATE INDEX IF NOT EXISTS transactions_created ON transactions (created_at)",
    "CREATE TABLE IF NOT EXISTS watchlist (symbol TEXT PRIMARY KEY, name TEXT, added_at TEXT NOT NULL)",
]


class _PgConnection:
    """Gives a psycopg connection the small sqlite3-style surface the engine uses."""

    def __init__(self, raw):
        self._raw = raw

    def execute(self, sql: str, params=()):
        cur = self._raw.cursor()
        cur.execute(sql.replace("?", "%s"), params)
        return cur


class PostgresDatabase(_BaseDatabase):
    """Postgres for serverless hosting. Writes are serialized with an advisory lock, so two
    function instances can't fill the same order or overspend cash at the same time."""

    LOCK_KEY = 7_311_942

    def __init__(self, url: str):
        try:
            import psycopg
            from psycopg.rows import dict_row
        except ImportError:
            raise SystemExit("DATABASE_URL is set, so the 'psycopg' package is required: pip install -r requirements.txt")
        self._psycopg, self._dict_row = psycopg, dict_row
        self._url = url
        self._raw = None
        self.lock = threading.RLock()
        with self.lock:
            c = self._conn()
            for statement in PG_SCHEMA:
                c.execute(statement)

    def _conn(self) -> _PgConnection:
        if self._raw is None or self._raw.closed or self._raw.broken:
            # prepare_threshold=None keeps it compatible with pooled (PgBouncer) connection strings.
            self._raw = self._psycopg.connect(self._url, autocommit=True, row_factory=self._dict_row,
                                              prepare_threshold=None, connect_timeout=10)
        return _PgConnection(self._raw)

    @contextmanager
    def read(self):
        with self.lock:
            yield self._conn()

    @contextmanager
    def tx(self):
        with self.lock:
            c = self._conn()
            c.execute("BEGIN")
            try:
                c.execute("SELECT pg_advisory_xact_lock(?)", (self.LOCK_KEY,))
                yield c
            except BaseException:
                if not self._raw.broken:
                    c.execute("ROLLBACK")
                raise
            else:
                c.execute("COMMIT")


def open_database(path: Path, url: str = ""):
    return PostgresDatabase(url) if url else Database(path)


def get_setting(conn: sqlite3.Connection, key: str):
    row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else None


def set_setting(conn: sqlite3.Connection, key: str, value) -> None:
    conn.execute(
        "INSERT INTO settings (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, str(value)),
    )
