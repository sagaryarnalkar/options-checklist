"""Rebuild missing and backfilled minutes of oi_chain.db from the Fyers archive,
in exactly the format the LIVE recorder writes.

WHY THIS EXISTS. Two writers fill chain_snapshot and futures_minute, and until
this fix they disagreed about what a row means:

                     live recorder (kite.quote)      Kite backfill (historical bars)
    row at minute T  LTP at T (= close of bar T-1)   close of bar T
    volume           CUMULATIVE for the day          that one bar's volume

Everything downstream assumes the live meaning — oi_flow and llt.py both take
`volume - previous volume` as "traded this minute". So on every day the morning
was backfilled (any login after 09:15, since #37 on 2026-06-30) the first live
minute subtracted a single bar's volume from a day-cumulative one and dumped
the whole morning's turnover into one minute. Measured through the real
oi_flow.aggregate_day: on 2026-07-01 and 2026-06-30 the largest flow minute of
the ENTIRE session and the #1 big print both sit exactly on that seam (27.8x
and 14.7x the median minute). The same seam produced a fake LLT print, and
those prints are inside the LLT scorecard (#86).

WHAT THIS DOES. For each (day, underlying) it rebuilds every minute that the
live recorder did NOT write — whether missing or backfilled — from Fyers 1-min
bars, converted by `to_live()` into live semantics:

    row T   ltp = close of bar starting T-1        spot = index close of bar T-1
            oi  = OI of bar T-1                     volume = sum of bars 09:15..T-1
    09:15   ltp = OPEN of the 09:15 bar, volume 0, oi = prior session's close OI
    a minute with no trade carries the last price/OI forward, as LTP would.

Live-written minutes are NEVER touched: they are identified from recorder_log
(the live recorder writes one row per minute per underlying; a backfill writes
one row tagged 'backfill ...'). Underlying candles are bar-start stamped by both
writers already, so they are only gap-filled, never rewritten.

`compare()` is the safety gate: it rebuilds minutes we DID record live and
measures them against the real rows. If the conventions are right the two
agree; nothing is written until they do.

Recorder session: 09:15-15:30. Fyers bars run to 15:39 (closing auction); bars
after 15:29 are ignored so rebuilt days look like recorded ones.
"""
from __future__ import annotations

import sqlite3
from datetime import date, datetime, timedelta, timezone
from typing import Optional

IST = timezone(timedelta(hours=5, minutes=30))
ATM_N = 10
FUT_ROLL_DAYS = 2                 # recorder._current_nifty_future's rule


# ------------------------------------------------------------- conversion --

def session_minutes(day: date) -> list[str]:
    """09:15 .. 15:30 inclusive, in the recorder's exact ts format."""
    t = datetime(day.year, day.month, day.day, 9, 15, tzinfo=IST)
    end = t.replace(hour=15, minute=30)
    out = []
    while t <= end:
        out.append(t.isoformat())
        t += timedelta(minutes=1)
    return out


def minute_iso(epoch: int) -> str:
    return datetime.fromtimestamp(int(epoch), IST).replace(second=0, microsecond=0).isoformat()


def to_live(bars: dict, minutes: list[str], seed_close=None, seed_oi=None) -> dict:
    """Bars keyed by their START minute -> what the live recorder would have
    written at each minute.

    `bars`: {minute_iso: (open, close, volume, oi)}; `minutes`: the FULL
    contiguous session (cumulative volume needs every bar from 09:15).
    Returns {minute_iso: (ltp, cumulative_volume, oi)}; minutes with no price
    at all yet are omitted, never filled with a guess.
    """
    out = {}
    last_close, last_oi, cum = seed_close, seed_oi, 0
    for i, m in enumerate(minutes):
        if i == 0:
            b = bars.get(m)
            ltp = b[0] if (b and b[0] is not None) else last_close
            oi = last_oi if last_oi is not None else (b[3] if b else None)
            if ltp is not None:
                out[m] = (ltp, 0, oi)
            continue
        b = bars.get(minutes[i - 1])          # the bar that ENDED at minute m
        if b:
            _, c, v, o = b
            if c is not None:
                last_close = c
            if o is not None:
                last_oi = o
            cum += v or 0
        if last_close is not None:
            out[m] = (last_close, cum, last_oi)
    return out


# --------------------------------------------------------------- provenance --

