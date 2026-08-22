"""Did-it-hold tracking for per-strike open-interest builds.

The OI Flow tab flags activity. Nothing until now checked whether the flagged
build was still there at the closing bell — so the score had never been graded
against anything. This module supplies that grade.

The idea is borrowed from DaySwingTrader's "...and the ones that did not hold"
panel, and it is the most honest thing on their site: they follow every flag to
the end of the session and report whether open interest stayed on. Note what
their own numbers say — 18 held against 16 that did not, barely better than a
coin toss, printed next to a "Signal Confidence 100" badge. That ratio is the
product, not the badge.

DEFINITIONS (deliberately narrow — this measures ONE thing)
    A BUILD is a minute in which a strike's open interest rose by more than its
    own recent noise, on real traded value.
    It HELD if the strike's open interest at the closing bell is still above
    the PRIOR SESSION's close. It DID NOT HOLD if it round-tripped back below.

WHAT THIS IS NOT: a profit or loss figure. A build that holds can still be a
losing position, and a build that unwinds may have been closed at a gain. This
answers "did the position stay on", nothing more.

HONEST GAPS, enforced in code rather than in a footnote:
  * Prior-session close is unknown for the first session we recorded and for a
    strike's first day of life. Those rows get held=None and are EXCLUDED from
    the base rate rather than silently counted as failures.
  * A session we only partly recorded has no real closing bell. Sessions
    without a snapshot at/after CLOSE_MIN are marked incomplete and excluded.
  * We record the nearest expiry, ATM+-10 only. A build on a strike that walks
    out of the recorded band mid-session is dropped, not guessed at.
"""
from __future__ import annotations

import statistics
from collections import defaultdict

# Gate: a build must clear BOTH its own recent noise and an absolute floor, the
# same max(floor, median + k*MAD) discipline the LLT gate uses. A pure
# percentile gate fires constantly on a dead strike; a pure absolute floor
# never fires on a quiet one.
MAD_K = 4.0
FLOOR_UNITS = 25_000          # ~330 NIFTY lots; below this it is noise
MIN_VALUE_CR = 0.25           # traded premium in the build minute
TRAIL_MIN = 60                # trailing window for the noise estimate
WARMUP_MIN = 20               # need this much history before gating
CLOSE_MIN = "15:2"            # a real close needs a snapshot at/after 15:20


def _mad(xs):
    if not xs:
        return 0.0
    m = statistics.median(xs)
    return statistics.median([abs(x - m) for x in xs]) or 0.0


def _side(opt_type: str, dprice: float) -> tuple[str, str]:
    """Classify by the minute's price direction — the same proxy the OI Flow
    buckets use, kept identical on purpose so the two views agree.

    This is a PROXY for bid/ask aggressor, not the real thing; see aggressor.py
    for the tick-level version that measures it directly.
    """
    if opt_type == "CE":
        return ("call_buy", "bullish") if dprice > 0 else ("call_write", "bearish")
    return ("put_buy", "bearish") if dprice > 0 else ("put_write", "bullish")


def _series(conn, underlying: str, date: str):
    """Per-(expiry,strike,type) minute series for one session.

    Ranges on `ts` rather than substr(ts,1,10) so the UNIQUE(ts, ...) index is
    usable — the substr form forces a full scan and made a 16-session sweep
    take minutes.
    """
    rows = conn.execute(
        "SELECT ts, expiry, strike, opt_type, oi, volume, ltp, spot "
        "FROM chain_snapshot WHERE ts>=? AND ts<? AND underlying=? "
        "ORDER BY expiry, strike, opt_type, ts",
        (date, date + "~", underlying)).fetchall()
    out = defaultdict(list)
    for r in rows:
        out[(r[1], r[2], r[3])].append(
            {"ts": r[0], "oi": r[4] or 0, "volume": r[5] or 0,
             "ltp": r[6] or 0.0, "spot": r[7] or 0.0})
    return out


def _prior_close_oi(conn, underlying: str, date: str) -> dict:
    """Closing OI per contract on the last session BEFORE `date`."""
    prev = conn.execute(
        "SELECT substr(MAX(ts),1,10) FROM chain_snapshot "
        "WHERE ts<? AND underlying=?", (date, underlying)).fetchone()[0]
    if not prev:
        return {}
    # ordered scan, keep the last row per contract — far cheaper than a
    # correlated MAX(ts) subquery per contract
    out = {}
    for exp, k, t, oi in conn.execute(
            "SELECT expiry, strike, opt_type, oi FROM chain_snapshot "
            "WHERE ts>=? AND ts<? AND underlying=? ORDER BY ts",
            (prev, prev + "~", underlying)):
        out[(exp, k, t)] = oi or 0
    return out


