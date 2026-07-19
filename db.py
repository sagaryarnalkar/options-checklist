"""
SQLite store for the whole dashboard. Lives at ./data/oi_chain.db on the
droplet (override dir via $OPTIONS_DATA_DIR). WAL mode: the per-minute
recorder writes while the web app reads.

Schema:
  chain_snapshot          — one row per (minute, underlying, strike, CE/PE):
                            ltp / cumulative volume / OI. Nearest expiry,
                            ATM ± 10. Written live by recorder.py and by the
                            historical backfill (login-gap repair). UNIQUE
                            constraint makes both paths idempotent.
  underlying_candle       — 1-min OHLC of the underlying (chart + expiry-day
                            settle prices for the paper ledger).
  recorder_log            — one row per recorder/backfill invocation.
  score_marker_outcomes   — forward returns (+5/15/30 min) of OI-Flow score
                            markers under a PINNED definition (see oi_flow.py)
                            for signal-quality analysis.
  paper_trades            — the paper-trading ledger (10 lots per actionable
                            recommendation): legs JSON, entry/exit values
                            (net-cash-per-unit convention, SELL +, BUY −),
                            entry/exit costs, gross and NET realized P&L,
                            hedge-roll history in meta_json.
  paper_marks             — mark-to-market history per open paper trade
                            (net "if closed now" incl. estimated exit costs).

init_db() also runs idempotent ALTER-TABLE migrations for columns added
after tables shipped (e.g. the #48 cost columns).

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

-- Track every score marker that gets produced so we can later test whether
-- the indicator actually predicts what it claims to predict. Forward-return
-- columns are filled in over time as the requisite future spot becomes
-- available; markers near end-of-day legitimately stay NULL on long horizons.
CREATE TABLE IF NOT EXISTS score_marker_outcomes (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    ts                  TEXT    NOT NULL,
    underlying          TEXT    NOT NULL,
    side                TEXT    NOT NULL,   -- 'put_writing' | 'call_writing'
    score               INTEGER NOT NULL,
    amount_cr           REAL    NOT NULL,
    mode                TEXT    NOT NULL,   -- 'premium' | 'notional' | 'margin'
    atm_band            INTEGER NOT NULL,
    threshold_cr        REAL    NOT NULL,
    spot_at_marker      REAL    NOT NULL,
    spot_5min           REAL,
    spot_15min          REAL,
    spot_30min          REAL,
    return_5min_bps     REAL,
    return_15min_bps    REAL,
    return_30min_bps    REAL,
    UNIQUE(ts, underlying, side, mode, atm_band, threshold_cr)
);

CREATE INDEX IF NOT EXISTS idx_smo_underlying_ts ON score_marker_outcomes(underlying, ts);

-- Paper-trading ledger: every actionable dashboard recommendation is assumed
-- executed at PAPER_LOTS lots. Opened/marked/closed by paper.sync() on each
-- compute.py refresh. entry/exit/mark values are net cash PER UNIT with the
-- convention: SELL premium positive, BUY premium negative (credit > 0,
-- debit < 0). realized_pnl is in RUPEES for the full position.
CREATE TABLE IF NOT EXISTS paper_trades (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    strategy      TEXT    NOT NULL,
    direction     TEXT,
    opened_ts     TEXT    NOT NULL,
    entry_context TEXT,
    lots          INTEGER NOT NULL,
    lot_size      INTEGER NOT NULL,
    legs_json     TEXT    NOT NULL,
    entry_value   REAL    NOT NULL,
    expiry        TEXT,
    meta_json     TEXT,
    status        TEXT    NOT NULL DEFAULT 'open',
    closed_ts     TEXT,
    exit_reason   TEXT,
    exit_value    REAL,
    realized_pnl  REAL
);
CREATE INDEX IF NOT EXISTS idx_paper_strategy_status ON paper_trades(strategy, status);

-- NIFTY current-month futures, one row per minute (recorder + backfill).
-- volume is CUMULATIVE for the day (Kite convention); LLT detection diffs it.
CREATE TABLE IF NOT EXISTS futures_minute (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          TEXT    NOT NULL,
    symbol      TEXT    NOT NULL,
    ltp         REAL,
    volume      INTEGER,
    oi          INTEGER,
    UNIQUE(ts, symbol)
);
CREATE INDEX IF NOT EXISTS idx_fut_ts ON futures_minute(ts);

-- Large-lot trader (LLT) prints detected from futures_minute (llt.py).
-- Minute-resolution adaptation of the Vtrender-style order-flow read:
-- side from the minute's price direction (aggressor heuristic), OI quadrant
-- classification, confidence, closing-session flag, cross-session match.
CREATE TABLE IF NOT EXISTS llt_prints (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    ts             TEXT    NOT NULL,
    symbol         TEXT    NOT NULL,
    side           TEXT    NOT NULL,      -- BUY | SELL
    lots           INTEGER NOT NULL,
    price          REAL,
    oi_delta       INTEGER,               -- print-minute ΔOI (contracts qty)
    oi_delta_next  INTEGER,               -- ΔOI one minute later (~+60s check)
    classification TEXT,                  -- FRESH LONGS | SHORT COVERING | FRESH SHORTS | LONG UNWINDING | UNCLEAR
    confidence     TEXT,                  -- HIGH | MEDIUM
    closing_flag   INTEGER DEFAULT 0,     -- 1 if 15:00–15:30 IST
    matched_id     INTEGER,               -- possible same-player exit reference
    UNIQUE(ts, symbol)
);
CREATE INDEX IF NOT EXISTS idx_llt_ts ON llt_prints(ts);

CREATE TABLE IF NOT EXISTS paper_marks (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    trade_id   INTEGER NOT NULL,
    ts         TEXT    NOT NULL,
    mark_value REAL    NOT NULL,
    upnl       REAL    NOT NULL,
    spot       REAL,
    note       TEXT
);
CREATE INDEX IF NOT EXISTS idx_paper_marks_trade ON paper_marks(trade_id, ts);
"""