def live_minutes(conn, underlying: str, day: date) -> set[str]:
    """Minutes the LIVE recorder wrote for this underlying (recorder_log has one
    row per successful live tick; backfills log one row tagged 'backfill')."""
    d = day.isoformat()
    return {r[0][:16] for r in conn.execute(
        "SELECT ts FROM recorder_log WHERE underlying=? AND ts>=? AND ts<? "
        "AND rows_inserted > 0 AND (error IS NULL OR error NOT LIKE 'backfill%')",
        (underlying, d, d + "~"))}


def tick_minutes(conn, day: date) -> set[str]:
    """Minutes ANY underlying logged live. The futures snapshot runs at the
    start of every tick and does not log, so a tick that ran = futures live."""
    d = day.isoformat()
    return {r[0][:16] for r in conn.execute(
        "SELECT ts FROM recorder_log WHERE ts>=? AND ts<? AND rows_inserted > 0 "
        "AND (error IS NULL OR error NOT LIKE 'backfill%')", (d, d + "~"))}


def tracked_expiry(conn, underlying: str, day: date) -> Optional[str]:
    """The expiry the recorder tracked (or would have): what it actually wrote
    that day if anything, else the nearest expiry on/after the day from OUR OWN
    history (see recorder._true_expiry_for_day for why not the live dump)."""
    d = day.isoformat()
    r = conn.execute("SELECT expiry, COUNT(*) n FROM chain_snapshot WHERE underlying=? "
                     "AND ts>=? AND ts<? GROUP BY expiry ORDER BY n DESC LIMIT 1",
                     (underlying, d, d + "~")).fetchone()
    if r and r[0]:
        return str(r[0])[:10]
    r = conn.execute("SELECT MIN(expiry) FROM chain_snapshot WHERE underlying=? AND expiry>=?",
                     (underlying, d)).fetchone()
    return str(r[0])[:10] if r and r[0] else None


# ------------------------------------------------------------- the archive --

def _day_bounds(day: date) -> tuple[int, int]:
    a = int(datetime(day.year, day.month, day.day, 9, 15, tzinfo=IST).timestamp())
    b = int(datetime(day.year, day.month, day.day, 15, 29, tzinfo=IST).timestamp())
    return a, b


def _bars_for(arch, contract_id: int, day: date):
    a, b = _day_bounds(day)
    bars = {minute_iso(ts): (o, c, v, oi) for ts, o, c, v, oi in arch.execute(
        "SELECT ts, o, c, v, oi FROM bar WHERE contract_id=? AND ts BETWEEN ? AND ?",
        (contract_id, a, b))}
    seed = arch.execute("SELECT c, oi FROM bar WHERE contract_id=? AND ts < ? "
                        "ORDER BY ts DESC LIMIT 1", (contract_id, a)).fetchone()
    return bars, (seed[0] if seed else None), (seed[1] if seed else None)


def _index_bars(arch, underlying: str, day: date):
    a, b = _day_bounds(day)
    rows = arch.execute("SELECT ts, o, h, l, c, v FROM index_bar WHERE underlying=? AND res='1' "
                        "AND ts BETWEEN ? AND ? ORDER BY ts", (underlying, a, b)).fetchall()
    return {minute_iso(ts): (o, c, v or 0, None) for ts, o, h, l, c, v in rows}, rows


def _future_for(arch, day: date):
    """recorder._current_nifty_future: nearest FUT expiring on/after the day,
    rolled to the next one within FUT_ROLL_DAYS of expiry."""
    futs = arch.execute("SELECT id, symbol, expiry FROM contract WHERE underlying='NIFTY' "
                        "AND kind='FUT' AND expiry >= ? ORDER BY expiry LIMIT 2",
                        (day.isoformat(),)).fetchall()
    if not futs:
        return None
    pick = futs[0]
    if (date.fromisoformat(pick[2]) - day).days <= FUT_ROLL_DAYS and len(futs) > 1:
        pick = futs[1]
    return {"id": pick[0], "symbol": pick[1].split(":", 1)[-1], "expiry": pick[2]}