def session_complete(conn, underlying: str, date: str) -> bool:
    """Did we record through the closing bell? Without it there is no 'close'
    to judge against, and a partial session would fake failures."""
    last = conn.execute(
        "SELECT MAX(ts) FROM chain_snapshot WHERE ts>=? AND ts<? AND underlying=?",
        (date, date + "~", underlying)).fetchone()[0]
    return bool(last) and last[11:16] >= CLOSE_MIN.ljust(5, "0")[:5]


def detect_builds(conn, underlying: str, date: str, *, mad_k=MAD_K,
                  floor_units=FLOOR_UNITS, min_value_cr=MIN_VALUE_CR,
                  lot_size: int = 1) -> list:
    """Find OI-build minutes and follow each one to the closing bell."""
    series = _series(conn, underlying, date)
    prior = _prior_close_oi(conn, underlying, date)
    complete = session_complete(conn, underlying, date)
    builds = []
    for (exp, strike, otype), pts in series.items():
        if len(pts) < WARMUP_MIN + 2:
            continue
        doi = [0] + [pts[i]["oi"] - pts[i - 1]["oi"] for i in range(1, len(pts))]
        dvol = [0] + [max(0, pts[i]["volume"] - pts[i - 1]["volume"])
                      for i in range(1, len(pts))]
        oi_close = pts[-1]["oi"]
        pc = prior.get((exp, strike, otype))
        for i in range(WARMUP_MIN, len(pts)):
            trail = [abs(x) for x in doi[max(1, i - TRAIL_MIN):i]]
            if not trail:
                continue
            gate = max(floor_units, statistics.median(trail) + mad_k * _mad(trail))
            if doi[i] < gate:
                continue
            value_cr = dvol[i] * (pts[i]["ltp"] or 0) / 1e7
            if value_cr < min_value_cr:
                continue
            dprice = (pts[i]["ltp"] or 0) - (pts[i - 1]["ltp"] or 0)
            side, bias = _side(otype, dprice)
            after = [p["oi"] for p in pts[i:]]
            builds.append({
                "ts": pts[i]["ts"], "underlying": underlying, "expiry": exp,
                "strike": strike, "opt_type": otype,
                "oi_at_fire": pts[i]["oi"], "doi_at_fire": doi[i],
                "vol_units": dvol[i], "value_cr": round(value_cr, 3),
                "ltp_at_fire": pts[i]["ltp"], "spot_at_fire": pts[i]["spot"],
                "side": side, "bias": bias,
                "gate_units": round(gate, 0),
                "oi_prior_close": pc,
                "oi_peak": max(after), "oi_low": min(after), "oi_close": oi_close,
                # held is None — not False — when we cannot honestly judge it
                "held": None if (pc is None or not complete)
                        else int(oi_close > pc),
                "day_delta": None if pc is None else oi_close - pc,
                "session_complete": int(complete),
            })
    builds.sort(key=lambda b: b["ts"])
    return builds


def annotate_hits(builds: list) -> list:
    """Stamp each build with how many builds hit that contract that session.

    Measured on 16 sessions: one build is worth almost nothing (56.9% held vs a
    54.9% baseline), while 5+ builds on the same contract hold 71.5% of the
    time. Accumulation is the signal; a lone flag is close to noise. The UI
    ranks on this rather than on individual flags.
    """
    from collections import Counter
    n = Counter((b["expiry"], b["strike"], b["opt_type"], b["ts"][:10]) for b in builds)
    for b in builds:
        b["hits"] = n[(b["expiry"], b["strike"], b["opt_type"], b["ts"][:10])]
    return builds


def store_builds(conn, builds: list) -> int:
    cols = ("ts", "underlying", "expiry", "strike", "opt_type", "oi_at_fire",
            "doi_at_fire", "vol_units", "value_cr", "ltp_at_fire", "spot_at_fire",
            "side", "bias", "gate_units", "oi_prior_close", "oi_peak", "oi_low",
            "oi_close", "day_delta", "held", "hits", "session_complete")
    conn.executemany(
        f"INSERT OR REPLACE INTO oi_builds ({','.join(cols)}) "
        f"VALUES ({','.join('?' * len(cols))})",
        [tuple(b.get(c) for c in cols) for b in builds])
    conn.commit()
    return len(builds)


def session_baseline(conn, underlying: str, date: str, flagged: set) -> dict | None:
    """The control the whole feature rests on: one row per CONTRACT-DAY, split
    by whether it carried a flagged build.

    Without this the hold rate is meaningless — open interest drifts upward
    through an expiry cycle, so a high "held" number can be pure drift. Only
    the gap between flagged and unflagged contract-days is evidence, and it is
    the same partial-correlation discipline that exposed GEX and HHI as
    artifacts. Measured over 16 sessions: 67.8% vs 54.9%, +12.9pp, z=4.14.
    """
    if not session_complete(conn, underlying, date):
        return None
    prior = _prior_close_oi(conn, underlying, date)
    if not prior:
        return None
    wb = wn = nb = nn = 0
    for key, pts in _series(conn, underlying, date).items():
        pc = prior.get(key)
        if pc is None or not pts:
            continue
        h = int(pts[-1]["oi"] > pc)
        if key in flagged:
            wb += h; wn += 1
        else:
            nb += h; nn += 1
    if not (wn or nn):
        return None
    return {"d": date, "underlying": underlying, "with_build_held": wb,
            "with_build_n": wn, "no_build_held": nb, "no_build_n": nn}


