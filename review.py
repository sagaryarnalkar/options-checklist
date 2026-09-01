"""Signal scorecard — where our own signals were wrong.

Every directional thing this app emits gets graded here against a MATCHED
CONTROL, because a hit rate on its own is not evidence. Three separate ideas in
this project looked strong on a raw number and dissolved once the control was
run: GEX (corr -0.22 -> partial +0.055 holding DTE), HHI concentration
(Spearman -0.316 -> +0.055), and the OI-build hold rate (76% raw, 51% baseline).
Only the third survived, and only because the baseline was computed.

So the rule enforced here: NO RATE WITHOUT ITS DENOMINATOR AND ITS BASELINE.

THE BASELINE HAS TO BE TIME-OF-DAY MATCHED. Signals do not fire uniformly
through the session — the OI-flow score spikes in the first six minutes, when
both sides fire and the market is at its most directional. Comparing a signal
that clusters at 09:16 against an all-day average silently credits it with the
opening drift. Two baselines are therefore reported:

    baseline_all      every minute of the same sessions
    baseline_matched  same minute-of-day mix as the signals themselves

`baseline_matched` is the honest one. Where the two disagree, the signal is
partly measuring what time of day it likes to fire at.

VERDICTS ARE DELIBERATELY BLUNT. "no evidence" is the default and the most
common correct answer. A signal is only called working when it clears its
matched baseline with |z| > 1.96, and it is called worse-than-baseline when it
significantly underperforms — which is information too, and the kind that never
gets published anywhere else.
"""
from __future__ import annotations

import math
from collections import Counter, defaultdict

HORIZONS = (5, 15, 30)
MIN_SAMPLE = 20                 # below this, report but never judge

# LLT classification -> the direction it claims. UNCLEAR asserts nothing and is
# excluded rather than counted as a miss.
LLT_DIRECTION = {
    "FRESH LONGS": "bullish",
    "SHORT COVERING": "bullish",
    "FRESH SHORTS": "bearish",
    "LONG UNWINDING": "bearish",
}


def _two_prop_z(h1: int, n1: int, h2: int, n2: int):
    """Two-proportion z. None when either sample is too small to mean anything."""
    if not n1 or not n2:
        return None
    p1, p2 = h1 / n1, h2 / n2
    p = (h1 + h2) / (n1 + n2)
    se = math.sqrt(p * (1 - p) * (1 / n1 + 1 / n2))
    return round((p1 - p2) / se, 2) if se else None


def _verdict(hits, n, base_hits, base_n) -> str:
    if n < MIN_SAMPLE:
        return "insufficient sample"
    z = _two_prop_z(hits, n, base_hits, base_n)
    if z is None:
        return "no baseline"
    if z > 1.96:
        return "beats baseline"
    if z < -1.96:
        return "WORSE than baseline"
    return "no evidence"


def _series(conn, table: str, col: str, symbol: str) -> dict:
    """{ts -> price} for a minute series, used to build forward returns."""
    q = (f"SELECT ts, {col} FROM {table} WHERE {'underlying' if table == 'underlying_candle' else 'symbol'}=? "
         f"ORDER BY ts")
    return {r[0]: r[1] for r in conn.execute(q, (symbol,)) if r[1]}


def _forward(series: dict, horizon: int) -> dict:
    """{minute -> forward return in bps} over `horizon` minutes, same session only.

    Keyed on `ts[:16]` (minute precision, timezone suffix dropped) so a lookup
    works regardless of whether the source stored "+05:30" — futures_minute
    does and a naive key match silently found nothing.
    """
    keys = sorted(series)
    idx = {k: i for i, k in enumerate(keys)}
    out = {}
    for k in keys:
        j = idx[k] + horizon
        if j >= len(keys):
            continue
        if keys[j][:10] != k[:10]:            # never cross a session boundary
            continue
        a, b = series[k], series[keys[j]]
        if a:
            out[k[:16]] = (b / a - 1) * 1e4
    return out


def _baselines(fwd: dict, sessions: set, buckets: Counter):
    """Unconditional and time-of-day-matched 'return was positive' rates.

    The matched figure re-weights each minute-of-day by how often the signal
    actually fired in it, so a signal that only fires at the open is compared
    against the open, not against the whole day.
    """
    all_h = all_n = 0
    per_bucket = defaultdict(lambda: [0, 0])
    for ts, r in fwd.items():
        if ts[:10] not in sessions:
            continue
        all_n += 1
        all_h += r > 0
        b = ts[11:13]                          # hour bucket
        per_bucket[b][0] += r > 0
        per_bucket[b][1] += 1
    if not all_n:
        return None, None
    tot_w = sum(buckets.values()) or 1
    matched = 0.0
    for b, w in buckets.items():
        h, n = per_bucket.get(b, [0, 0])
        matched += (h / n if n else all_h / all_n) * (w / tot_w)
    return {"hits": all_h, "n": all_n, "pct": round(all_h / all_n * 100, 1)}, \
           {"pct": round(matched * 100, 1), "n": all_n}


