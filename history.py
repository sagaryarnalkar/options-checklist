"""Replay our signals over the Fyers archive (2022 -> now) through the
PRODUCTION code paths — the backtest of everything we built that runs on
intraday option and futures data.

HOW. Each historical session is rebuilt into an in-memory, oi_chain-shaped
database in exactly the live recorder's format (rebuild.build -> to_live, the
converter #90 proved reproduces live rows), and the UNCHANGED production
modules run on it:

    recorder._refresh_marker_outcomes   OI-flow score markers + forward returns
    hold.run_session                    OI-build follow-through + its control
    llt.detect_day                      large-lot futures prints (NIFTY)
    compute.wcash_read                  writer stress + bull/bear tilt
    compute.premium_ladder              skew / straddle axes
    pulse.expected_move, pulse.max_pain

Per-event outputs land in data/history.db in the SAME table shapes as
oi_chain.db, so review.py grades history with no special casing. Day-level
reads go to `hist_day`, taken twice: at the close (the coincident claims) and
from a copy of the session cut at 11:00 (the "is it predictive?" claims — the
11:00 checkpoint is the one every coincident-only verdict in CLAUDE.md used).

WHY A SEPARATE PROCESS. A full replay is hours of CPU. Inside the web worker
it would contend with the live recorder, so the app spawns
`python3 history.py run ...` detached at low priority and reads a status file.

HONEST LIMITS, also printed with the results:
  * Strikes: the archive holds ATM+-10 around each daily close — the live
    recorder's own footprint, so signals are like-for-like. Max pain over a
    truncated band is approximate; band-edge readings are excluded.
  * LLT's 750-lot floor is held fixed in UNITS (750 x 65): NIFTY's lot size
    changed several times since 2022 and Fyers does not report it.
  * Expiry rules changed (NIFTY weekly Thu -> Tue, BANKNIFTY weeklies ended
    Nov 2024), so every verdict is also reported per year, not only pooled.
  * Minute bars, not ticks: the same resolution the live recorder samples.
"""
from __future__ import annotations

import json
import math
import os
import sqlite3
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import db
import rebuild

IST = timezone(timedelta(hours=5, minutes=30))
HIST_DB = db.DATA_DIR / "history.db"
STATUS_FILE = db.DATA_DIR / "history_status.json"
STOP_FILE = db.DATA_DIR / "history_stop"
LOG_FILE = db.DATA_DIR / "history_run.log"
LLT_LOT_UNITS = 65          # 750 lots x 65 = the floor in units, held fixed
CHECKPOINT = "11:00"        # the "stand here and look forward" minute

# the per-event tables the graders read; copied day by day out of the replay
EVENT_TABLES = ("score_marker_outcomes", "oi_builds", "hold_baseline", "llt_prints",
                "futures_minute", "underlying_candle")

HIST_EXTRA = """
CREATE TABLE IF NOT EXISTS hist_day (
    underlying TEXT NOT NULL, d TEXT NOT NULL, expiry TEXT, dte INTEGER,
    open REAL, high REAL, low REAL, close REAL, move_pct REAL, range_pct REAL,
    -- at the close (coincident claims)
    wcash_cr REAL, wcash_ce REAL, wcash_pe REAL, wcash_tilt REAL,
    ladder_skew REAL, ladder_straddle_pct REAL,
    em_spot REAL, em_iv REAL, em_one_sigma REAL, em_dte REAL,
    mp_strike REAL, mp_edge INTEGER,
    -- at the 11:00 checkpoint (predictive claims) and what followed
    cp_spot REAL, cp_wcash_cr REAL, cp_tilt REAL, cp_skew REAL,
    cp_mp_strike REAL, cp_mp_edge INTEGER,
    pm_move_pct REAL, pm_range_pct REAL,
    markers INTEGER, builds INTEGER, llt INTEGER, band_short INTEGER,
    PRIMARY KEY (underlying, d)
);
CREATE TABLE IF NOT EXISTS hist_run (
    underlying TEXT NOT NULL, d TEXT NOT NULL, ok INTEGER, error TEXT,
    secs REAL, ts TEXT, PRIMARY KEY (underlying, d)
);
"""


def connect_hist(path: Optional[str] = None) -> sqlite3.Connection:
    p = Path(path) if path else HIST_DB
    p.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(str(p), timeout=30)
    c.row_factory = sqlite3.Row
    c.executescript(db.SCHEMA)
    c.executescript(HIST_EXTRA)
    c.execute("PRAGMA journal_mode=WAL;")
    return c


