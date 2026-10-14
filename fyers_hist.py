"""Expired F&O archive from Fyers — the backtest dataset this project lacked.

Every validation so far (WCash, the hold-rate lift, expected-move coverage,
the signal scorecard) ran on ~60 sessions of nearest-expiry ATM+-10 data from a
VIX 10-13 summer. Fyers serves EXPIRED NSE options and futures at 1-minute,
WITH open interest, back to 2022-01-03 — 4.7 years spanning real stress. This
module copies that into a separate SQLite archive, resumably and politely.

WHAT IT IS NOT: a live feed (expired contracts only — Kite still owns the
present) and not a replacement for oi_chain.db. It is a SEPARATE file,
DATA_DIR/fyers_hist.db, because a multi-gigabyte bulk import has no business
inside the database the live app serves under WAL while the recorder writes to
it every minute.

THE RATE LIMIT IS THE THING THAT CAN HURT US. Free tier: 10/s, 200/min,
100,000/day — and per the docs, exceeding the per-minute limit more than three
times in a day blocks the user for the rest of that day. So this runs at
150/min with a 4/s burst cap, persists the day's call count, and STOPS after
two breaches rather than ever risking the third.

SCHEMA is deliberately lean. Our live chain_snapshot measures 271 bytes/row
(text timestamps, spot repeated on every strike row, a five-text-column UNIQUE
index). Here: contracts are stored once, bars are (contract_id, epoch) integer
keyed WITHOUT ROWID.

CROSS-VALIDATION BEFORE TRUST. `validate()` compares Fyers' bars against the
minutes our own Kite recorder captured for the same contracts. Everything
downstream inherits this data's quality, and we happen to have an independent
check sitting in our own database — so it gets run before any backtest does.
"""
from __future__ import annotations

import json
import math
import os
import sqlite3
import sys
import threading
import time
from collections import deque
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import fyers_auth

API = fyers_auth.API
IST = fyers_auth.IST
DATA_DIR = fyers_auth.DATA_DIR
ARCHIVE = DATA_DIR / "fyers_hist.db"

AVAILABLE_FROM = {"NSE": date(2022, 1, 3), "BSE": date(2023, 8, 7)}
MAX_DAYS_INTRADAY = 100          # per historical-data request at 1..240 min
MAX_DAYS_DAILY = 366             # per expiry-dates / daily-history request

PER_SECOND = 4                   # docs allow 10
PER_MINUTE = 150                 # docs allow 200 — headroom is deliberate
PER_DAY = 90_000                 # docs allow 100,000
MAX_BREACHES = 2                 # >3 in a day = blocked; never reach 3

UNDERLYINGS = {
    "NIFTY":     {"fyers": "NSE:NIFTY50-INDEX",   "exch": "NSE"},
    "BANKNIFTY": {"fyers": "NSE:NIFTYBANK-INDEX", "exch": "NSE"},
    "SENSEX":    {"fyers": "BSE:SENSEX-INDEX",    "exch": "BSE"},
}
WEEKLY_WINDOW_DAYS = 35          # how far before expiry to fetch a weekly
MONTHLY_WINDOW_DAYS = 70         # monthlies are entered 20-40+ DTE (GG/NK/Panther)
DEFAULT_STRIKES = 10             # ATM+-N around EACH day's close in the window


# ------------------------------------------------------------------ schema --