def grade_markers(conn, underlying: str = "NIFTY") -> dict:
    """OI-flow score markers vs what the same minutes did unconditionally.

    A put_writing marker claims the next move is up, call_writing that it is
    down. The stored return columns already hold the outcome; what was missing
    — and what makes the difference between a finding and a coin flip — is the
    baseline.
    """
    rows = conn.execute(
        "SELECT ts, side, score, return_5min_bps, return_15min_bps, return_30min_bps "
        "FROM score_marker_outcomes WHERE underlying=?", (underlying,)).fetchall()
    if not rows:
        return {"ok": False, "reason": "no markers recorded"}
    spot = _series(conn, "underlying_candle", "close", underlying)
    sessions = {r[0][:10] for r in rows}
    out = {"ok": True, "underlying": underlying, "n_markers": len(rows),
           "sessions": len(sessions), "horizons": {}}
    for hi, horizon in enumerate(HORIZONS):
        col = 3 + hi
        fwd = _forward(spot, horizon)
        per_side = {}
        for side in ("put_writing", "call_writing"):
            sel = [r for r in rows if r[1] == side and r[col] is not None]
            if not sel:
                continue
            # a bullish signal wins on a positive move, a bearish one on negative
            hits = sum(1 for r in sel if (r[col] > 0) == (side == "put_writing"))
            buckets = Counter(r[0][11:13] for r in sel)
            b_all, b_match = _baselines(fwd, {r[0][:10] for r in sel}, buckets)
            if b_all is None:
                continue
            # for a bearish signal the baseline is the chance of a DOWN move
            bh = b_all["hits"] if side == "put_writing" else b_all["n"] - b_all["hits"]
            bm = b_match["pct"] if side == "put_writing" else round(100 - b_match["pct"], 1)
            per_side[side] = {
                "n": len(sel), "hits": hits,
                "pct": round(hits / len(sel) * 100, 1),
                "mean_bps": round(sum(r[col] for r in sel) / len(sel), 2),
                "baseline_all_pct": round(bh / b_all["n"] * 100, 1),
                "baseline_matched_pct": bm,
                "baseline_n": b_all["n"],
                "z_vs_all": _two_prop_z(hits, len(sel), bh, b_all["n"]),
                "verdict": _verdict(hits, len(sel), bh, b_all["n"]),
            }
        # does a HIGHER score do better? the score is sold as a strength ladder
        ladder = {}
        for sc in sorted({r[2] for r in rows}):
            sel = [r for r in rows if r[2] == sc and r[col] is not None]
            if len(sel) < 10:
                continue
            hits = sum(1 for r in sel if (r[col] > 0) == (r[1] == "put_writing"))
            ladder[str(sc)] = {"n": len(sel), "pct": round(hits / len(sel) * 100, 1)}
        out["horizons"][f"{horizon}min"] = {"by_side": per_side, "by_score": ladder}
    return out