def _mem() -> sqlite3.Connection:
    m = sqlite3.connect(":memory:")
    m.row_factory = sqlite3.Row
    m.executescript(db.SCHEMA)
    return m


# ------------------------------------------------------------ day databases --

def nearest_expiry(arch, underlying: str, day: date) -> Optional[str]:
    """The recorder's rule (nearest option expiry on/after the day), applied
    to the archive's expired contracts."""
    r = arch.execute("SELECT MIN(expiry) FROM contract WHERE underlying=? "
                     "AND kind IN ('CE','PE') AND expiry >= ?",
                     (underlying, day.isoformat())).fetchone()
    return r[0] if r and r[0] else None


def _load_build(mem, b: dict) -> None:
    mem.executemany(
        "INSERT OR IGNORE INTO chain_snapshot (ts, underlying, spot, expiry, strike, "
        "opt_type, ltp, volume, oi) VALUES (?,?,?,?,?,?,?,?,?)",
        [(r["ts"], r["underlying"], r["spot"], r["expiry"], r["strike"], r["opt_type"],
          r["ltp"], r["volume"], r["oi"]) for rs in b["chain"].values() for r in rs])
    mem.executemany(
        "INSERT OR IGNORE INTO underlying_candle (ts, underlying, open, high, low, close, "
        "volume) VALUES (?,?,?,?,?,?,?)",
        [(c["ts"], c["underlying"], c["open"], c["high"], c["low"], c["close"], c["volume"])
         for c in b["candles"]])
    f = b.get("futures")
    if f:
        mem.executemany("INSERT OR IGNORE INTO futures_minute (ts, symbol, ltp, volume, oi) "
                        "VALUES (?,?,?,?,?)",
                        [(m, f["symbol"], v[0], v[1], v[2]) for m, v in f["rows"].items()])


class ArchiveDays:
    """Builds each session from the archive once. hold.py judges a build
    against the PRIOR session's close, so the previous build is kept."""

    def __init__(self, arch, underlying: str):
        self.arch, self.u, self._cache = arch, underlying, {}

    def get(self, day: date) -> dict:
        if day not in self._cache:
            exp = nearest_expiry(self.arch, self.u, day)
            b = (rebuild.build(self.arch, self.u, day, exp) if exp
                 else {"ok": False, "reason": f"no archived expiry on/after {day}"})
            self._cache = {k: v for k, v in self._cache.items() if k > day - timedelta(days=10)}
            self._cache[day] = b
        return self._cache[day]

    def day_db(self, day: date, prev: Optional[date]):
        b = self.get(day)
        if not b.get("ok"):
            return None, b.get("reason")
        mem = _mem()
        if prev:
            pb = self.get(prev)
            if pb.get("ok"):
                _load_build(mem, pb)
        _load_build(mem, b)
        mem.commit()
        return mem, {"expiry": b["expiry"], "band_short": b.get("band_short_minutes")}


def live_day_db(live, underlying: str, day: date, prev: Optional[date]):
    """The same shape from the REAL recorded rows — the control the replay is
    checked against before it is trusted over four years."""
    mem = _mem()
    for d in [x for x in (prev, day) if x]:
        a, z = d.isoformat(), d.isoformat() + "~"
        for table, key in (("chain_snapshot", "underlying"), ("underlying_candle", "underlying")):
            cur = live.execute(f"SELECT * FROM {table} WHERE {key}=? AND ts>=? AND ts<?",
                               (underlying, a, z))
            cols = [c[0] for c in cur.description if c[0] != "id"]
            rows = [tuple(r[c] for c in cols) for r in cur]
            mem.executemany(f"INSERT OR IGNORE INTO {table} ({','.join(cols)}) VALUES "
                            f"({','.join('?' * len(cols))})", rows)
        if underlying == "NIFTY":
            try:
                cur = live.execute("SELECT ts, symbol, ltp, volume, oi FROM futures_minute "
                                   "WHERE ts>=? AND ts<?", (a, z))
            except sqlite3.OperationalError:          # a pre-#53 database
                continue
            mem.executemany("INSERT OR IGNORE INTO futures_minute (ts, symbol, ltp, volume, oi) "
                            "VALUES (?,?,?,?,?)", [tuple(r) for r in cur])
    mem.commit()
    exp = mem.execute("SELECT expiry FROM chain_snapshot WHERE underlying=? AND ts>=? "
                      "GROUP BY expiry ORDER BY COUNT(*) DESC LIMIT 1",
                      (underlying, day.isoformat())).fetchone()
    return mem, {"expiry": exp[0] if exp else None, "band_short": None}