SCHEMA = """
CREATE TABLE IF NOT EXISTS contract (
    id         INTEGER PRIMARY KEY,
    symbol     TEXT    NOT NULL UNIQUE,      -- NSE:NIFTY25MAR23000CE
    underlying TEXT    NOT NULL,             -- NIFTY
    expiry     TEXT    NOT NULL,             -- yyyy-mm-dd
    kind       TEXT    NOT NULL,             -- CE | PE | FUT
    strike     REAL                          -- NULL for FUT
);
CREATE INDEX IF NOT EXISTS idx_contract_ue ON contract(underlying, expiry);

-- volume is TRADED IN THE BAR (Fyers), not cumulative like chain_snapshot.
CREATE TABLE IF NOT EXISTS bar (
    contract_id INTEGER NOT NULL,
    ts          INTEGER NOT NULL,            -- epoch seconds, bar start
    o REAL, h REAL, l REAL, c REAL,
    v  INTEGER,
    oi INTEGER,
    PRIMARY KEY (contract_id, ts)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS index_bar (
    underlying TEXT    NOT NULL,
    res        TEXT    NOT NULL,             -- '1D' | '1'
    ts         INTEGER NOT NULL,
    o REAL, h REAL, l REAL, c REAL, v INTEGER,
    PRIMARY KEY (underlying, res, ts)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS planned_expiry (
    underlying TEXT NOT NULL,
    expiry     TEXT NOT NULL,
    monthly    INTEGER,
    n_options  INTEGER,                      -- listed by Fyers
    n_selected INTEGER,                      -- kept by the strike filter
    PRIMARY KEY (underlying, expiry)
);

CREATE TABLE IF NOT EXISTS crawl_task (
    task_id    TEXT PRIMARY KEY,             -- bar|<symbol>  or  idx|NIFTY|1|<from>
    kind       TEXT NOT NULL,                -- bar | idx
    symbol     TEXT NOT NULL,
    range_from TEXT NOT NULL,
    range_to   TEXT NOT NULL,
    priority   INTEGER NOT NULL,             -- higher first
    status     TEXT NOT NULL DEFAULT 'todo', -- todo|done|empty|error
    rows       INTEGER,
    attempts   INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    updated    TEXT
);
CREATE INDEX IF NOT EXISTS idx_task_q ON crawl_task(status, priority);

CREATE TABLE IF NOT EXISTS api_day (
    day      TEXT PRIMARY KEY,               -- IST date
    calls    INTEGER NOT NULL DEFAULT 0,
    breaches INTEGER NOT NULL DEFAULT 0
);
"""


def connect(path: Path | str = ARCHIVE) -> sqlite3.Connection:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=30, check_same_thread=False)
    conn.executescript(SCHEMA)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    return conn


# ------------------------------------------------------------- rate limit --

class RateLimiter:
    """Sliding-window limiter with a persisted daily count.

    `clock`/`sleep` are injectable so the limits can be tested without waiting
    real minutes.
    """

    def __init__(self, conn, per_second=PER_SECOND, per_minute=PER_MINUTE,
                 per_day=PER_DAY, clock=time.monotonic, sleep=time.sleep):
        self.conn, self.ps, self.pm, self.pd = conn, per_second, per_minute, per_day
        self.clock, self.sleep = clock, sleep
        self.sec, self.min = deque(), deque()

    def _day(self) -> str:
        return datetime.now(IST).date().isoformat()

    def day_counts(self) -> tuple[int, int]:
        r = self.conn.execute("SELECT calls, breaches FROM api_day WHERE day=?",
                              (self._day(),)).fetchone()
        return (r[0], r[1]) if r else (0, 0)

    def acquire(self) -> None:
        calls, breaches = self.day_counts()
        if calls >= self.pd:
            raise DailyBudgetSpent(f"{calls} calls today (cap {self.pd})")
        if breaches >= MAX_BREACHES:
            raise RateLimited(f"{breaches} rate-limit breaches today — stopping "
                              f"before Fyers' 3-breach day block")
        while True:
            now = self.clock()
            while self.sec and now - self.sec[0] >= 1.0:
                self.sec.popleft()
            while self.min and now - self.min[0] >= 60.0:
                self.min.popleft()
            if len(self.sec) < self.ps and len(self.min) < self.pm:
                break
            wait = (1.0 - (now - self.sec[0])) if len(self.sec) >= self.ps else \
                   (60.0 - (now - self.min[0]))
            self.sleep(max(wait, 0.01))
        now = self.clock()
        self.sec.append(now)
        self.min.append(now)
        self.conn.execute(
            "INSERT INTO api_day(day,calls,breaches) VALUES(?,1,0) "
            "ON CONFLICT(day) DO UPDATE SET calls=calls+1", (self._day(),))
        self.conn.commit()

    def breach(self) -> None:
        self.conn.execute(
            "INSERT INTO api_day(day,calls,breaches) VALUES(?,0,1) "
            "ON CONFLICT(day) DO UPDATE SET breaches=breaches+1", (self._day(),))
        self.conn.commit()