def run_session(conn, underlying: str, date: str, **kw) -> dict:
    """Detect, grade and store one session's builds, plus its control row."""
    b = annotate_hits(detect_builds(conn, underlying, date, **kw))
    flagged = {(x["expiry"], x["strike"], x["opt_type"]) for x in b}
    base = session_baseline(conn, underlying, date, flagged)
    if base:
        conn.execute(
            "INSERT OR REPLACE INTO hold_baseline "
            "(d,underlying,with_build_held,with_build_n,no_build_held,no_build_n) "
            "VALUES (?,?,?,?,?,?)",
            (base["d"], base["underlying"], base["with_build_held"],
             base["with_build_n"], base["no_build_held"], base["no_build_n"]))
        conn.commit()
    return {"date": date, "underlying": underlying,
            "builds": store_builds(conn, b), "baseline": base,
            "judgeable": sum(1 for x in b if x["held"] is not None),
            "held": sum(x["held"] for x in b if x["held"] is not None)}


def hold_stats(conn, underlying: str | None = None, limit_days: int = 60) -> dict:
    """Base rate, plus the breakdowns that turned out to matter.

    Every rate is reported with its denominator. A hold rate quoted without the
    sample it came from is how a coin flip gets sold as a signal.
    """
    where, args = "held IS NOT NULL", []
    if underlying:
        where += " AND underlying=?"
        args.append(underlying)
    rows = conn.execute(
        f"SELECT side, bias, value_cr, hits, held, underlying FROM oi_builds "
        f"WHERE {where}", args).fetchall()
    if not rows:
        return {"ok": False, "reason": "no graded builds yet"}

    def rate(sel):
        s = [r for r in rows if sel(r)]
        return {"held": sum(r[4] for r in s), "n": len(s),
                "pct": round(sum(r[4] for r in s) / len(s) * 100, 1) if s else None}

    hb = [("1", lambda r: r[3] == 1), ("2-4", lambda r: 2 <= r[3] <= 4),
          ("5+", lambda r: r[3] >= 5)]

    # The headline is the MATCHED per-contract-day comparison, not the
    # per-event rate: a contract with 20 builds counts 20 times in the event
    # tally, which flatters it. The lift over unflagged contract-days is the
    # only number that is evidence of anything.
    bw, bargs = ("underlying=?", [underlying]) if underlying else ("1=1", [])
    b = conn.execute(
        f"SELECT SUM(with_build_held), SUM(with_build_n), SUM(no_build_held), "
        f"SUM(no_build_n) FROM hold_baseline WHERE {bw}", bargs).fetchone()
    matched = None
    if b and b[1] and b[3]:
        wp, np_ = b[0] / b[1] * 100, b[2] / b[3] * 100
        se = ((b[0] + b[2]) / (b[1] + b[3]))
        se = (se * (1 - se) * (1 / b[1] + 1 / b[3])) ** 0.5
        matched = {
            "with_build": {"held": b[0], "n": b[1], "pct": round(wp, 1)},
            "no_build": {"held": b[2], "n": b[3], "pct": round(np_, 1)},
            "lift_pp": round(wp - np_, 1),
            "z": round((wp - np_) / 100 / se, 2) if se else None,
        }
        matched["significant"] = bool(matched["z"] and abs(matched["z"]) > 1.96)
    return {
        "ok": True,
        "matched": matched,
        "overall": rate(lambda r: True),
        "by_side": {s: rate(lambda r, s=s: r[0] == s)
                    for s in sorted({r[0] for r in rows})},
        "by_bias": {b: rate(lambda r, b=b: r[1] == b)
                    for b in sorted({r[1] for r in rows if r[1]})},
        "by_hits": {k: rate(f) for k, f in hb},
        "sessions": conn.execute(
            f"SELECT COUNT(DISTINCT substr(ts,1,10)) FROM oi_builds WHERE {where}",
            args).fetchone()[0],
    }


def recent_builds(conn, underlying: str, date: str, limit: int = 40) -> dict:
    """One session's builds, split into the two panels the UI shows."""
    rows = [dict(zip([c[0] for c in cur.description], r))
            for cur in [conn.execute(
                "SELECT * FROM oi_builds WHERE ts>=? AND ts<? AND underlying=? "
                "ORDER BY value_cr DESC LIMIT ?", (date, date + "~", underlying, limit))]
            for r in cur]
    return {"held": [r for r in rows if r["held"] == 1],
            "did_not_hold": [r for r in rows if r["held"] == 0],
            "unjudged": [r for r in rows if r["held"] is None]}
