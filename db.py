"""
SQLite store for OI chain snapshots — the "existing database" the OI Flow tab
will read from. Lives at ./data/oi_chain.db on the droplet (or
$OPTIONS_DATA_DIR/oi_chain.db).

Schema:
  chain_snapshot  — per (timestamp, underlying, strike, expiry, CE/PE) row
  recorder_log    — one row per recorder invocation (success or error)

Time-stored fields are ISO 8601 strings with explicit IST offset. SQLite's
DATE() function works directly on these to bucket by trading day.

All rows are de-duplicated by (ts, underlying, strike, expiry, opt_type), so
the recorder is safe to retry within the same minute.
"""
from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterator, Optional

ROOT = Path(__file__).parent
DATA_DIR = Path(os.environ.get("OPTIONS_DATA_DIR", str(ROOT / "data")))
DB_PATH = DATA_DIR / "oi_chain.db"

IST = timezone(timedelta(hours=5, minutes=30))

SCHEMA = """
CREATE TABLE IF NOT EXISTS chain_snapshot (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          TEXT    NOT NULL,
    underlying  TEXT    NOT NULL,
    spot        REAL    NOT NULL,
    expiry      TEXT    NOT NULL,
    strike      INTEGER NOT NULL,
    opt_type    TEXT    NOT NULL CHECK (opt_type IN ('CE', 'PE')),
    ltp         REAL,
    volume      INTEGER,
    oi          INTEGER,
    UNIQUE(ts, underlying, strike, expiry, opt_type)
);

CREATE INDEX IF NOT EXISTS idx_chain_underlying_ts ON chain_snapshot(underlying, ts);
CREATE INDEX IF NOT EXISTS idx_chain_ts            ON chain_snapshot(ts);
CREATE INDEX IF NOT EXISTS idx_chain_underlying_day_strike
    ON chain_snapshot(underlying, DATE(ts), strike, opt_type);

CREATE TABLE IF NOT EXISTS recorder_log (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ts              TEXT    NOT NULL,
    underlying      TEXT    NOT NULL,
    expiry          TEXT,
    rows_inserted   INTEGER DEFAULT 0,
    strikes         INTEGER DEFAULT 0,
    error           TEXT
);

CREATE INDEX IF NOT EXISTS idx_recorder_log_ts ON recorder_log(ts);

CREATE TABLE IF NOT EXISTS underlying_candle (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          TEXT    NOT NULL,
    underlying  TEXT    NOT NULL,
    open        REAL,
    high        REAL,
    low         REAL,
    close       REAL,
    volume      INTEGER,
    UNIQUE(ts, underlying)
);

CREATE INDEX IF NOT EXISTS idx_underlying_candle_ts ON underlying_candle(underlying, ts);
"""


def init_db() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(str(DB_PATH)) as conn:
        conn.executescript(SCHEMA)
        # WAL mode → safe for concurrent reads while the recorder writes.
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA synchronous=NORMAL;")


@contextmanager
def get_conn() -> Iterator[sqlite3.Connection]:
    init_db()
    conn = sqlite3.connect(str(DB_PATH), timeout=10)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()


# ---------- writes ----------

def insert_snapshots(conn: sqlite3.Connection, rows: list) -> int:
    """Bulk insert. rows = list of dicts with all chain_snapshot fields.
    Returns the number of newly-inserted rows (duplicates are ignored)."""
    if not rows:
        return 0
    keys = ("ts", "underlying", "spot", "expiry", "strike", "opt_type",
            "ltp", "volume", "oi")
    sql = (
        f"INSERT OR IGNORE INTO chain_snapshot ({','.join(keys)}) "
        f"VALUES ({','.join('?' for _ in keys)})"
    )
    data = [tuple(r.get(k) for k in keys) for r in rows]
    cur = conn.executemany(sql, data)
    conn.commit()
    return cur.rowcount


def insert_candles(conn: sqlite3.Connection, rows: list) -> int:
    """rows = list of dicts with ts, underlying, open, high, low, close, volume."""
    if not rows:
        return 0
    keys = ("ts", "underlying", "open", "high", "low", "close", "volume")
    sql = (f"INSERT OR IGNORE INTO underlying_candle ({','.join(keys)}) "
           f"VALUES ({','.join('?' for _ in keys)})")
    data = [tuple(r.get(k) for k in keys) for r in rows]
    cur = conn.executemany(sql, data)
    conn.commit()
    return cur.rowcount


def log_recorder_run(
    conn: sqlite3.Connection,
    underlying: str,
    expiry: Optional[str],
    rows_inserted: int,
    strikes: int = 0,
    error: Optional[str] = None,
) -> None:
    conn.execute(
        "INSERT INTO recorder_log (ts, underlying, expiry, rows_inserted, strikes, error) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (datetime.now(IST).isoformat(), underlying, expiry, rows_inserted, strikes, error),
    )
    conn.commit()


# ---------- reads (used by the OI Flow tab in PR B) ----------

def available_days(conn: sqlite3.Connection, underlying: Optional[str] = None,
                   limit: int = 30) -> list:
    """Return list of YYYY-MM-DD dates (newest first) that have data."""
    if underlying:
        cur = conn.execute(
            "SELECT DISTINCT DATE(ts) AS d FROM chain_snapshot "
            "WHERE underlying = ? ORDER BY d DESC LIMIT ?",
            (underlying, limit),
        )
    else:
        cur = conn.execute(
            "SELECT DISTINCT DATE(ts) AS d FROM chain_snapshot ORDER BY d DESC LIMIT ?",
            (limit,),
        )
    return [r["d"] for r in cur.fetchall()]


def day_summary(conn: sqlite3.Connection) -> list:
    """One row per (underlying, day) with row count + time range."""
    cur = conn.execute(
        "SELECT underlying, DATE(ts) AS day, COUNT(*) AS n, "
        "MIN(ts) AS first_ts, MAX(ts) AS last_ts "
        "FROM chain_snapshot GROUP BY underlying, DATE(ts) "
        "ORDER BY day DESC, underlying"
    )
    return [dict(r) for r in cur.fetchall()]


def recent_recorder_runs(conn: sqlite3.Connection, limit: int = 20) -> list:
    cur = conn.execute(
        "SELECT * FROM recorder_log ORDER BY id DESC LIMIT ?", (limit,)
    )
    return [dict(r) for r in cur.fetchall()]