# ----------------------------------------------------------------- client --

class FyersError(Exception):
    pass


class AuthExpired(FyersError):
    """Token missing/expired — needs a browser login. Stops the crawl."""


class RateLimited(FyersError):
    pass


class DailyBudgetSpent(FyersError):
    pass


class NoData(FyersError):
    pass


_AUTH_CODES = {-8, -15, -16, -17}


class Client:
    """Thin authenticated GET with error classification.

    `http` is anything with requests' .get(url, params=, headers=, timeout=)
    returning an object with .status_code and .json() — injectable so the full
    crawl loop is testable without a token or the network.
    """

    def __init__(self, limiter: RateLimiter, http=None, header=None):
        import requests
        self.lim = limiter
        self.http = http or requests.Session()
        self._header = header

    def header(self) -> str:
        h = self._header or fyers_auth.auth_header()
        if not h:
            raise AuthExpired("no valid Fyers session — log in at /fyers/login")
        return h

    def get(self, path: str, params: dict) -> dict:
        # Header FIRST: a request refused for want of a login never leaves the
        # machine, so it must not consume a rate-limit slot or the day's quota.
        # (It did — a refused probe showed calls_today=1.)
        auth = self.header()
        self.lim.acquire()
        r = self.http.get(f"{API}{path}", params=params, timeout=30,
                          headers={"Authorization": auth})
        try:
            d = r.json()
        except Exception:
            d = {}
        code = d.get("code")
        if r.status_code == 429 or code == -429:
            self.lim.breach()
            raise RateLimited(d.get("message") or "HTTP 429")
        if r.status_code in (401, 403) or code in _AUTH_CODES:
            raise AuthExpired(d.get("message") or f"HTTP {r.status_code}")
        if d.get("s") == "no_data":
            raise NoData(d.get("message") or "no_data")
        if r.status_code != 200 or d.get("s") not in (None, "ok"):
            raise FyersError(f"HTTP {r.status_code} code={code} "
                             f"message={d.get('message')!r}")
        return d

    # --- the four endpoints we use ----------------------------------------
    def expiry_dates(self, symbol, frm: date, to: date) -> dict:
        d = self.get("/data/history/fno/expired/expiry-dates", {
            "symbol": symbol, "range_from": frm.isoformat(),
            "range_to": to.isoformat(), "date_format": 1})
        return (d.get("data") or {}).get("expiry_dates") or {}

    def contracts(self, symbol, expiry: str) -> dict:
        d = self.get("/data/history/fno/expired/underlying-symbols",
                     {"symbol": symbol, "expiry_date": expiry})
        data = d.get("data") or {}
        return {"root": data.get("symbol"),
                **(data.get("contracts") or {})}

    def expired_candles(self, symbol, frm: date, to: date, res="1") -> dict:
        return self.get("/data/history/fno/expired/historical-data", {
            "symbol": symbol, "resolution": res, "date_format": 1,
            "range_from": frm.isoformat(), "range_to": to.isoformat(),
            "include_oi": 1})

    def history(self, symbol, frm: date, to: date, res="1D") -> dict:
        return self.get("/data/history", {
            "symbol": symbol, "resolution": res, "date_format": 1,
            "range_from": frm.isoformat(), "range_to": to.isoformat(),
            "cont_flag": 1})


# ----------------------------------------------------------------- parsing --

def parse_symbol(symbol: str, root: str) -> Optional[dict]:
    """'NSE:NIFTY2510923500CE' -> {kind, strike}.

    The date code after the root is ALWAYS five characters — weekly YY+M+DD
    ('25109', M in 1-9/O/N/D) or monthly YY+MMM ('25MAR') — so with the root
    known the strike is simply what sits between it and the CE/PE suffix. A
    pure regex cannot split weekly '25109'+'23500' safely; a known root can.
    """
    try:
        body = symbol.split(":", 1)[1]
    except IndexError:
        return None
    if not root or not body.startswith(root):
        return None
    rest = body[len(root):]
    if rest.endswith("FUT") and len(rest) == 8:
        return {"kind": "FUT", "strike": None}
    if len(rest) < 8 or rest[-2:] not in ("CE", "PE"):
        return None
    try:
        strike = float(rest[5:-2])
    except ValueError:
        return None
    return {"kind": rest[-2:], "strike": strike}