def build(arch, underlying: str, day: date, expiry: str) -> dict:
    """Everything the live recorder would have written that day, from Fyers."""
    minutes = session_minutes(day)
    idx_bars, idx_rows = _index_bars(arch, underlying, day)
    if not idx_bars:
        return {"ok": False, "reason": f"no Fyers index minutes for {underlying} on {day}"}
    spot = {m: v[0] for m, v in to_live(idx_bars, minutes).items()}
    contracts = arch.execute("SELECT id, strike, kind FROM contract WHERE underlying=? "
                             "AND expiry=? AND kind IN ('CE','PE')",
                             (underlying, expiry)).fetchall()
    if not contracts:
        return {"ok": False, "reason": f"no archived {underlying} contracts for expiry {expiry}"}
    strikes = sorted({k for _, k, _ in contracts})
    series = {}
    for cid, strike, kind in contracts:
        bars, sc, so = _bars_for(arch, cid, day)
        if bars or sc is not None:
            series[(strike, kind)] = to_live(bars, minutes, sc, so)
    chain, short = {}, 0
    for m in minutes:
        s = spot.get(m)
        if s is None:
            continue
        ai = min(range(len(strikes)), key=lambda i: abs(strikes[i] - s))
        if ai - ATM_N < 0 or ai + ATM_N >= len(strikes):
            short += 1                       # band runs off the archived strikes
        band = strikes[max(0, ai - ATM_N): ai + ATM_N + 1]
        rows = []
        for k in band:
            for kind in ("CE", "PE"):
                v = series.get((k, kind), {}).get(m)
                if v:
                    rows.append({"ts": m, "underlying": underlying, "spot": float(s),
                                 "expiry": expiry, "strike": int(k), "opt_type": kind,
                                 "ltp": v[0], "volume": v[1], "oi": v[2]})
        chain[m] = rows
    fut = None
    if underlying == "NIFTY":
        f = _future_for(arch, day)
        if f:
            bars, sc, so = _bars_for(arch, f["id"], day)
            fut = {"symbol": f["symbol"], "rows": to_live(bars, minutes, sc, so)}
    candles = [{"ts": minute_iso(ts), "underlying": underlying, "open": o, "high": h,
                "low": l, "close": c, "volume": v or 0} for ts, o, h, l, c, v in idx_rows]
    return {"ok": True, "expiry": expiry, "chain": chain, "futures": fut,
            "candles": candles, "band_short_minutes": short,
            "archived_strikes": len(strikes)}


# ---------------------------------------------------------------- compare --

