"""Do writers' strikes hold PRICE? The test hold.py never ran.

hold.py proved that OI builds PERSIST (writers stay on): about 70% of built
contracts close with OI above the prior session against about 36% for
contracts without a build, every year since 2022. That says nothing about
price. The tempting next step, drawing those strikes as support and
resistance, needs its own evidence, and this module is that test.

THE QUESTION. Standing at 11:00, take every OTM strike where writers have
built (call writing on a CE above spot, put writing on a PE below spot). For
the rest of the session, does price stay away from that strike more often
than it stays away from an ordinary strike at the same distance?

WHY THE MATCHING IS THE WHOLE TEST
  * Distance: writers build near the money, and a near strike is touched far
    more often than a far one, whoever wrote it. Every wall is compared with
    control strikes in the same distance bucket.
  * Volatility: 0.5% is far on a quiet day and close on a wild one. Distance
    is measured in units of the TYPICAL DAILY RANGE (mean high-low over the
    prior 10 sessions, known at 11:00), not in points or percent.
  * Day selection: if writers build more on calm days, calm days would make
    walls look strong. Controls come only from the SAME sessions (and the
    same side) as the walls.
  * Clustering: one strong rally breaks every call wall that day. The z is
    clustered by session (sum of per-session residuals over their root sum of
    squares), never a count of walls as independent bets. That is the error
    that made the OI-flow markers look like a signal on 98 live sessions.

CONTROL STRIKES are the exchange grid (NIFTY 50, BANKNIFTY 100) within ATM±10
of the 11:00 spot with NO build of any kind before 11:00. A strike with only
buyer builds belongs to neither group.

OUTCOMES, both measured from the 11:00 bar to the close on the index:
    stayed away       price never reached the strike (CE: high < strike)
    closed its side   the session closed on the wall's side of the strike

Each is reported as wall rate vs matched expected rate, so higher = better,
the same orientation as every other row of the scorecard.

HONEST LIMITS
  * Index minute bars, not the option's own settlement: "touched" is the
    index trading through the strike.
  * The first 10 sessions of any slice have no typical range and are skipped
    (per-year slices lose them each year).
  * An effect here would show that writers' strikes are reached less often.
    It cannot say whether writers DEFEND the strike or simply sell where the
    market was not going anyway. For a seller choosing a strike, either reading
    is usable; as a forecast of a bounce, neither is proven.
"""
from __future__ import annotations

import bisect
from collections import defaultdict

CUT = "11:00"
CLOSE_MIN = "15:20"            # a session must run to here to have a close
STEP = {"NIFTY": 50, "BANKNIFTY": 100}
BAND = 10                      # ATM ± strikes, the recorder's own footprint
RANGE_DAYS = 10                # typical daily range = mean of the prior N
MIN_PRIOR = 5                  # need at least this many prior sessions
# distance from spot in units of the typical daily range
EDGES = (0.0, 0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.8, 1.0, 1.25, 1.5, 2.0, 3.0)
MIN_CTRL = 10                  # control strikes needed in a bucket to grade a wall
MIN_SAMPLE = 20
WRITER_SIDE = {"CE": "call_write", "PE": "put_write"}
HIT_GROUPS = (("1", 1, 1), ("2-4", 2, 4), ("5+", 5, 10 ** 9))


def _sessions(conn, underlying: str) -> dict:
    """{day: {"open", "range_pct", "spot", "hi", "lo", "close"}} for every
    complete session, with the 11:00 cut already applied."""
    days = defaultdict(list)
    for ts, o, h, l, c in conn.execute(
            "SELECT ts, open, high, low, close FROM underlying_candle "
            "WHERE underlying=? ORDER BY ts", (underlying,)):
        if None in (o, h, l, c):
            continue
        days[ts[:10]].append((ts[11:16], o, h, l, c))
    out = {}
    for d in sorted(days):
        bars = days[d]
        if bars[-1][0] < CLOSE_MIN or bars[0][0] > CUT:
            continue
        after = [b for b in bars if b[0] >= CUT]
        if not after:
            continue
        o = bars[0][1]
        out[d] = {"open": o,
                  "range_pct": (max(b[2] for b in bars) - min(b[3] for b in bars)) / o * 100,
                  "spot": after[0][1],
                  "hi": max(b[2] for b in after), "lo": min(b[3] for b in after),
                  "close": after[-1][4]}
    order = sorted(out)
    for i, d in enumerate(order):
        prior = [out[x]["range_pct"] for x in order[max(0, i - RANGE_DAYS):i]]
        out[d]["typical"] = sum(prior) / len(prior) if len(prior) >= MIN_PRIOR else None
    return out


def _builds_by_cut(conn, underlying: str):
    """Writer-build counts and the any-build set, as known before the cut."""
    writer = defaultdict(int)
    any_build = set()
    expiry_days = set()
    for ts, exp, k, t, side in conn.execute(
            "SELECT ts, expiry, strike, opt_type, side FROM oi_builds WHERE underlying=?",
            (underlying,)):
        d = ts[:10]
        if exp and exp[:10] == d:
            expiry_days.add(d)
        if ts[11:16] >= CUT:
            continue
        any_build.add((d, k, t))
        if side == WRITER_SIDE.get(t):
            writer[(d, k, t)] += 1
    return writer, any_build, expiry_days