def candles_to_rows(resp: dict) -> list[tuple]:
    """Map by the `columns` header rather than assuming position — the docs say
    open_interest is APPENDED only when include_oi=1."""
    cols = resp.get("columns") or ["timestamp", "open", "high", "low", "close",
                                   "volume", "open_interest"]
    ix = {c: i for i, c in enumerate(cols)}
    out = []
    for c in resp.get("candles") or []:
        g = lambda k: c[ix[k]] if k in ix and ix[k] < len(c) else None
        out.append((int(g("timestamp")), g("open"), g("high"), g("low"),
                    g("close"), g("volume"), g("open_interest")))
    return out


# --------------------------------------------------------------- planning --

def _chunks(frm: date, to: date, days: int):
    cur = frm
    while cur <= to:
        end = min(cur + timedelta(days=days - 1), to)
        yield cur, end
        cur = end + timedelta(days=1)


def _ist_date(ts: int) -> date:
    return datetime.fromtimestamp(int(ts), IST).date()


def ensure_index_daily(cli: Client, conn, underlying: str, frm: date, to: date) -> int:
    """Daily closes, used to decide which strikes were near the money."""
    sym = UNDERLYINGS[underlying]["fyers"]
    have = conn.execute("SELECT MIN(ts), MAX(ts) FROM index_bar WHERE underlying=? "
                        "AND res='1D'", (underlying,)).fetchone()
    n = 0
    for a, b in _chunks(frm, to, MAX_DAYS_DAILY):
        if have[0] and _ist_date(have[0]) <= a and _ist_date(have[1]) >= b - timedelta(days=4):
            continue
        try:
            d = cli.history(sym, a, b, "1D")
        except NoData:
            continue
        rows = [(underlying, "1D", int(c[0]), c[1], c[2], c[3], c[4],
                 c[5] if len(c) > 5 else None) for c in d.get("candles") or []]
        conn.executemany("INSERT OR IGNORE INTO index_bar VALUES (?,?,?,?,?,?,?,?)", rows)
        conn.commit()
        n += len(rows)
    return n


def _closes(conn, underlying: str, frm: date, to: date) -> list[float]:
    lo = int(datetime(frm.year, frm.month, frm.day, tzinfo=IST).timestamp())
    hi = int(datetime(to.year, to.month, to.day, 23, 59, tzinfo=IST).timestamp())
    return [r[0] for r in conn.execute(
        "SELECT c FROM index_bar WHERE underlying=? AND res='1D' AND ts BETWEEN ? AND ? "
        "ORDER BY ts", (underlying, lo, hi)) if r[0]]


def select_strikes(strikes: list[float], closes: list[float], n: int) -> set[float]:
    """Union of ATM+-n around EVERY close in the window — so a contract that was
    at the money on any day we might have traded it is kept, not just the ones
    near the money on expiry day."""
    s = sorted(set(strikes))
    keep: set[float] = set()
    for c in closes:
        i = min(range(len(s)), key=lambda k: abs(s[k] - c))
        keep.update(s[max(0, i - n): i + n + 1])
    return keep