# ------------------------------------------------------------ one session --

def _copy(src, dst, table: str, where: str, args: tuple) -> int:
    cur = src.execute(f"SELECT * FROM {table} WHERE {where}", args)
    cols = [c[0] for c in cur.description]
    keep = [i for i, c in enumerate(cols) if c != "id"]      # let dst assign ids
    rows = [tuple(r[i] for i in keep) for r in cur]
    if rows:
        names = [cols[i] for i in keep]
        dst.executemany(f"INSERT OR IGNORE INTO {table} ({','.join(names)}) "
                        f"VALUES ({','.join('?' * len(names))})", rows)
    return len(rows)


def _candles(mem, underlying: str, d: str, after: Optional[str] = None):
    lo = f"{d}T{after}" if after else d
    return mem.execute("SELECT ts, open, high, low, close FROM underlying_candle "
                       "WHERE underlying=? AND ts>=? AND ts<? ORDER BY ts",
                       (underlying, lo, d + "~")).fetchall()


def _reads(mem, underlying: str, d: str) -> dict:
    """WCash, ladder, expected move and max pain on whatever `mem` holds for
    the day — called once on the full session and once on the 11:00 cut."""
    import compute
    import pulse
    out = {}
    w = compute.wcash_read(mem, underlying, d)
    if w.get("ok"):
        out.update(wcash_cr=w["close_cr"], wcash_ce=w["ce_cr"], wcash_pe=w["pe_cr"],
                   wcash_tilt=w["tilt_pct"])
    lad = compute.premium_ladder(mem, underlying, d)
    if lad.get("ok"):
        out.update(ladder_skew=lad["skew_pct"], ladder_straddle_pct=lad["straddle_pct"])
    em = pulse.expected_move(mem, underlying, d)
    if em.get("ok"):
        out.update(em_spot=em["spot"], em_iv=em["iv"], em_one_sigma=em["one_sigma"],
                   em_dte=em["dte"])
    mp = pulse.max_pain(mem, underlying, d)
    if mp.get("ok"):
        out.update(mp_strike=mp["max_pain"], mp_edge=int(bool(mp["on_band_edge"])))
    return out


def process_mem(mem, hist, underlying: str, day: date, meta: dict) -> dict:
    """Run every production module on one session's database and keep the
    results. Each module fails soft: one broken read must not lose the day."""
    import hold
    import llt
    import recorder
    d = day.isoformat()
    lo, hi = d, d + "~"
    n = {"markers": 0, "builds": 0, "llt": 0}
    errors = []
    try:
        recorder._refresh_marker_outcomes(mem, underlying, d)
        n["markers"] = _copy(mem, hist, "score_marker_outcomes",
                             "underlying=? AND ts>=? AND ts<?", (underlying, lo, hi))
    except Exception as e:
        errors.append(f"markers {type(e).__name__}: {e}")
    try:
        hold.run_session(mem, underlying, d)
        n["builds"] = _copy(mem, hist, "oi_builds", "underlying=? AND ts>=? AND ts<?",
                            (underlying, lo, hi))
        _copy(mem, hist, "hold_baseline", "underlying=? AND d=?", (underlying, d))
    except Exception as e:
        errors.append(f"holds {type(e).__name__}: {e}")
    if underlying == "NIFTY":
        try:
            if mem.execute("SELECT 1 FROM futures_minute WHERE ts>=? AND ts<? LIMIT 1",
                           (lo, hi)).fetchone():
                llt.detect_day(mem, d, LLT_LOT_UNITS)
                n["llt"] = _copy(mem, hist, "llt_prints", "ts>=? AND ts<?", (lo, hi))
                _copy(mem, hist, "futures_minute", "ts>=? AND ts<?", (lo, hi))
        except Exception as e:
            errors.append(f"llt {type(e).__name__}: {e}")
    _copy(mem, hist, "underlying_candle", "underlying=? AND ts>=? AND ts<?",
          (underlying, lo, hi))

    row = {"expiry": meta.get("expiry"), "band_short": meta.get("band_short"), **n}
    if row["expiry"]:
        row["dte"] = (date.fromisoformat(row["expiry"][:10]) - day).days
    c = _candles(mem, underlying, d)
    if c:
        o, cl = c[0]["open"], c[-1]["close"]
        h, l = max(x["high"] for x in c), min(x["low"] for x in c)
        row.update(open=o, high=h, low=l, close=cl,
                   move_pct=round((cl / o - 1) * 100, 4) if o else None,
                   range_pct=round((h - l) / o * 100, 4) if o else None)
    row.update(_reads(mem, underlying, d))

    # the 11:00 cut: same reads on the session as it stood at the checkpoint
    pm = _candles(mem, underlying, d, CHECKPOINT)
    if pm and c:
        mem.execute("DELETE FROM chain_snapshot WHERE underlying=? AND ts>? AND ts<?",
                    (underlying, f"{d}T{CHECKPOINT}:59", hi))
        cp = _reads(mem, underlying, d)
        s0 = pm[0]["open"]
        row.update(cp_spot=s0, cp_wcash_cr=cp.get("wcash_cr"), cp_tilt=cp.get("wcash_tilt"),
                   cp_skew=cp.get("ladder_skew"), cp_mp_strike=cp.get("mp_strike"),
                   cp_mp_edge=cp.get("mp_edge"),
                   pm_move_pct=round((c[-1]["close"] / s0 - 1) * 100, 4) if s0 else None,
                   pm_range_pct=round((max(x["high"] for x in pm) - min(x["low"] for x in pm))
                                      / s0 * 100, 4) if s0 else None)
    cols = ["underlying", "d"] + list(row)
    hist.execute(f"INSERT OR REPLACE INTO hist_day ({','.join(cols)}) "
                 f"VALUES ({','.join('?' * len(cols))})", (underlying, d, *row.values()))
    hist.commit()
    return {"ok": True, "errors": errors, **row}