def grade_llt(conn, symbol_like: str = "NIFTY") -> dict:
    """Large-lot futures prints vs the futures move that followed.

    These have never been graded — llt_prints has no outcome column at all, so
    every classification shipped unchecked.
    """
    rows = conn.execute(
        "SELECT ts, symbol, classification, confidence, lots, closing_flag "
        "FROM llt_prints ORDER BY ts").fetchall()
    rows = [r for r in rows if r[2] in LLT_DIRECTION]
    if not rows:
        return {"ok": False, "reason": "no classified LLT prints"}
    sym = Counter(r[1] for r in rows).most_common(1)[0][0]
    fut = _series(conn, "futures_minute", "ltp", sym)
    if not fut:
        return {"ok": False, "reason": f"no futures_minute series for {sym}"}
    out = {"ok": True, "symbol": sym, "n_prints": len(rows),
           "sessions": len({r[0][:10] for r in rows}), "horizons": {}}
    for horizon in HORIZONS:
        fwd = _forward(fut, horizon)
        graded = [(r, fwd.get(r[0][:16])) for r in rows]
        graded = [(r, v) for r, v in graded if v is not None]
        if not graded:
            out["horizons"][f"{horizon}min"] = {"n": 0,
                                                "reason": "no forward prices matched"}
            continue
        def bucketise(sel, label):
            if not sel:
                return None
            hits = sum(1 for r, v in sel
                       if (v > 0) == (LLT_DIRECTION[r[2]] == "bullish"))
            buckets = Counter(r[0][11:13] for r, _ in sel)
            b_all, b_match = _baselines(fwd, {r[0][:10] for r, _ in sel}, buckets)
            if b_all is None:
                return None
            n_bull = sum(1 for r, _ in sel if LLT_DIRECTION[r[2]] == "bullish")
            # blended baseline: each print judged against the chance of a move
            # in ITS OWN claimed direction
            up = b_all["hits"] / b_all["n"]
            base_rate = (n_bull * up + (len(sel) - n_bull) * (1 - up)) / len(sel)
            bh = round(base_rate * b_all["n"])
            return {"label": label, "n": len(sel), "hits": hits,
                    "pct": round(hits / len(sel) * 100, 1),
                    "mean_bps": round(sum(v for _, v in sel) / len(sel), 2),
                    "baseline_pct": round(base_rate * 100, 1),
                    "baseline_n": b_all["n"],
                    "z": _two_prop_z(hits, len(sel), bh, b_all["n"]),
                    "verdict": _verdict(hits, len(sel), bh, b_all["n"])}
        h = {"all": bucketise(graded, "all prints")}
        for conf in ("HIGH", "MEDIUM"):
            b = bucketise([g for g in graded if g[0][3] == conf], conf)
            if b:
                h[conf] = b
        for cls in sorted(LLT_DIRECTION):
            b = bucketise([g for g in graded if g[0][2] == cls], cls)
            if b:
                h[cls] = b
        out["horizons"][f"{horizon}min"] = h
    return out


def _safe(fn, *a, **k) -> dict:
    """Run one grader without letting it take the scorecard down.

    A signal family with no data yet — an older database, a table added in a
    later release, a feed that has not run — must degrade to one honest "no
    data" row. A health check that dies because one input is missing is the
    opposite of a health check.
    """
    try:
        return fn(*a, **k)
    except Exception as e:
        return {"ok": False, "reason": f"{type(e).__name__}: {e}"}


def scorecard(conn, underlying: str = "NIFTY") -> dict:
    """Everything gradeable, in one place, each with its control."""
    import hold as _hold
    out = {"ok": True, "underlying": underlying,
           "markers": _safe(grade_markers, conn, underlying),
           "llt": _safe(grade_llt, conn),
           "oi_builds": _safe(_hold.hold_stats, conn, underlying)}
    # a flat list of the honest headlines, for the UI and for a quick read
    lines = []
    m = out["markers"]
    if m.get("ok"):
        for hz, blk in m["horizons"].items():
            for side, s in (blk.get("by_side") or {}).items():
                lines.append({"signal": f"OI flow · {side} · {hz}",
                              "n": s["n"], "pct": s["pct"],
                              "baseline": s["baseline_matched_pct"],
                              "verdict": s["verdict"]})
    l = out["llt"]
    if l.get("ok"):
        for hz, blk in l["horizons"].items():
            a = blk.get("all")
            if a:
                lines.append({"signal": f"LLT prints · all · {hz}",
                              "n": a["n"], "pct": a["pct"],
                              "baseline": a["baseline_pct"], "verdict": a["verdict"]})
    b = out["oi_builds"]
    if b.get("ok") and b.get("matched"):
        mm = b["matched"]
        lines.append({"signal": "OI builds · did it hold",
                      "n": mm["with_build"]["n"], "pct": mm["with_build"]["pct"],
                      "baseline": mm["no_build"]["pct"],
                      "verdict": "beats baseline" if mm.get("significant") else "no evidence"})
    for key, label in (("markers", "OI flow markers"), ("llt", "LLT prints"),
                       ("oi_builds", "OI builds")):
        blk = out.get(key) or {}
        if not blk.get("ok"):
            why = str(blk.get("reason") or "no data")
            # "no such table" means the feed has never run here, which is a
            # normal state, not an error the reader should have to decode.
            if "no such table" in why or "no such column" in why:
                why = "no data recorded yet"
            lines.append({"signal": f"{label} — not graded", "n": 0, "pct": None,
                          "baseline": None, "verdict": why[:60]})
    out["summary"] = lines
    return out