def plan(cli: Client, conn, underlying="NIFTY", since: Optional[date] = None,
         until: Optional[date] = None, n_strikes=DEFAULT_STRIKES,
         index_minutes=True, log=print) -> dict:
    """Queue every contract worth fetching. Idempotent: an expiry already
    planned is skipped, so re-running after a token expiry costs nothing."""
    u = UNDERLYINGS[underlying]
    since = max(since or AVAILABLE_FROM[u["exch"]], AVAILABLE_FROM[u["exch"]])
    until = until or datetime.now(IST).date() - timedelta(days=1)
    ensure_index_daily(cli, conn, underlying,
                       since - timedelta(days=MONTHLY_WINDOW_DAYS), until)

    expiries, fut_exp = [], []
    for a, b in _chunks(since, until, MAX_DAYS_DAILY):
        try:
            e = cli.expiry_dates(u["fyers"], a, b)
        except NoData:
            continue
        expiries += e.get("options") or []
        fut_exp += e.get("futures") or []
    expiries = sorted(set(x for x in expiries if x <= until.isoformat()), reverse=True)
    fut_exp = set(fut_exp)
    # monthly = the last option expiry of its calendar month
    last_in_month: dict[str, str] = {}
    for x in expiries:
        last_in_month[x[:7]] = max(last_in_month.get(x[:7], x), x)

    done = {r[0] for r in conn.execute(
        "SELECT expiry FROM planned_expiry WHERE underlying=?", (underlying,))}
    queued = 0
    for exp in expiries:
        if exp in done:
            continue
        monthly = last_in_month.get(exp[:7]) == exp
        window = MONTHLY_WINDOW_DAYS if monthly else WEEKLY_WINDOW_DAYS
        e = date.fromisoformat(exp)
        frm = max(e - timedelta(days=window), since)
        try:
            cs = cli.contracts(u["fyers"], exp)
        except NoData:
            conn.execute("INSERT OR REPLACE INTO planned_expiry VALUES (?,?,?,?,?)",
                         (underlying, exp, int(monthly), 0, 0))
            conn.commit()
            continue
        root = cs.get("root") or underlying
        opts = []
        for s in cs.get("options") or []:
            p = parse_symbol(s, root)
            if p:
                opts.append((s, p))
        keep = select_strikes([p["strike"] for _, p in opts],
                              _closes(conn, underlying, frm, e), n_strikes) if opts else set()
        chosen = [(s, p) for s, p in opts if p["strike"] in keep]
        if exp in fut_exp:
            for s in cs.get("futures") or []:
                p = parse_symbol(s, root)
                if p:
                    chosen.append((s, p))
        prio = int(exp.replace("-", ""))
        for s, p in chosen:
            conn.execute("INSERT OR IGNORE INTO contract(symbol,underlying,expiry,kind,strike) "
                         "VALUES (?,?,?,?,?)", (s, underlying, exp, p["kind"], p["strike"]))
            conn.execute("INSERT OR IGNORE INTO crawl_task(task_id,kind,symbol,range_from,"
                         "range_to,priority) VALUES (?,?,?,?,?,?)",
                         (f"bar|{s}", "bar", s, frm.isoformat(), exp, prio))
        conn.execute("INSERT OR REPLACE INTO planned_expiry VALUES (?,?,?,?,?)",
                     (underlying, exp, int(monthly), len(opts), len(chosen)))
        conn.commit()
        queued += len(chosen)
        log(f"[plan] {underlying} {exp} {'M' if monthly else 'W'}: "
            f"{len(chosen)}/{len(opts)} contracts")

    if index_minutes:                       # spot at 1-min, for backtests + validation
        for a, b in _chunks(since, until, MAX_DAYS_INTRADAY):
            conn.execute("INSERT OR IGNORE INTO crawl_task(task_id,kind,symbol,range_from,"
                         "range_to,priority) VALUES (?,?,?,?,?,?)",
                         (f"idx|{underlying}|1|{a}", "idx", underlying,
                          a.isoformat(), b.isoformat(), 99999999))
        conn.commit()
    return {"expiries": len(expiries), "newly_planned_contracts": queued}


# ------------------------------------------------------------------- crawl --

def _contract_id(conn, symbol: str) -> Optional[int]:
    r = conn.execute("SELECT id FROM contract WHERE symbol=?", (symbol,)).fetchone()
    return r[0] if r else None


def run_task(cli: Client, conn, task: tuple) -> int:
    task_id, kind, symbol, frm, to = task
    a, b = date.fromisoformat(frm), date.fromisoformat(to)
    if kind == "idx":
        d = cli.history(UNDERLYINGS[symbol]["fyers"], a, b, "1")
        rows = [(symbol, "1", int(c[0]), c[1], c[2], c[3], c[4],
                 c[5] if len(c) > 5 else None) for c in d.get("candles") or []]
        conn.executemany("INSERT OR IGNORE INTO index_bar VALUES (?,?,?,?,?,?,?,?)", rows)
        return len(rows)
    cid = _contract_id(conn, symbol)
    if cid is None:
        raise FyersError("contract row missing")
    rows = candles_to_rows(cli.expired_candles(symbol, a, b, "1"))
    conn.executemany("INSERT OR IGNORE INTO bar VALUES (?,?,?,?,?,?,?,?)",
                     [(cid, *r) for r in rows])
    return len(rows)