def clear_day(hist, underlying: str, d: str) -> None:
    """Make a redo idempotent: the event tables are INSERT OR IGNORE on keys
    that may not include every field, so a rerun deletes the day first."""
    lo, hi = d, d + "~"
    hist.execute("DELETE FROM score_marker_outcomes WHERE underlying=? AND ts>=? AND ts<?",
                 (underlying, lo, hi))
    hist.execute("DELETE FROM oi_builds WHERE underlying=? AND ts>=? AND ts<?",
                 (underlying, lo, hi))
    hist.execute("DELETE FROM hold_baseline WHERE underlying=? AND d=?", (underlying, d))
    hist.execute("DELETE FROM underlying_candle WHERE underlying=? AND ts>=? AND ts<?",
                 (underlying, lo, hi))
    if underlying == "NIFTY":
        hist.execute("DELETE FROM llt_prints WHERE ts>=? AND ts<?", (lo, hi))
        hist.execute("DELETE FROM futures_minute WHERE ts>=? AND ts<?", (lo, hi))


# -------------------------------------------------------------------- run --

def _status(pid: Optional[int] = None, **kw) -> None:
    try:
        STATUS_FILE.write_text(json.dumps({"ts": datetime.now(IST).isoformat(timespec="seconds"),
                                           "pid": pid or os.getpid(), **kw}))
    except Exception:
        pass


def mark_starting(pid: int, args: list) -> None:
    """Written by the app the moment it spawns a replay, so a second request
    in the startup window is refused instead of launching a duplicate."""
    _status(pid=pid, running=True, progress="starting", args=args)


def read_status() -> dict:
    try:
        s = json.loads(STATUS_FILE.read_text())
    except Exception:
        return {"running": False, "reason": "never run"}
    if s.get("running"):
        try:
            os.kill(int(s["pid"]), 0)
        except Exception:
            s["running"], s["note"] = False, "process gone (killed or crashed mid-run)"
    return s


def archive_days(arch, underlying: str, since: date, until: date) -> list:
    """Sessions with archived index minutes — rebuild.trading_days, minus the
    live-database union (history only ever replays the archive)."""
    return rebuild.trading_days(_mem(), arch, underlying, since, until)