def init_db() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(str(DB_PATH)) as conn:
        conn.executescript(SCHEMA)
        # Migrations for existing DBs (CREATE TABLE IF NOT EXISTS won't add
        # columns). Idempotent: ALTER fails harmlessly if the column exists.
        for col, decl in (("entry_costs", "REAL"), ("exit_costs", "REAL"),
                          ("gross_pnl", "REAL")):
            try:
                conn.execute(f"ALTER TABLE paper_trades ADD COLUMN {col} {decl}")
            except sqlite3.OperationalError:
                pass  # column already present
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


def upsert_marker_outcomes(conn: sqlite3.Connection, rows: list) -> int:
    """Insert OR replace score marker outcomes. Each row has the full
    (ts, underlying, side, mode, atm_band, threshold_cr) key + all data
    fields. Forward-return fields may be NULL."""
    if not rows:
        return 0
    keys = (
        "ts", "underlying", "side", "score", "amount_cr",
        "mode", "atm_band", "threshold_cr",
        "spot_at_marker", "spot_5min", "spot_15min", "spot_30min",
        "return_5min_bps", "return_15min_bps", "return_30min_bps",
    )
    sql = (f"INSERT OR REPLACE INTO score_marker_outcomes ({','.join(keys)}) "
           f"VALUES ({','.join('?' for _ in keys)})")
    data = [tuple(r.get(k) for k in keys) for r in rows]
    cur = conn.executemany(sql, data)
    conn.commit()
    return cur.rowcount


def marker_outcomes_summary(conn: sqlite3.Connection) -> dict:
    """Aggregated stats for the analysis endpoint."""
    out: dict = {"by_score": {}, "by_side": {}, "totals": {}, "by_day": []}

    # Per (side, score) summary at each horizon
    for horizon in ("5min", "15min", "30min"):
        col = f"return_{horizon}_bps"
        cur = conn.execute(
            f"SELECT side, score, COUNT({col}) AS n, "
            f"AVG({col}) AS mean_bps, "
            f"SUM(CASE WHEN ((side='put_writing' AND {col}>0) OR (side='call_writing' AND {col}<0)) THEN 1 ELSE 0 END) AS hits "
            f"FROM score_marker_outcomes WHERE {col} IS NOT NULL "
            f"GROUP BY side, score ORDER BY side, score"
        )
        for r in cur.fetchall():
            key = f"{r['side']}_{r['score']}"
            entry = out["by_score"].setdefault(key, {
                "side": r["side"], "score": r["score"],
            })
            entry[f"n_{horizon}"] = r["n"]
            entry[f"mean_bps_{horizon}"] = r["mean_bps"]
            entry[f"hit_rate_{horizon}"] = (r["hits"] / r["n"]) if r["n"] else None

    # Per-side aggregated
    for horizon in ("5min", "15min", "30min"):
        col = f"return_{horizon}_bps"
        cur = conn.execute(
            f"SELECT side, COUNT({col}) AS n, AVG({col}) AS mean_bps, "
            f"SUM(CASE WHEN ((side='put_writing' AND {col}>0) OR (side='call_writing' AND {col}<0)) THEN 1 ELSE 0 END) AS hits "
            f"FROM score_marker_outcomes WHERE {col} IS NOT NULL GROUP BY side"
        )
        for r in cur.fetchall():
            entry = out["by_side"].setdefault(r["side"], {"side": r["side"]})
            entry[f"n_{horizon}"] = r["n"]
            entry[f"mean_bps_{horizon}"] = r["mean_bps"]
            entry[f"hit_rate_{horizon}"] = (r["hits"] / r["n"]) if r["n"] else None

    cur = conn.execute(
        "SELECT underlying, DATE(ts) AS day, COUNT(*) AS n_markers "
        "FROM score_marker_outcomes GROUP BY underlying, DATE(ts) ORDER BY day DESC"
    )
    out["by_day"] = [dict(r) for r in cur.fetchall()]

    cur = conn.execute("SELECT COUNT(*) AS n FROM score_marker_outcomes")
    out["totals"]["n_markers"] = cur.fetchone()["n"]
    return out


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