def crawl(cli: Client, conn, max_calls: Optional[int] = None,
          stop: Optional[threading.Event] = None, log=print) -> dict:
    """Work the queue, highest priority first. Stops cleanly — never mid-write —
    on token expiry, rate-limit trouble, the daily budget, `max_calls`, or
    `stop`. Everything is resumable from the task table."""
    done = empty = failed = 0
    stopped_because = "queue empty"
    if not conn.execute("SELECT 1 FROM crawl_task LIMIT 1").fetchone():
        return {"done": 0, "empty": 0, "failed": 0,
                "stopped_because": "nothing planned yet — POST /fyers/plan first",
                **status(conn)}
    start_calls = cli.lim.day_counts()[0]
    while True:
        if stop is not None and stop.is_set():
            stopped_because = "stop requested"
            break
        if max_calls is not None and cli.lim.day_counts()[0] - start_calls >= max_calls:
            stopped_because = f"max_calls={max_calls} reached"
            break
        task = conn.execute(
            "SELECT task_id, kind, symbol, range_from, range_to FROM crawl_task "
            "WHERE status='todo' OR (status='error' AND attempts<3) "
            "ORDER BY priority DESC, task_id LIMIT 1").fetchone()
        if not task:
            break
        now = datetime.now(IST).isoformat(timespec="seconds")
        try:
            n = run_task(cli, conn, task)
            conn.execute("UPDATE crawl_task SET status='done', rows=?, attempts=attempts+1, "
                         "last_error=NULL, updated=? WHERE task_id=?", (n, now, task[0]))
            done += 1
        except NoData:
            conn.execute("UPDATE crawl_task SET status='empty', rows=0, attempts=attempts+1, "
                         "updated=? WHERE task_id=?", (now, task[0]))
            empty += 1
        except AuthExpired as e:
            stopped_because = f"login needed: {e}"
            break
        except (RateLimited, DailyBudgetSpent) as e:
            if isinstance(e, RateLimited) and cli.lim.day_counts()[1] < MAX_BREACHES:
                log(f"[crawl] rate limited — backing off 65s ({e})")
                time.sleep(65)
                continue
            stopped_because = f"{type(e).__name__}: {e}"
            break
        except Exception as e:
            conn.execute("UPDATE crawl_task SET status='error', attempts=attempts+1, "
                         "last_error=?, updated=? WHERE task_id=?",
                         (f"{type(e).__name__}: {e}"[:300], now, task[0]))
            failed += 1
        conn.commit()
        if (done + empty + failed) % 50 == 0:
            log(f"[crawl] {done} done · {empty} empty · {failed} failed")
    return {"done": done, "empty": empty, "failed": failed,
            "stopped_because": stopped_because, **status(conn)}


# ------------------------------------------------------------------ status --

def status(conn) -> dict:
    """Cheap by construction: every query here touches crawl_task (~23k rows),
    never `bar` (~180M).

    The first version ran COUNT(*) over `bar` plus an EXISTS lookup from each
    contract into crawl_task on an unindexed column. Both grew with the
    archive: at ~2 GB the endpoint passed a 20s client timeout and at 10 GB it
    took 100s — and a client giving up does not cancel the server-side scan.
    The bar count is already recorded per task, so it is summed from there.
    """
    by = dict(conn.execute("SELECT status, COUNT(*) FROM crawl_task GROUP BY status").fetchall())
    bars = conn.execute("SELECT COALESCE(SUM(rows),0) FROM crawl_task "
                        "WHERE kind='bar' AND status='done'").fetchone()[0]
    calls, breaches = conn.execute(
        "SELECT calls, breaches FROM api_day WHERE day=?",
        (datetime.now(IST).date().isoformat(),)).fetchone() or (0, 0)
    # a bar task's range_to IS its contract's expiry
    rng = conn.execute("SELECT MIN(range_to), MAX(range_to) FROM crawl_task "
                       "WHERE kind='bar' AND status='done'").fetchone()
    size = sum(p.stat().st_size for p in (ARCHIVE, Path(str(ARCHIVE) + "-wal"))
               if p.exists())
    return {"tasks": by, "bars": bars,
            "contracts": conn.execute("SELECT COUNT(*) FROM contract").fetchone()[0],
            "expiries_planned": conn.execute("SELECT COUNT(*) FROM planned_expiry").fetchone()[0],
            "done_expiry_range": list(rng) if rng and rng[0] else None,
            "calls_today": calls, "breaches_today": breaches,
            "db_mb": round(size / 1e6, 1),
            "bytes_per_bar": round(size / bars) if bars else None}