def run(underlying: str = "NIFTY", since: Optional[str] = None, until: Optional[str] = None,
        arch_path: Optional[str] = None, hist_path: Optional[str] = None,
        redo: bool = False) -> dict:
    """Replay every archived session. Resumable: finished sessions are skipped
    unless redo, so a stop or a crash costs nothing already done."""
    import fyers_hist
    st = read_status()
    if st.get("running") and int(st.get("pid") or 0) != os.getpid():
        # two replays would both clear and rewrite the same sessions
        return {"ok": False, "reason": f"another replay is running (pid {st.get('pid')})"}
    try:
        os.nice(10)                          # yield the CPU to the live app
    except Exception:
        pass
    try:
        STOP_FILE.unlink()
    except Exception:
        pass
    arch = fyers_hist.connect(arch_path) if arch_path else fyers_hist.connect()
    hist = connect_hist(hist_path)
    s = date.fromisoformat(since) if since else date(2022, 1, 1)
    u = date.fromisoformat(until) if until else datetime.now(IST).date() - timedelta(days=1)
    days = archive_days(arch, underlying, s, u)
    done = set() if redo else {r[0] for r in hist.execute(
        "SELECT d FROM hist_run WHERE underlying=? AND ok=1", (underlying,))}
    src = ArchiveDays(arch, underlying)
    t0, n_ok, n_fail, prev, stopped = time.time(), 0, 0, None, False
    _status(running=True, underlying=underlying, progress=f"0/{len(days)}", ok=0, failed=0)
    for i, day in enumerate(days):
        if STOP_FILE.exists():
            stopped = True
            break
        if day.isoformat() in done:
            prev = day
            continue
        t = time.time()
        err = None
        try:
            mem, meta = src.day_db(day, prev)
            if mem is None:
                ok, err = False, meta
            else:
                clear_day(hist, underlying, day.isoformat())
                r = process_mem(mem, hist, underlying, day, meta)
                mem.close()
                ok = True
                if r["errors"]:
                    err = "; ".join(r["errors"])[:500]
        except Exception as e:
            ok, err = False, f"{type(e).__name__}: {e}"
        hist.execute("INSERT OR REPLACE INTO hist_run VALUES (?,?,?,?,?,?)",
                     (underlying, day.isoformat(), int(ok), err, round(time.time() - t, 2),
                      datetime.now(IST).isoformat(timespec="seconds")))
        hist.commit()
        n_ok += ok
        n_fail += (not ok)
        prev = day
        if (n_ok + n_fail) % 5 == 1 or i == len(days) - 1:
            el = time.time() - t0
            per = el / max(n_ok + n_fail, 1)
            left = sum(1 for x in days[i + 1:] if x.isoformat() not in done)
            _status(running=True, underlying=underlying, day=day.isoformat(),
                    progress=f"{i + 1}/{len(days)}", ok=n_ok, failed=n_fail,
                    elapsed_min=round(el / 60, 1), eta_min=round(left * per / 60, 1))
    res = {"underlying": underlying, "days": len(days), "ok": n_ok, "failed": n_fail,
           "skipped_done": len(done), "stopped": stopped,
           "elapsed_min": round((time.time() - t0) / 60, 1)}
    if not stopped and n_ok:
        _status(running=True, underlying=underlying, progress="grading", ok=n_ok,
                failed=n_fail)
        try:
            save_scorecard(underlying, hist_path)
            res["scorecard"] = str(scorecard_path(underlying))
        except Exception as e:
            res["scorecard_error"] = f"{type(e).__name__}: {e}"
    _status(running=False, **res)
    return res


# -------------------------------------------------------------- scorecard --

def _corr(xs, ys):
    p = [(x, y) for x, y in zip(xs, ys) if x is not None and y is not None]
    if len(p) < 10:
        return None
    mx, my = sum(x for x, _ in p) / len(p), sum(y for _, y in p) / len(p)
    sx = math.sqrt(sum((x - mx) ** 2 for x, _ in p))
    sy = math.sqrt(sum((y - my) ** 2 for _, y in p))
    return round(sum((x - mx) * (y - my) for x, y in p) / (sx * sy), 3) if sx and sy else None


def _rank(v):
    order = sorted(range(len(v)), key=lambda i: v[i])
    r = [0.0] * len(v)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and v[order[j + 1]] == v[order[i]]:
            j += 1
        for k in range(i, j + 1):
            r[order[k]] = (i + j) / 2
        i = j + 1
    return r


def _spearman(xs, ys):
    p = [(x, y) for x, y in zip(xs, ys) if x is not None and y is not None]
    if len(p) < 10:
        return None
    return _corr(_rank([x for x, _ in p]), _rank([y for _, y in p]))