def observations(conn, underlying: str = "NIFTY") -> list:
    """One row per (session, OTM strike, side): wall or control, and what
    price did from the cut to the close."""
    sess = _sessions(conn, underlying)
    writer, any_build, expiry_days = _builds_by_cut(conn, underlying)
    step = STEP.get(underlying, 50)
    walls_on = defaultdict(set)
    for (d, k, t) in writer:
        walls_on[d].add((k, t))
    obs = []
    for d, s in sess.items():
        if not s["typical"] or d not in walls_on:
            continue
        spot = s["spot"]
        atm = round(spot / step) * step
        grid = {(atm + i * step, t) for i in range(-BAND, BAND + 1) for t in ("CE", "PE")}
        for k, t in grid | walls_on[d]:
            if (t == "CE" and k <= spot) or (t == "PE" and k >= spot):
                continue                                   # OTM only
            n_w = writer.get((d, k, t), 0)
            if not n_w and (d, k, t) in any_build:
                continue                                   # buyer-only builds: neither
            if not n_w and (k, t) not in grid:
                continue
            dist = abs(k - spot) / spot * 100 / s["typical"]
            b = bisect.bisect_right(EDGES, dist) - 1
            if b < 0 or b >= len(EDGES) - 1:
                continue
            touched = s["hi"] >= k if t == "CE" else s["lo"] <= k
            beyond = s["close"] > k if t == "CE" else s["close"] < k
            obs.append({"d": d, "side": t, "bucket": b, "dist": dist, "hits": n_w,
                        "touched": int(touched), "beyond": int(beyond),
                        "expiry_day": d in expiry_days})
    return obs


def _grade(walls: list, controls: list, sessions: set) -> dict:
    """Walls against same-session, same-side, distance-matched controls.

    `sessions` is every (day, side) that carried ANY writer wall, shared by all
    groups: restricting controls to a small group's own days (the 5+ walls fall
    on ~40) left most distance buckets without enough controls to grade."""
    wall_sessions = sessions
    ctrl = defaultdict(lambda: [0, 0, 0])               # touched, beyond, n
    for o in controls:
        if (o["d"], o["side"]) in wall_sessions:
            c = ctrl[(o["side"], o["bucket"])]
            c[0] += o["touched"]
            c[1] += o["beyond"]
            c[2] += 1
    used = [o for o in walls if ctrl[(o["side"], o["bucket"])][2] >= MIN_CTRL]
    n = len(used)
    out = {"n": n, "sessions": len({o["d"] for o in used}),
           "dropped_no_control": len(walls) - n}
    if not n:
        return {**out, "verdict": "insufficient sample"}
    res_t, res_b = defaultdict(float), defaultdict(float)
    et = eb = 0.0
    for o in used:
        t, b, m = ctrl[(o["side"], o["bucket"])]
        res_t[o["d"]] += o["touched"] - t / m
        res_b[o["d"]] += o["beyond"] - b / m
        et += t / m
        eb += b / m

    def z(res):
        ss = sum(v * v for v in res.values()) ** 0.5
        # stayed-away orientation: fewer touches than expected = positive
        return round(-sum(res.values()) / ss, 2) if ss else None

    ot = sum(o["touched"] for o in used)
    ob = sum(o["beyond"] for o in used)
    zt, zb = z(res_t), z(res_b)
    out.update({
        "stayed_away_pct": round((1 - ot / n) * 100, 1),
        "stayed_away_expected_pct": round((1 - et / n) * 100, 1),
        "z": zt,
        "closed_side_pct": round((1 - ob / n) * 100, 1),
        "closed_side_expected_pct": round((1 - eb / n) * 100, 1),
        "z_closed_side": zb,
        "mean_dist": round(sum(o["dist"] for o in used) / n, 2),
        "verdict": _verdict(n, zt),
    })
    return out


def _verdict(n, z) -> str:
    if n < MIN_SAMPLE:
        return "insufficient sample"
    if z is None:
        return "no baseline"
    if z > 1.96:
        return "beats baseline"
    if z < -1.96:
        return "WORSE than baseline"
    return "no evidence"


def respect(conn, underlying: str = "NIFTY") -> dict:
    """The full grade: all walls, by build count, by side, expiry day or not."""
    obs = observations(conn, underlying)
    walls = [o for o in obs if o["hits"]]
    controls = [o for o in obs if not o["hits"]]
    if not walls:
        return {"ok": False, "reason": "no writer builds before 11:00 on sessions "
                                       "with a typical range yet"}
    ws = {(o["d"], o["side"]) for o in walls}
    out = {"ok": True, "underlying": underlying, "cut": CUT,
           "distance_unit": f"mean daily range of the prior {RANGE_DAYS} sessions",
           "all": _grade(walls, controls, ws), "by_hits": {}, "by_side": {},
           "by_day_type": {}}
    for lab, lo, hi in HIT_GROUPS:
        out["by_hits"][lab] = _grade([o for o in walls if lo <= o["hits"] <= hi],
                                     controls, ws)
    for t, name in (("CE", "call walls (above spot)"), ("PE", "put walls (below spot)")):
        out["by_side"][name] = _grade([o for o in walls if o["side"] == t], controls, ws)
    # expiry day: controls from expiry days only (a different session shape)
    for lab, flag in (("expiry_day", True), ("other_days", False)):
        out["by_day_type"][lab] = _grade(
            [o for o in walls if o["expiry_day"] == flag],
            [o for o in controls if o["expiry_day"] == flag], ws)
    return out