# --------------------------------------------------------------- validate --

def validate(conn, kite_db: Path | str, underlying="NIFTY", day: Optional[str] = None,
             max_contracts: int = 200) -> dict:
    """Fyers bars vs the minutes OUR Kite recorder captured, same contracts.

    Two alignments are tested rather than assumed: our chain_snapshot row at
    minute T may correspond to the Fyers bar STARTING at T or the one ending at
    T, and the live recorder and our historical backfill may differ on which.
    Whichever aligns better is the one reported.

    Price: our ltp vs the Fyers close. OI: compared directly — both brokers
    report option OI in UNITS (the 75x lot-size mistake from #73 is the one to
    not repeat here).
    """
    k = sqlite3.connect(f"file:{kite_db}?mode=ro", uri=True)
    where, args = "underlying=?", [underlying]
    if day:
        where += " AND ts>=? AND ts<?"
        args += [day, day + "~"]
    ours = k.execute(f"SELECT DISTINCT expiry, strike, opt_type FROM chain_snapshot "
                     f"WHERE {where} LIMIT ?", (*args, max_contracts * 4)).fetchall()
    stats = {0: [], -60: []}
    oi_stats = {0: [], -60: []}
    matched_contracts = 0
    for exp, strike, ot in ours:
        r = conn.execute("SELECT id FROM contract WHERE underlying=? AND expiry=? "
                         "AND kind=? AND strike=?", (underlying, exp[:10], ot, float(strike))
                         ).fetchone()
        if not r:
            continue
        fy = {ts: (c, oi) for ts, c, oi in conn.execute(
            "SELECT ts, c, oi FROM bar WHERE contract_id=?", (r[0],))}
        if not fy:
            continue
        matched_contracts += 1
        q = (f"SELECT ts, ltp, oi FROM chain_snapshot WHERE {where} AND expiry=? "
             f"AND strike=? AND opt_type=?")
        for ts, ltp, oi in k.execute(q, (*args, exp, strike, ot)):
            try:
                ep = int(datetime.fromisoformat(ts).timestamp())
            except Exception:
                continue
            for off in (0, -60):
                hit = fy.get(ep + off)
                if hit is None or ltp is None:
                    continue
                c, foi = hit
                if c is not None:
                    stats[off].append(abs(c - ltp) / max(ltp, 0.05))
                if oi and foi is not None:
                    oi_stats[off].append(abs(foi - oi) / max(oi, 1))
        if matched_contracts >= max_contracts:
            break

    def summ(xs, tol):
        if not xs:
            return {"n": 0}
        xs = sorted(xs)
        return {"n": len(xs), "median_rel_err": round(xs[len(xs) // 2], 5),
                "within_tol_pct": round(sum(1 for x in xs if x <= tol) / len(xs) * 100, 1)}

    res = {off: {"price": summ(stats[off], 0.01), "oi": summ(oi_stats[off], 0.001)}
           for off in (0, -60)}
    best = max(res, key=lambda o: (res[o]["price"].get("within_tol_pct", 0),
                                   res[o]["price"].get("n", 0)))
    return {"ok": matched_contracts > 0, "underlying": underlying, "day": day,
            "matched_contracts": matched_contracts,
            "alignment": "fyers bar start = our minute" if best == 0
                         else "fyers bar start = our minute − 1",
            "best": res[best], "both": {str(o): v for o, v in res.items()},
            "tolerances": "price within 1% of our ltp; OI within 0.1%"}


# ------------------------------------------------------------------ probe --

def probe(cli: Client, conn, underlying="NIFTY") -> dict:
    """Spend ~6 calls to confirm the API behaves as documented BEFORE a crawl
    is built on it: real expiries, real symbols, 1-min candles, OI column, and
    whether a range wider than a contract's listed life errors or truncates."""
    out = {"underlying": underlying}
    u = UNDERLYINGS[underlying]
    today = datetime.now(IST).date()
    e = cli.expiry_dates(u["fyers"], today - timedelta(days=45), today - timedelta(days=1))
    opts = sorted(e.get("options") or [])
    out["recent_expiries"] = opts[-5:]
    if not opts:
        out["verdict"] = "no expired option expiries returned"
        return out
    exp = opts[-1]
    cs = cli.contracts(u["fyers"], exp)
    root = cs.get("root")
    parsed = [(s, parse_symbol(s, root)) for s in cs.get("options") or []]
    good = [(s, p) for s, p in parsed if p]
    out.update(root=root, n_listed=len(parsed), n_parsed=len(good),
               sample_symbols=[s for s, _ in good[:3]])
    ensure_index_daily(cli, conn, underlying, date.fromisoformat(exp) - timedelta(days=10),
                       date.fromisoformat(exp))
    closes = _closes(conn, underlying, date.fromisoformat(exp) - timedelta(days=5),
                     date.fromisoformat(exp))
    if not good or not closes:
        out["verdict"] = "could not parse symbols or fetch index closes"
        return out
    atm = min(good, key=lambda sp: abs(sp[1]["strike"] - closes[-1]) +
              (0 if sp[1]["kind"] == "CE" else 0.1))
    sym = atm[0]
    e_date = date.fromisoformat(exp)
    short = cli.expired_candles(sym, e_date - timedelta(days=3), e_date)
    rows = candles_to_rows(short)
    out["atm_contract"] = sym
    out["columns"] = short.get("columns")
    out["has_open_interest"] = "open_interest" in (short.get("columns") or [])
    out["candles_last_3d"] = len(rows)
    if rows:
        f = rows[-1]
        out["last_candle"] = {"ist": datetime.fromtimestamp(f[0], IST).isoformat(),
                              "close": f[4], "volume": f[5], "oi": f[6]}
    try:
        wide = candles_to_rows(cli.expired_candles(sym, e_date - timedelta(days=60), e_date))
        out["wide_range_60d"] = {"ok": True, "candles": len(wide),
                                 "first": datetime.fromtimestamp(wide[0][0], IST).isoformat()
                                 if wide else None}
    except FyersError as err:
        out["wide_range_60d"] = {"ok": False, "error": str(err)}
    out["verdict"] = ("OK — 1-min candles with open interest"
                      if out["has_open_interest"] and rows else "PROBLEM — see fields")
    return out


# -------------------------------------------------------------------- CLI --

def _cli():
    import argparse
    ap = argparse.ArgumentParser(description="Fyers expired-F&O archive")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("probe")
    p = sub.add_parser("plan")
    p.add_argument("--underlying", default="NIFTY", choices=sorted(UNDERLYINGS))
    p.add_argument("--strikes", type=int, default=DEFAULT_STRIKES)
    p.add_argument("--since")
    c = sub.add_parser("crawl")
    c.add_argument("--max-calls", type=int)
    sub.add_parser("status")
    v = sub.add_parser("validate")
    v.add_argument("--day")
    a = ap.parse_args()
    conn = connect()
    cli = Client(RateLimiter(conn))
    if a.cmd == "probe":
        print(json.dumps(probe(cli, conn), indent=2, default=str))
    elif a.cmd == "plan":
        print(plan(cli, conn, a.underlying, n_strikes=a.strikes,
                   since=date.fromisoformat(a.since) if a.since else None))
    elif a.cmd == "crawl":
        print(json.dumps(crawl(cli, conn, max_calls=a.max_calls), indent=2))
    elif a.cmd == "status":
        print(json.dumps(status(conn), indent=2))
    elif a.cmd == "validate":
        import db
        print(json.dumps(validate(conn, db.DB_PATH, day=a.day), indent=2))


if __name__ == "__main__":
    _cli()