def _sign_agree(xs, ys):
    """How often the signs agree, against what INDEPENDENT signs with the
    same up/down shares would give — not against 50%. If tilt leans bullish
    on most days and afternoons drift up, a coin-flip baseline credits the
    indicator with the drift."""
    p = [(x, y) for x, y in zip(xs, ys) if x and y]
    if len(p) < 10:
        return None
    n = len(p)
    k = sum(1 for x, y in p if (x > 0) == (y > 0))
    px = sum(1 for x, _ in p if x > 0) / n
    py = sum(1 for _, y in p if y > 0) / n
    e = px * py + (1 - px) * (1 - py)
    return {"n": n, "agree": k, "pct": round(k / n * 100, 1),
            "chance_pct": round(e * 100, 1),
            "z": round((k - n * e) / math.sqrt(n * e * (1 - e)), 2) if 0 < e < 1 else None}


def _partial(xs, ys, zs):
    """corr(x, y) holding z fixed — e.g. WCash@11 vs the afternoon range,
    controlling for how active the morning already was (vol clusters, so the
    morning alone predicts the afternoon)."""
    t = [(x, y, z) for x, y, z in zip(xs, ys, zs) if None not in (x, y, z)]
    if len(t) < 20:
        return None
    a, b, c = zip(*t)
    rxy, rxz, ryz = _corr(a, b), _corr(a, c), _corr(b, c)
    if None in (rxy, rxz, ryz) or rxz ** 2 >= 1 or ryz ** 2 >= 1:
        return None
    return round((rxy - rxz * ryz) / math.sqrt((1 - rxz ** 2) * (1 - ryz ** 2)), 3)


def _magnet(events: list) -> dict:
    """Did price move TOWARD max pain? events = (spot_then, max_pain, later).

    The obvious test — "did it end nearer max pain than it started?" — has no
    50% chance level: when max pain sits close to spot, almost any move
    overshoots it, so even a random level 'fails' most of the time (the first
    version of this grader reported a z of -17 from exactly that). Direction
    has a fair baseline: independent signs with the same shares of 'max pain
    above spot' and 'price rose'."""
    ev = [(s, m, c) for s, m, c in events if abs(m - s) >= 1 and c != s]
    if not ev:
        return {"n": 0}
    n = len(ev)
    k = sum(1 for s, m, c in ev if (c > s) == (m > s))
    above = sum(1 for s, m, c in ev if m > s) / n
    up = sum(1 for s, m, c in ev if c > s) / n
    e = above * up + (1 - above) * (1 - up)
    nearer = sum(1 for s, m, c in ev if abs(c - m) < abs(s - m))
    return {"n": n, "toward": k, "pct": round(k / n * 100, 1), "chance_pct": round(e * 100, 1),
            "z": round((k - n * e) / math.sqrt(n * e * (1 - e)), 2) if 0 < e < 1 else None,
            "ended_nearer_pct": round(nearer / n * 100, 1),
            "ended_nearer_note": "NOT a magnet test — no 50% chance level (see docstring)"}