def compare(conn, arch, underlying: str, day: date) -> dict:
    """Rebuild minutes that were recorded LIVE and measure the rebuild against
    the real rows. The gate before any write: if the conventions in to_live()
    were wrong, it shows up here as disagreement."""
    live = live_minutes(conn, underlying, day)
    expiry = tracked_expiry(conn, underlying, day)
    if not live or not expiry:
        return {"ok": False, "reason": "no live minutes to compare against"}
    b = build(arch, underlying, day, expiry)
    if not b.get("ok"):
        return b
    d = day.isoformat()
    real = {}
    for ts, k, t, ltp, vol, oi, sp in conn.execute(
            "SELECT ts, strike, opt_type, ltp, volume, oi, spot FROM chain_snapshot "
            "WHERE underlying=? AND ts>=? AND ts<?", (underlying, d, d + "~")):
        if ts[:16] in live:
            real[(ts, int(k), t)] = (ltp, vol, oi, sp)
    errs = {"ltp": [], "volume": [], "oi": [], "spot": []}
    band_same = band_n = 0
    for m, rows in b["chain"].items():
        if m[:16] not in live:
            continue
        rebuilt = {(r["strike"], r["opt_type"]) for r in rows}
        actual = {(k, t) for (ts, k, t) in real if ts == m}
        if actual:
            band_n += 1
            band_same += rebuilt == actual
        for r in rows:
            x = real.get((m, r["strike"], r["opt_type"]))
            if not x:
                continue
            for key, a, z in (("ltp", r["ltp"], x[0]), ("volume", r["volume"], x[1]),
                              ("oi", r["oi"], x[2]), ("spot", r["spot"], x[3])):
                if a is not None and z:
                    errs[key].append(abs(a - z) / abs(z))

    def s(xs, tol):
        if not xs:
            return {"n": 0}
        xs.sort()
        return {"n": len(xs), "median": round(xs[len(xs) // 2], 5),
                "p90": round(xs[int(len(xs) * .9)], 5),
                "within_tol_pct": round(sum(x <= tol for x in xs) / len(xs) * 100, 1)}
    return {"ok": True, "underlying": underlying, "day": d, "expiry": expiry,
            "live_minutes": len(live),
            "band_identical_pct": round(band_same / band_n * 100, 1) if band_n else None,
            "ltp": s(errs["ltp"], 0.01), "oi": s(errs["oi"], 0.001),
            "volume": s(errs["volume"], 0.02), "spot": s(errs["spot"], 0.0005),
            "tolerances": "ltp 1%, oi 0.1%, cumulative volume 2%, spot 0.05%"}


# ------------------------------------------------------------------ apply --

def plan_day(conn, arch, underlying: str, day: date) -> dict:
    live = live_minutes(conn, underlying, day)
    minutes = session_minutes(day)
    target = [m for m in minutes if m[:16] not in live]
    expiry = tracked_expiry(conn, underlying, day)
    d = day.isoformat()
    existing = {r[0] for r in conn.execute(
        "SELECT DISTINCT ts FROM chain_snapshot WHERE underlying=? AND ts>=? AND ts<?",
        (underlying, d, d + "~"))}
    return {"day": d, "underlying": underlying, "expiry": expiry,
            "live": len(live), "to_rebuild": len(target),
            "of_which_backfilled": sum(1 for m in target if m in existing),
            "of_which_missing": sum(1 for m in target if m not in existing),
            "_target": target}


def rebuild_day(conn, arch, underlying: str, day: date, dry_run: bool = True) -> dict:
    """Replace every non-live minute of (day, underlying) with the Fyers-derived
    live-convention rows. Live minutes are never modified."""
    p = plan_day(conn, arch, underlying, day)
    target = p.pop("_target")
    out = dict(p)
    if not target or not p["expiry"]:
        out.update(ok=True, note="nothing to rebuild" if not target else "no tracked expiry")
        return out
    b = build(arch, underlying, day, p["expiry"])
    if not b.get("ok"):
        out.update(ok=False, reason=b.get("reason"))
        return out
    rows = [r for m in target for r in b["chain"].get(m, [])]
    covered = sum(1 for m in target if b["chain"].get(m))
    out.update(chain_rows=len(rows), minutes_covered=covered,
               band_short_minutes=b["band_short_minutes"])
    fut_rows, fut_target = [], []
    if b["futures"]:
        ticks = tick_minutes(conn, day)
        d = day.isoformat()
        have_fut = {r[0][:16] for r in conn.execute(
            "SELECT ts FROM futures_minute WHERE ts>=? AND ts<? AND symbol LIKE 'NIFTY%FUT'",
            (d, d + "~"))}
        if have_fut & ticks:
            # futures were recorded live that day: only fill what the ticks missed
            fut_target = [m for m in session_minutes(day) if m[:16] not in ticks]
        else:
            # NO live futures that day — before LLT tracking began (#53,
            # 2026-07-15) the recorder never wrote them. "A tick ran" does not
            # mean "futures were recorded" here, so rebuild the WHOLE session:
            # a fragment (the canary wrote 13 minutes for 2026-07-01) would
            # feed llt.detect_day a partial day and seed a print from it.
            fut_target = list(session_minutes(day))
        out["futures_scope"] = "fill" if (have_fut & ticks) else "full_session"
        fut_rows = [(m, b["futures"]["symbol"], *b["futures"]["rows"][m])
                    for m in fut_target if m in b["futures"]["rows"]]
        out.update(future=b["futures"]["symbol"], futures_rows=len(fut_rows))
    if dry_run:
        out.update(ok=True, dry_run=True)
        return out

    if conn.in_transaction:
        conn.commit()
    conn.execute("BEGIN")
    try:
        conn.executemany("DELETE FROM chain_snapshot WHERE underlying=? AND ts=?",
                         [(underlying, m) for m in target])
        conn.executemany(
            "INSERT OR IGNORE INTO chain_snapshot (ts, underlying, spot, expiry, strike, "
            "opt_type, ltp, volume, oi) VALUES (?,?,?,?,?,?,?,?,?)",
            [(r["ts"], r["underlying"], r["spot"], r["expiry"], r["strike"], r["opt_type"],
              r["ltp"], r["volume"], r["oi"]) for r in rows])
        if fut_target:
            conn.executemany("DELETE FROM futures_minute WHERE ts=? AND symbol LIKE 'NIFTY%FUT'",
                             [(m,) for m in fut_target])
            conn.executemany("INSERT OR IGNORE INTO futures_minute (ts, symbol, ltp, volume, oi) "
                             "VALUES (?,?,?,?,?)", fut_rows)
        conn.executemany(
            "INSERT OR IGNORE INTO underlying_candle (ts, underlying, open, high, low, close, volume) "
            "VALUES (?,?,?,?,?,?,?)",
            [(c["ts"], c["underlying"], c["open"], c["high"], c["low"], c["close"], c["volume"])
             for c in b["candles"]])
        conn.execute("INSERT INTO recorder_log (ts, underlying, expiry, rows_inserted, strikes, error) "
                     "VALUES (?,?,?,?,?,?)",
                     (datetime.now(IST).isoformat(), underlying, p["expiry"], len(rows),
                      b["archived_strikes"], f"backfill {p['day']} (fyers rebuild)"))
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    out.update(ok=True, dry_run=False)
    return out


def redo_llt(conn, day: date, lot_size: int) -> dict:
    """Fake seam prints survive a plain re-detect (detect_day is INSERT OR
    IGNORE), so the day's prints are deleted first. Later prints that had been
    matched to a deleted one are unmatched and re-matched."""
    import llt
    d = day.isoformat()
    gone = [r[0] for r in conn.execute("SELECT id FROM llt_prints WHERE DATE(ts)=?", (d,))]
    if gone:
        q = ",".join("?" * len(gone))
        later = [r[0] for r in conn.execute(
            f"SELECT DISTINCT DATE(ts) FROM llt_prints WHERE matched_id IN ({q})", gone)]
        conn.execute(f"UPDATE llt_prints SET matched_id=NULL WHERE matched_id IN ({q})", gone)
        conn.execute("DELETE FROM llt_prints WHERE DATE(ts)=?", (d,))
        conn.commit()
    else:
        later = []
    written = llt.detect_day(conn, d, lot_size)
    for ld in sorted(set(later)):
        llt._match_cross_session(conn, ld)
    return {"deleted": len(gone), "detected": written, "rematched_days": sorted(set(later))}


def trading_days(conn, arch, underlying: str, since: date, until: date) -> list[date]:
    """Days the exchange traded, from Fyers' index minutes (authoritative — it
    is how the archive proves 2026-06-26 was not a session) unioned with any
    day we recorded."""
    a = int(datetime(since.year, since.month, since.day, tzinfo=IST).timestamp())
    b = int(datetime(until.year, until.month, until.day, 23, 59, tzinfo=IST).timestamp())
    # IST day number = (epoch + 5h30m) // 1 day; one row per traded day
    days = {date.fromordinal(date(1970, 1, 1).toordinal() + r[0]) for r in arch.execute(
        "SELECT DISTINCT (ts + 19800) / 86400 FROM index_bar WHERE underlying=? AND res='1' "
        "AND ts BETWEEN ? AND ?", (underlying, a, b))}
    days |= {date.fromisoformat(r[0]) for r in conn.execute(
        "SELECT DISTINCT substr(ts,1,10) FROM chain_snapshot WHERE underlying=? AND ts>=? AND ts<?",
        (underlying, since.isoformat(), until.isoformat() + "~"))}
    return sorted(d for d in days if since <= d <= until)


def rebuild_range(conn, arch, underlying: str, since: date, until: date,
                  dry_run: bool = True, lot_size: Optional[int] = None,
                  stop=None, log=print) -> dict:
    """Every trading day in the range: rebuild non-live minutes, redo LLT where
    futures changed, and re-grade OI-build holds for the day AND the next one
    (the next day's 'held' is judged against this day's close)."""
    import hold
    days = trading_days(conn, arch, underlying, since, until)
    changed, results = [], []
    for d in days:
        if stop is not None and stop.is_set():
            break
        try:
            r = rebuild_day(conn, arch, underlying, d, dry_run=dry_run)
        except Exception as e:
            r = {"day": d.isoformat(), "ok": False, "reason": f"{type(e).__name__}: {e}"}
        if r.get("to_rebuild"):
            results.append({k: v for k, v in r.items() if k != "underlying"})
            log(f"[rebuild] {underlying} {d}: {r.get('to_rebuild')} min "
                f"({r.get('of_which_missing')} missing, {r.get('of_which_backfilled')} backfilled)"
                f" -> {'DRY' if dry_run else 'written'} ok={r.get('ok')} {r.get('reason') or ''}")
            if r.get("ok") and not dry_run and r.get("chain_rows"):
                changed.append(d)
                if r.get("futures_rows") and lot_size:
                    r["llt"] = redo_llt(conn, d, lot_size)
    regraded = set()
    if not dry_run:
        for d in changed:
            for g in (d, d + timedelta(days=1), d + timedelta(days=3)):
                if g.isoformat() in regraded:
                    continue
                try:
                    hold.run_session(conn, underlying, g.isoformat())
                    regraded.add(g.isoformat())
                except Exception:
                    pass
    return {"underlying": underlying, "since": since.isoformat(), "until": until.isoformat(),
            "dry_run": dry_run, "trading_days": len(days),
            "days_needing_work": len(results), "days_written": len(changed),
            "missing_minutes": sum(r.get("of_which_missing") or 0 for r in results),
            "backfilled_minutes": sum(r.get("of_which_backfilled") or 0 for r in results),
            "failed": [r for r in results if not r.get("ok")],
            "holds_regraded": sorted(regraded), "days": results}