def day_level(rows: list, am_range: Optional[dict] = None) -> dict:
    """Every day-level claim in CLAUDE.md, re-measured. `rows` is hist_day in
    date order for one underlying (any slice of it); `am_range` maps day ->
    09:15-11:00 range in points, the control for the 11:00 range test."""
    if not rows:
        return {"sessions": 0}
    g = lambda k, rs=rows: [r[k] for r in rs]
    absmove = [abs(m) if m is not None else None for m in g("move_pct")]
    w = [r for r in rows if r["wcash_cr"] is not None]
    nx = [r for r in rows if r["dte"] not in (0, None)]       # expiry days excluded
    out = {"sessions": len(rows)}
    out["wcash"] = {
        "n": len(w),
        "corr_vs_abs_move": _corr(g("wcash_cr"), absmove),
        "corr_vs_range": _corr(g("wcash_cr"), g("range_pct")),
        "corr_vs_signed_move": _corr(g("wcash_cr"), g("move_pct")),
        "writers_profitable_pct": round(sum(1 for r in w if r["wcash_cr"] > 0) / len(w) * 100, 1)
        if w else None,
        "ex_expiry_day": {"n": len(nx),
                          "corr_vs_abs_move": _corr(g("wcash_cr", nx),
                                                    [abs(r["move_pct"]) if r["move_pct"] is not None
                                                     else None for r in nx]),
                          "corr_vs_range": _corr(g("wcash_cr", nx), g("range_pct", nx))},
        "claimed": "corr -0.62 |move|, -0.70 range, +0.20 signed, writers profitable 75%",
    }
    out["wcash_tilt"] = {
        "corr_vs_signed_move": _corr(g("wcash_tilt"), g("move_pct")),
        "spearman": _spearman(g("wcash_tilt"), g("move_pct")),
        "sign": _sign_agree(g("wcash_tilt"), g("move_pct")),
        "claimed": "+0.765 (Spearman +0.887), sign right 98% — COINCIDENT",
    }
    am = [((am_range or {}).get(r["d"]) / r["open"] * 100)
          if (am_range or {}).get(r["d"]) is not None and r["open"] else None for r in rows]
    out["predictive_from_11"] = {
        "wcash_vs_pm_range": _corr(g("cp_wcash_cr"), g("pm_range_pct")),
        "wcash_vs_pm_range_partial_am_range": _partial(g("cp_wcash_cr"), g("pm_range_pct"), am),
        "am_range_vs_pm_range": _corr(am, g("pm_range_pct")),
        "tilt_vs_pm_move": _corr(g("cp_tilt"), g("pm_move_pct")),
        "tilt_sign_vs_pm_move": _sign_agree(g("cp_tilt"), g("pm_move_pct")),
        # ladder skew is PE-minus-CE: positive = puts richening = BEARISH, so
        # the claim it would make is the NEGATIVE of the afternoon move
        "skew_vs_pm_move": _corr(g("cp_skew"), g("pm_move_pct")),
        "skew_sign_vs_pm_move": _sign_agree([-x if x is not None else None for x in g("cp_skew")],
                                            g("pm_move_pct")),
        "claimed": "WCash@11 vs afternoon range -0.008 (-0.045 partial); tilt@11 -0.285, "
                   "sign 43% — NOT predictive",
    }
    out["ladder"] = {
        "skew_vs_signed_move": _corr(g("ladder_skew"), g("move_pct")),
        "claimed": "skew coincident (same family as tilt); expect strongly NEGATIVE (PE-CE)",
    }
    # expected move: the close-of-day sigma, judged on the NEXT session's close
    # (1-day band, calendar-day time as pulse.py uses) and on the expiry close
    by_d = {r["d"]: r for r in rows}
    days = [r["d"] for r in rows]
    nxt = {a: b for a, b in zip(days, days[1:])}
    one_day, one_day_wkend, to_exp = [], [], []
    for r in rows:
        if not (r["em_iv"] and r["em_spot"]):
            continue
        nd = nxt.get(r["d"])
        if nd and by_d[nd]["close"]:
            gap = (date.fromisoformat(nd) - date.fromisoformat(r["d"])).days
            sd = r["em_spot"] * r["em_iv"] / 100 * math.sqrt(gap / 365.0)
            (one_day if gap == 1 else one_day_wkend).append(
                abs(by_d[nd]["close"] - r["em_spot"]) <= sd)
        e = (r["expiry"] or "")[:10]
        if e in by_d and e != r["d"] and by_d[e]["close"] and r["em_one_sigma"]:
            to_exp.append(abs(by_d[e]["close"] - r["em_spot"]) <= r["em_one_sigma"])
    pct = lambda xs: round(sum(xs) / len(xs) * 100, 1) if xs else None
    out["expected_move"] = {
        "next_session_1_cal_day": {"n": len(one_day), "inside_pct": pct(one_day)},
        "next_session_over_weekend": {"n": len(one_day_wkend), "inside_pct": pct(one_day_wkend),
                                      "note": "calendar-day time over-counts non-trading days, "
                                              "so the band is wide here"},
        "to_expiry": {"n": len(to_exp), "inside_pct": pct(to_exp)},
        "target": "68% if implied = realised; above 68% = implied rich (seller's edge)",
        "claimed": "1-day: NIFTY 81%, BANKNIFTY 69%",
    }
    intraday = [(r["cp_spot"], r["cp_mp_strike"], r["close"]) for r in rows
                if r["cp_mp_strike"] and not r["cp_mp_edge"] and r["close"] and r["cp_spot"]]
    to_expiry = []
    for r in rows:
        e = (r["expiry"] or "")[:10]
        if (r["mp_strike"] and not r["mp_edge"] and e in by_d and e != r["d"]
                and by_d[e]["close"] and r["em_spot"]):
            to_expiry.append((r["em_spot"], r["mp_strike"], by_d[e]["close"]))
    out["max_pain"] = {
        "from_11_to_close": _magnet(intraday),
        "close_to_expiry": _magnet(to_expiry),
        "note": "band-edge readings excluded; close_to_expiry windows overlap (several "
                "sessions share one expiry), so its z is optimistic",
        "claimed": "magnet 21/32, p=0.055 — suggestive, not significant",
    }
    return out


def _am_ranges(hist, underlying: str) -> dict:
    """09:15-11:00 high-low range in points, per session (as % of the open
    inside day_level, where the open is at hand)."""
    return {d: h - l for d, h, l in hist.execute(
        "SELECT substr(ts,1,10), MAX(high), MIN(low) FROM underlying_candle "
        "WHERE underlying=? AND substr(ts,12,5) <= ? GROUP BY substr(ts,1,10)",
        (underlying, CHECKPOINT)) if h is not None and l is not None}


def _slice(hist, years: Optional[tuple]):
    """An in-memory copy of the event tables for a date window, so the
    unchanged review graders can be run per year."""
    m = _mem()
    lo, hi = years
    for t in EVENT_TABLES:
        col = "d" if t == "hold_baseline" else "ts"
        cur = hist.execute(f"SELECT * FROM {t} WHERE {col}>=? AND {col}<?", (lo, hi))
        cols = [c[0] for c in cur.description]
        rows = [tuple(r) for r in cur]
        if rows:
            m.executemany(f"INSERT OR IGNORE INTO {t} ({','.join(cols)}) VALUES "
                          f"({','.join('?' * len(cols))})", rows)
    m.commit()
    return m


def _signals(conn, underlying: str) -> dict:
    import review
    return review.scorecard(conn, underlying)


def scorecard_path(underlying: str) -> Path:
    return db.DATA_DIR / f"history_scorecard_{underlying}.json"


def save_scorecard(underlying: str, hist_path: Optional[str] = None) -> dict:
    """Grade once and cache: the full grade walks every replayed minute
    (seconds to minutes of CPU), far too slow to redo per page view."""
    sc = scorecard(underlying, hist_path)
    sc["graded_at"] = datetime.now(IST).isoformat(timespec="seconds")
    scorecard_path(underlying).write_text(json.dumps(sc, default=str))
    return sc


def scorecard(underlying: str = "NIFTY", hist_path: Optional[str] = None,
              per_year: bool = True) -> dict:
    hist = connect_hist(hist_path)
    rows = hist.execute("SELECT * FROM hist_day WHERE underlying=? ORDER BY d",
                        (underlying,)).fetchall()
    if not rows:
        return {"ok": False, "reason": f"no replayed {underlying} sessions yet"}
    runs = hist.execute("SELECT SUM(ok), SUM(1-ok), SUM(error IS NOT NULL AND ok=1) "
                        "FROM hist_run WHERE underlying=?", (underlying,)).fetchone()
    am = _am_ranges(hist, underlying)
    out = {"ok": True, "underlying": underlying, "range": [rows[0]["d"], rows[-1]["d"]],
           "sessions": len(rows),
           "runs": {"ok": runs[0], "failed": runs[1], "ok_with_module_errors": runs[2]},
           "signals": _signals(hist, underlying),
           "day_level": day_level(rows, am)}
    if per_year:
        out["by_year"] = {}
        for y in sorted({r["d"][:4] for r in rows}):
            yr = [r for r in rows if r["d"][:4] == y]
            m = _slice(hist, (f"{y}-01-01", f"{int(y) + 1}-01-01"))
            out["by_year"][y] = {"signals": _signals(m, underlying)["summary"],
                                 "day_level": day_level(yr, am)}
            m.close()
    return out


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("--underlying", default="NIFTY")
    r.add_argument("--since")
    r.add_argument("--until")
    r.add_argument("--arch")
    r.add_argument("--hist")
    r.add_argument("--redo", action="store_true")
    sc = sub.add_parser("scorecard")
    sc.add_argument("--underlying", default="NIFTY")
    sc.add_argument("--hist")
    sc.add_argument("--save", action="store_true", help="write the cached JSON the app serves")
    a = ap.parse_args()
    if a.cmd == "run":
        print(json.dumps(run(a.underlying, a.since, a.until, a.arch, a.hist, a.redo)))
    elif a.save:
        sc = save_scorecard(a.underlying, a.hist)
        print(json.dumps({"saved": str(scorecard_path(a.underlying)), "ok": sc.get("ok"),
                          "sessions": sc.get("sessions")}))
    else:
        print(json.dumps(scorecard(a.underlying, a.hist), indent=1, default=str))
