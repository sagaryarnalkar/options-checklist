"""
OI Flow aggregation — produces what the OI Flow tab renders:

  - candles[]          — minute OHLC from underlying_candle (Kite-sourced).
                         Falls back to a single-value bar (O=H=L=C=spot) when
                         no candle is stored for that minute, so legacy data
                         still draws something.
  - score_markers[]    — at most one marker per minute, only when the
                         dominant ATM-band writing pressure (PUT or CALL)
                         exceeds the threshold. Score is 1..10 scaled
                         linearly between threshold and the day's max.
  - big_prints_top10[] — for the side panel (all actions, top 10 by amount).
  - summary{}          — minute count, # marked minutes, # BIG prints.

FLOW BASIS (what drives the ₹ amount per strike-minute):
    'volume' (default) — traded turnover (Δvolume × price). This is the
        reference HFT indicator's method, established by reverse-engineering
        its published fund-flow: the CE/PE balance matches VOLUME (calls trade
        more), not ΔOI (puts write more). Every trade is writing or buying by
        the price-aggressor rule below — no OI gate.
    'oi' — net new positioning (|ΔOI| × price). Our original measure; the
        forward-return tracker is pinned to this for series stability.
        Writing/buying require ΔOI > 0; falling OI is covering/unwinding.

CLASSIFICATION (per strike, per minute):
    price < 0  → WRITING  (PE = put writing / bullish; CE = call writing / bearish)
    price > 0  → BUYING   (CE = call buying / bullish; PE = put buying / bearish)
    (oi basis only) ΔOI < 0 & price > 0 → SHORT COVERING; ΔOI < 0 & price < 0 → LONG UNWINDING

AMOUNTS, FIXED IN THIS REVISION:
    Kite's `oi` field for NSE F&O is reported in *share-equivalent quantity*,
    not in contracts. The previous code multiplied by lot_size, which
    inflated every amount by ~75× for NIFTY and ~15× for BANKNIFTY (e.g.
    ₹244,000 cr for one minute's ΔOI on a single PE strike — clearly wrong).
    Now we use ΔOI directly:
        premium  = |ΔOI| × option_LTP
        notional = |ΔOI| × spot
        margin   = notional × MARGIN_PCT
    The display of "ΔOI in lots" in the side panel uses |ΔOI| / lot_size.

SCORE (per minute):
    atm_strikes = ATM ± atm_band
    basis 'combined' (default): bullish = PE writing + CE buying;
                                bearish = CE writing + PE buying
    basis 'writing':            bullish = PE writing; bearish = CE writing
    dominant = max(bullish, bearish)
    threshold: absolute (fixed ₹cr) or adaptive (trailing-60-min mean + 2σ,
               current minute excluded — live-equivalent, no lookahead)
    if dominant < threshold: no marker
    else: score = clamp(1 + 9 * (dominant/threshold − 1) / 3, 1, 10)
          (ratio scaling: 1× threshold → 1, ≥4× threshold → 10)
"""
from __future__ import annotations

import sqlite3
import statistics
from collections import defaultdict, deque
from datetime import datetime
from typing import Optional

CRORE = 1e7
DEFAULT_MARGIN_PCT = 0.12

KNOWN_LOT_SIZES = {"NIFTY": 75, "BANKNIFTY": 15}


def _ts_to_unix(ts_iso: str) -> int:
    """Return a unix value that, when Lightweight Charts displays it as UTC,
    reads as IST on the axis. Shift by +5h30m."""
    return int(datetime.fromisoformat(ts_iso).timestamp() + 5.5 * 3600)


def aggregate_day(
    conn: sqlite3.Connection,
    underlying: str,
    date: str,
    mode: str = "notional",
    score_threshold_cr: float = 50.0,
    n: int = 10,
    atm_band: int = 2,
    margin_pct: float = DEFAULT_MARGIN_PCT,
    lot_size: Optional[int] = None,
    threshold_mode: str = "absolute",
    score_basis: str = "combined",
    collapse_episodes: bool = True,
    cooldown_minutes: int = 10,
    roll_window_minutes: int = 20,
    flow_basis: str = "volume",
    trend_window_minutes: int = 1,
) -> dict:
    """threshold_mode:
        'absolute' — score/print threshold is score_threshold_cr, fixed.
        'adaptive' — per-minute threshold = mean + 2σ of the TRAILING 60
                     anchored minutes' dominant flow (floored at 0.5cr; falls
                     back to score_threshold_cr during the first few minutes).
                     Trailing-only stats mean the score you'd see live at
                     12:41 equals the score replay shows — no lookahead.

    score_basis:
        'combined' — bullish = PE writing + CE buying; bearish = CE writing +
                     PE buying. Marker side is 'bullish'/'bearish'. This is
                     the basis the reference HFT Algo Scanner appears to use
                     (its callouts pair put-writing with call-buying).
        'writing'  — original spec: only writing flows drive the score; side
                     stays 'put_writing'/'call_writing' (forward-return
                     tracking uses this for series stability).

    Score scaling is ratio-based, not day-max-based (which would also be
    lookahead): score 1 at the threshold, score 10 at ≥4× the threshold."""
    if mode not in ("premium", "notional", "margin"):
        raise ValueError(f"unknown mode: {mode}")
    if threshold_mode not in ("absolute", "adaptive"):
        raise ValueError(f"unknown threshold_mode: {threshold_mode}")
    if score_basis not in ("writing", "combined"):
        raise ValueError(f"unknown score_basis: {score_basis}")
    if flow_basis not in ("volume", "oi"):
        raise ValueError(f"unknown flow_basis: {flow_basis}")
    if trend_window_minutes < 1:
        raise ValueError(f"trend_window_minutes must be >= 1: {trend_window_minutes}")
    if lot_size is None:
        lot_size = KNOWN_LOT_SIZES.get(underlying, 75)

    threshold_rs = float(score_threshold_cr) * CRORE
    # In adaptive mode we can't know the final threshold until the full walk
    # is done, so collect candidate prints above a low floor and post-filter.
    PRINT_COLLECT_FLOOR_RS = min(threshold_rs, 1.0 * CRORE)

    cur = conn.execute(
        "SELECT ts, spot, strike, opt_type, ltp, oi, volume "
        "FROM chain_snapshot "
        "WHERE underlying = ? AND DATE(ts) = ? "
        "ORDER BY ts, strike, opt_type",
        (underlying, date),
    )
    rows = cur.fetchall()

    candle_cur = conn.execute(
        "SELECT ts, open, high, low, close, volume "
        "FROM underlying_candle "
        "WHERE underlying = ? AND DATE(ts) = ? "
        "ORDER BY ts",
        (underlying, date),
    )
    candle_rows = candle_cur.fetchall()
    candles_by_minute = {c["ts"]: c for c in candle_rows}

    empty_result = {
        "underlying": underlying, "date": date, "mode": mode,
        "score_threshold_cr": score_threshold_cr,
        "threshold_mode": threshold_mode,
        "score_basis": score_basis,
        "flow_basis": flow_basis,
        "trend_window_minutes": trend_window_minutes,
        "effective_threshold_cr": score_threshold_cr,
        "n": n, "atm_band": atm_band,
        "lot_size": lot_size, "strike_step": None,
        "candles": [], "score_markers": [], "histogram": [],
        "regime": [], "fund_flow": None,
        "bull_stats": None, "bear_stats": None, "big_prints_top10": [],
        "summary": {"total_minutes": 0, "n_unique_strikes": 0,
                    "n_score_markers": 0, "n_big_prints": 0,
                    "has_ohlc": False},
    }
    if not rows and not candle_rows:
        return empty_result

    # Group chain by minute
    minutes: dict = defaultdict(list)
    spot_at_minute: dict = {}
    for r in rows:
        minutes[r["ts"]].append(r)
        spot_at_minute[r["ts"]] = r["spot"]
    sorted_minutes = sorted(minutes.keys())
    all_strikes = sorted({r["strike"] for r in rows})

    strike_step = 50
    if len(all_strikes) >= 2:
        diffs = sorted({all_strikes[i + 1] - all_strikes[i] for i in range(len(all_strikes) - 1)})
        diffs = [d for d in diffs if d > 0]
        if diffs:
            strike_step = diffs[0]

    prev_oi: dict = {}
    prev_ltp: dict = {}
    prev_vol: dict = {}
    # Rolling LTP history per contract for the trend-based write/buy split.
    # `trend_window_minutes` widens the price-direction lens from the 1-minute
    # tick to a multi-minute trend. DEFAULT 1 = the original 1-min tick.
    #
    # Why default 1: an early throwaway analysis suggested a ~20-min trend
    # matched the reference indicator's directional decisiveness, but running
    # the *production* path (which is premium/notional-weighted and accumulates
    # only over the ATM score-band, unlike that standalone) against the stored
    # Jun-15 fund-flow shows the 1-min tick is the CLOSEST fit and every longer
    # window pulls away from his published buckets. So the longer window is
    # kept only as an experimental knob, not the default. deque[0] is ≈
    # trend_window in-band minutes ago.
    trend_w = max(1, int(trend_window_minutes))
    ltp_window: dict = {}

    # Per-minute pressures, indexed by the END timestamp (ts when we observed
    # the change). After the walk we re-key by the START timestamp so that
    # score markers align time-wise with the corresponding underlying candle.
    pressure_by_minute: dict = {}  # ts -> {"put": ..., "call": ...}
    big_prints: list = []

    for i, ts in enumerate(sorted_minutes):
        rows_this_min = minutes[ts]
        spot = spot_at_minute[ts]

        atm = min(all_strikes, key=lambda s: abs(s - spot))
        atm_idx = all_strikes.index(atm)
        big_lo = max(0, atm_idx - n)
        big_hi = min(len(all_strikes), atm_idx + n + 1)
        big_strikes = set(all_strikes[big_lo:big_hi])
        score_lo = max(0, atm_idx - atm_band)
        score_hi = min(len(all_strikes), atm_idx + atm_band + 1)
        score_strikes = set(all_strikes[score_lo:score_hi])

        put_writing_atm = 0.0
        call_writing_atm = 0.0
        put_buying_atm = 0.0
        call_buying_atm = 0.0

        for r in rows_this_min:
            strike = r["strike"]
            if strike not in big_strikes:
                continue
            opt_type = r["opt_type"]
            ltp = r["ltp"]
            oi = r["oi"]
            vol = r["volume"]
            if ltp is None or oi is None:
                continue

            key = (strike, opt_type)
            p_oi = prev_oi.get(key)
            p_ltp = prev_ltp.get(key)
            p_vol = prev_vol.get(key)
            prev_oi[key] = oi
            prev_ltp[key] = ltp
            prev_vol[key] = vol
            # Rolling LTP history for the trend-based write/buy split. Appended
            # on EVERY in-band observation — including this contract's first,
            # which the guard below skips — so window[0] is a genuinely prior
            # price. deque holds [t-trend_w, …, t-1, t], so window[0] is
            # ≈ trend_w in-band minutes ago. The 1-min tick (`d_ltp`) is too
            # noisy and splits a strike's volume ~50/50; the trend recovers the
            # reference indicator's directional decisiveness. With
            # trend_window_minutes=1 the deque is [t-1, t] and trend == d_ltp
            # exactly, so the OI/recorder path is bit-for-bit unchanged.
            w = ltp_window.get(key)
            if w is None:
                w = deque(maxlen=trend_w + 1)
                ltp_window[key] = w
            w.append(ltp)
            if p_oi is None or p_ltp is None:
                continue

            d_oi = oi - p_oi
            d_ltp = ltp - p_ltp
            trend = ltp - w[0]
            # Per-minute traded volume (Kite volume is cumulative for the day).
            d_vol = (vol - p_vol) if (vol is not None and p_vol is not None) else 0
            if d_vol < 0:
                d_vol = 0

            # Quantity that drives the ₹ amount depends on flow_basis:
            #   'volume' (default, = reference indicator) → traded turnover.
            #     Reverse-engineered from his published fund-flow: his CE/PE
            #     balance matches VOLUME (calls trade more), not ΔOI (puts
            #     write more). Classified purely by price direction.
            #   'oi' → our original net-positioning measure (ΔOI).
            if flow_basis == "volume":
                qty = d_vol
            else:
                qty = abs(d_oi)
            if qty == 0 or trend == 0:
                continue

            premium = qty * ltp
            notional = qty * spot
            margin = notional * margin_pct
            amount = {"premium": premium, "notional": notional, "margin": margin}[mode]

            # Classification. For OI basis, writing/buying require OI to be
            # rising (new positions); falling OI is covering/unwinding (not a
            # bucket). For VOLUME basis every trade is writing or buying by the
            # price-aggressor rule — there's no OI gate.
            # Direction comes from the `trend` (see above), not the 1-min tick.
            oi_gate = (d_oi > 0) if flow_basis == "oi" else True
            if oi_gate and trend < 0:
                action = "put_writing" if opt_type == "PE" else "call_writing"
                if strike in score_strikes:
                    if opt_type == "PE":
                        put_writing_atm += amount
                    else:
                        call_writing_atm += amount
            elif oi_gate and trend > 0:
                action = "put_buying" if opt_type == "PE" else "call_buying"
                if strike in score_strikes:
                    if opt_type == "PE":
                        put_buying_atm += amount
                    else:
                        call_buying_atm += amount
            elif d_oi < 0 and trend > 0:
                action = "short_covering"
            else:
                action = "long_unwinding"

            if amount >= PRINT_COLLECT_FLOOR_RS:
                big_prints.append({
                    "time": _ts_to_unix(ts),
                    "ts_iso": ts,
                    "strike": strike,
                    "opt_type": opt_type,
                    "action": action,
                    "delta_oi_lots": int(round(d_oi / max(lot_size, 1))),
                    "traded_lots": int(round(d_vol / max(lot_size, 1))),
                    "amount_rs": amount,
                    "amount_cr": amount / CRORE,
                    "premium": premium,
                    "notional": notional,
                    "margin": margin,
                    "spot": spot,
                    "ltp": ltp,
                })

        # Activity observed at `ts` actually happened during the previous
        # minute. Anchor pressure to that previous ts so the score marker
        # lines up with the candle of that minute.
        if i > 0:
            anchor_ts = sorted_minutes[i - 1]
            pressure_by_minute[anchor_ts] = {
                "put": put_writing_atm,
                "call": call_writing_atm,
                "put_buy": put_buying_atm,
                "call_buy": call_buying_atm,
            }

    sorted_pressure = sorted(pressure_by_minute.items())

    def _basis_flows(p):
        """(bullish_rs, bearish_rs) under the selected score basis."""
        if score_basis == "combined":
            return p["put"] + p["call_buy"], p["call"] + p["put_buy"]
        return p["put"], p["call"]

    # Rolling adaptive threshold — stats over the TRAILING window only, and
    # the current minute is appended AFTER its threshold is computed, so a
    # spike never raises the bar it is judged against. The first few minutes
    # fall back to the absolute floor. This is intentionally live-equivalent:
    # replaying a day gives the same thresholds the live page would have had.
    #
    # Window length matters a lot: the market-open flow spike is genuinely the
    # day's largest, so a long window keeps the bar elevated for its full
    # length and suppresses every midday signal (the symptom: all markers
    # cluster at 09:15). A short window lets the opening spike age out fast so
    # midday turning points clear the bar — matching the reference indicator's
    # ~25-min-after-open first signal. Default 20 min.
    ROLL_WINDOW = max(5, roll_window_minutes)
    MIN_SAMPLES = 5
    # Score 10 only at 10× the threshold, so the 1-10 scale SPREADS the way
    # the reference indicator's does: a 2× signal ≈ 2, a 4× ≈ 4, and 10 is
    # reserved for genuine monsters. With the old 4× cap almost every signal
    # pegged at 10 (vs the reference's typical 2-4).
    RATIO_FOR_10 = 10.0
    window = deque(maxlen=ROLL_WINDOW)
    thr_by_minute = {}
    dom_by_minute = {}
    for ts, p in sorted_pressure:
        bull_rs, bear_rs = _basis_flows(p)
        dom = max(bull_rs, bear_rs)
        dom_by_minute[ts] = (dom, bull_rs, bear_rs)
        if threshold_mode == "adaptive" and len(window) >= MIN_SAMPLES:
            # Median + 2·MAD is robust to a single opening outlier in a way
            # mean + 2σ is not — one ₹62cr open shouldn't define "normal" for
            # the next 20 minutes. Falls back to mean/σ shape via MAD scaling.
            try:
                vals = sorted(window)
                med = statistics.median(vals)
                mad = statistics.median([abs(v - med) for v in vals])
                # 1.4826 scales MAD to be a σ-equivalent for normal data.
                thr = max(med + 2 * 1.4826 * mad, 0.5 * CRORE)
            except statistics.StatisticsError:
                thr = threshold_rs
        else:
            thr = threshold_rs
        thr_by_minute[ts] = thr
        window.append(dom)

    effective_threshold_rs = (
        thr_by_minute[sorted_pressure[-1][0]] if sorted_pressure else threshold_rs
    )

    # Post-filter prints by their own minute's threshold. Prints carry the
    # OBSERVATION minute; pressure/thresholds are keyed by the ANCHOR minute
    # (one minute earlier), so look up at time-60s with the floor as fallback.
    thr_by_unix = {_ts_to_unix(ts): thr for ts, thr in thr_by_minute.items()}
    big_prints = [
        b for b in big_prints
        if b["amount_rs"] >= thr_by_unix.get(b["time"] - 60, threshold_rs)
    ]

    # ATM flow histogram — one entry per anchored minute, all four flows in
    # ₹ crore. The UI stacks bullish flows above zero (PE writing light green
    # + CE buying deep green) and bearish flows below zero (CE writing light
    # red + PE buying deep red), matching the reference indicator's language.
    #
    # `significant` marks minutes whose dominant (score-basis) flow reaches at
    # least half of that minute's threshold. The UI draws flow bars ONLY for
    # significant minutes — the reference indicator's pane is empty most of
    # the time, and that sparseness is what makes its events readable. The
    # full series is still emitted (stats, fund-flow, regime all use it).
    FLOW_RENDER_FRACTION = 0.5
    histogram = [
        {
            "time": _ts_to_unix(ts),
            "put_writing_cr": p["put"] / CRORE,
            "call_writing_cr": p["call"] / CRORE,
            "put_buying_cr": p["put_buy"] / CRORE,
            "call_buying_cr": p["call_buy"] / CRORE,
            "bullish_cr": (p["put"] + p["call_buy"]) / CRORE,
            "bearish_cr": (p["call"] + p["put_buy"]) / CRORE,
            "significant": dom_by_minute[ts][0] >= FLOW_RENDER_FRACTION * thr_by_minute[ts],
        }
        for ts, p in sorted_pressure
    ]

    # Score markers — ratio-based scaling (1 at threshold, 10 at ≥4×) so the
    # score is computable live without knowing the day's eventual maximum.
    raw_hits = []
    for ts, p in sorted_pressure:
        dom, bull_rs, bear_rs = dom_by_minute[ts]
        thr = thr_by_minute[ts]
        if dom < thr or thr <= 0:
            continue
        ratio = dom / thr
        raw = 1 + 9 * (ratio - 1) / (RATIO_FOR_10 - 1)
        score = max(1, min(10, int(round(raw))))
        if score_basis == "combined":
            side = "bullish" if bull_rs >= bear_rs else "bearish"
        else:
            side = "put_writing" if bull_rs >= bear_rs else "call_writing"
        raw_hits.append({
            "time": _ts_to_unix(ts),
            "ts_iso": ts,
            "score": score,
            "side": side,
            "amount_cr": dom / CRORE,
            "threshold_cr": thr / CRORE,
            "bull_cr": bull_rs / CRORE,
            "bear_cr": bear_rs / CRORE,
        })

    # Collapse bursts into EPISODES — one marker per burst, at its peak.
    # Consecutive above-threshold minutes of the same side with gaps ≤ 3 min
    # belong to one episode; a flow burst that persists for six minutes is one
    # event, not six. This is what makes the reference indicator look sparse:
    # it marks bursts, not minutes. Tracking callers pass
    # collapse_episodes=False to keep the raw per-minute series stable.
    EPISODE_GAP_S = 3 * 60
    if collapse_episodes and raw_hits:
        episodes = []
        cur = [raw_hits[0]]
        for h in raw_hits[1:]:
            if h["side"] == cur[-1]["side"] and h["time"] - cur[-1]["time"] <= EPISODE_GAP_S:
                cur.append(h)
            else:
                episodes.append(cur)
                cur = [h]
        episodes.append(cur)
        episode_peaks = []
        for ep in episodes:
            peak = dict(max(ep, key=lambda h: h["amount_cr"]))
            peak["episode_minutes"] = len(ep)
            episode_peaks.append(peak)

        # Cooldown / non-maximum suppression — PER SIDE. Episode collapsing
        # handles consecutive SAME-side minutes; this further thins a choppy
        # run of same-side episodes a few minutes apart, keeping only the
        # strongest in each cooldown window. CRITICAL: suppression is
        # side-scoped. A strong Buy must NOT silence a nearby Sell — they're
        # opposite signals, and the reference indicator routinely shows a Buy
        # then a Sell a few minutes later (e.g. open Buy:2 then 09:18 Sell:3).
        # The earlier global (side-agnostic) version let a giant opening Buy
        # shadow every signal for 10 minutes, leaving us with a lone marker
        # while the reference showed four.
        COOLDOWN_S = cooldown_minutes * 60
        kept = []
        for i, m in enumerate(episode_peaks):
            dominated = False
            for j, other in enumerate(episode_peaks):
                if i == j:
                    continue
                if other["side"] != m["side"]:
                    continue  # opposite-side markers never suppress each other
                if abs(other["time"] - m["time"]) <= COOLDOWN_S:
                    # Stronger neighbour, or equal-strength earlier one, wins.
                    if (other["amount_cr"] > m["amount_cr"] or
                            (other["amount_cr"] == m["amount_cr"] and other["time"] < m["time"])):
                        dominated = True
                        break
            if not dominated:
                kept.append(m)
        score_markers = kept
    else:
        score_markers = raw_hits

    # Regime — rolling 30-minute net flow (always all four flows, regardless
    # of score basis: regime is context, not signal). Neutral dead-zone: only
    # call a side when the window has enough samples AND the net is at least
    # 15% of the gross flow — otherwise the early-session window flips sign
    # minute-to-minute and the shading renders as alternating noise stripes
    # instead of zones.
    REGIME_WINDOW = 30
    REGIME_MIN_SAMPLES = 10
    REGIME_DEADZONE = 0.15
    flow_window = deque(maxlen=REGIME_WINDOW)  # (bull_rs, bear_rs)
    regime = []
    for ts, p in sorted_pressure:
        flow_window.append((p["put"] + p["call_buy"], p["call"] + p["put_buy"]))
        bull_sum = sum(f[0] for f in flow_window)
        bear_sum = sum(f[1] for f in flow_window)
        net_sum = bull_sum - bear_sum
        gross = bull_sum + bear_sum
        if len(flow_window) < REGIME_MIN_SAMPLES or gross <= 0 or abs(net_sum) < REGIME_DEADZONE * gross:
            side = "neutral"
        else:
            side = "bull" if net_sum > 0 else "bear"
        regime.append({
            "time": _ts_to_unix(ts),
            "net30_cr": round(net_sum / CRORE, 2),
            "side": side,
        })

    # Fund-flow summary — rolling last-60-minutes + day totals per flow,
    # mirroring the reference indicator's table.
    def _sum_flows(items):
        out = {"put_writing_cr": 0.0, "call_writing_cr": 0.0,
               "call_buying_cr": 0.0, "put_buying_cr": 0.0}
        for _, p in items:
            out["put_writing_cr"] += p["put"] / CRORE
            out["call_writing_cr"] += p["call"] / CRORE
            out["call_buying_cr"] += p["call_buy"] / CRORE
            out["put_buying_cr"] += p["put_buy"] / CRORE
        return {k: round(v, 1) for k, v in out.items()}

    fund_flow = {
        "last_60min": _sum_flows(sorted_pressure[-60:]),
        "day": _sum_flows(sorted_pressure),
        "n_minutes": len(sorted_pressure),
    } if sorted_pressure else None

    # Candles — prefer real OHLC; fall back to spot-only synthetic bars
    candles_out = []
    has_ohlc = bool(candles_by_minute)
    if has_ohlc:
        for c in candle_rows:
            if c["open"] is None or c["high"] is None or c["low"] is None or c["close"] is None:
                continue
            candles_out.append({
                "time": _ts_to_unix(c["ts"]),
                "open": float(c["open"]),
                "high": float(c["high"]),
                "low": float(c["low"]),
                "close": float(c["close"]),
            })
    else:
        # Synthetic O=H=L=C=spot for legacy days without OHLC
        for ts in sorted_minutes:
            sp = spot_at_minute[ts]
            candles_out.append({
                "time": _ts_to_unix(ts),
                "open": sp, "high": sp, "low": sp, "close": sp,
            })

    top10 = sorted(big_prints, key=lambda b: -b["amount_rs"])[:10]

    # Day-wide stats for the lower-pane reference lines (1σ / 2σ above the
    # mean writing volume in ₹cr). Computed across ALL anchored minutes
    # including zeros — represents "what's a typical writing pressure today".
    def _stats(values):
        if len(values) < 5:
            return None
        try:
            m = statistics.mean(values)
            sd = statistics.stdev(values)
        except statistics.StatisticsError:
            return None
        return {"mean": round(m, 3), "sd": round(sd, 3),
                "p1sd": round(m + sd, 3), "p2sd": round(m + 2 * sd, 3)}

    # SD bands on the TOTAL bullish / bearish flow (writing + buying), since
    # that's what the stacked histogram displays.
    bull_values = [h["bullish_cr"] for h in histogram]
    bear_values = [h["bearish_cr"] for h in histogram]
    bull_stats = _stats(bull_values)
    bear_stats = _stats(bear_values)

    max_dom_rs = max((v[0] for v in dom_by_minute.values()), default=None)

    return {
        "underlying": underlying,
        "date": date,
        "mode": mode,
        "score_threshold_cr": score_threshold_cr,
        "threshold_mode": threshold_mode,
        "score_basis": score_basis,
        "flow_basis": flow_basis,
        "trend_window_minutes": trend_window_minutes,
        "effective_threshold_cr": round(effective_threshold_rs / CRORE, 2),
        "n": n,
        "atm_band": atm_band,
        "lot_size": lot_size,
        "strike_step": strike_step,
        "candles": candles_out,
        "score_markers": score_markers,
        "histogram": histogram,
        "regime": regime,
        "fund_flow": fund_flow,
        "bull_stats": bull_stats,
        "bear_stats": bear_stats,
        "big_prints_top10": top10,
        "summary": {
            "total_minutes": len(sorted_minutes),
            "first_ts": sorted_minutes[0] if sorted_minutes else None,
            "last_ts": sorted_minutes[-1] if sorted_minutes else None,
            "n_unique_strikes": len(all_strikes),
            "n_score_markers": len(score_markers),
            "n_big_prints": len(big_prints),
            "has_ohlc": has_ohlc,
            "max_pressure_cr": round(max_dom_rs / CRORE, 1) if max_dom_rs else None,
        },
    }


def aggregate_range(conn, underlying, dates, **kwargs):
    """Run aggregate_day for each date in `dates` (chronological order) and
    concatenate into one continuous series for a multi-day chart.

    Each day is computed INDEPENDENTLY — the rolling threshold, episode
    collapse, and cooldown all reset at every market open. That's deliberate:
    the overnight gap makes a cross-midnight ΔOI meaningless, and each session
    is its own regime. We only concatenate the rendered outputs (candles,
    markers, flow bars, regime) so the chart flows continuously; Lightweight
    Charts collapses the overnight gaps since it spaces bars by index.

    fund_flow reflects the MOST RECENT day (the actionable "today"); the
    BIG-print list is merged across the whole range and re-topped to 10.
    """
    candles, score_markers, histogram, regime = [], [], [], []
    all_prints = []
    fund_flow = None
    last_day_summary = None
    strike_step = lot_size = None
    n_unique = 0
    any_ohlc = False
    max_dom = 0.0

    for d in dates:
        r = aggregate_day(conn, underlying, d, **kwargs)
        candles += r.get("candles", [])
        score_markers += r.get("score_markers", [])
        histogram += r.get("histogram", [])
        regime += r.get("regime", [])
        all_prints += r.get("big_prints_top10", [])
        if r.get("fund_flow"):
            fund_flow = r["fund_flow"]            # last non-empty wins (most recent)
        strike_step = r.get("strike_step") or strike_step
        lot_size = r.get("lot_size") or lot_size
        s = r.get("summary", {})
        n_unique = max(n_unique, s.get("n_unique_strikes", 0))
        any_ohlc = any_ohlc or s.get("has_ohlc", False)
        if s.get("max_pressure_cr"):
            max_dom = max(max_dom, s["max_pressure_cr"])
        last_day_summary = s

    top10 = sorted(all_prints, key=lambda b: -b.get("amount_cr", 0))[:10]

    return {
        "underlying": underlying,
        "dates": dates,
        "date": dates[-1] if dates else "",
        "continuous": True,
        "mode": kwargs.get("mode"),
        "score_threshold_cr": kwargs.get("score_threshold_cr"),
        "threshold_mode": kwargs.get("threshold_mode"),
        "score_basis": kwargs.get("score_basis"),
        "flow_basis": kwargs.get("flow_basis", "volume"),
        "trend_window_minutes": kwargs.get("trend_window_minutes", 1),
        "n": kwargs.get("n"),
        "atm_band": kwargs.get("atm_band"),
        "lot_size": lot_size,
        "strike_step": strike_step,
        "candles": candles,
        "score_markers": score_markers,
        "histogram": histogram,
        "regime": regime,
        "fund_flow": fund_flow,
        "bull_stats": None,   # σ bands are per-day; omitted on the merged view
        "bear_stats": None,
        "big_prints_top10": top10,
        "summary": {
            "total_minutes": len(candles),
            "n_days": len(dates),
            "n_unique_strikes": n_unique,
            "n_score_markers": len(score_markers),
            "n_big_prints": len(all_prints),
            "has_ohlc": any_ohlc,
            "max_pressure_cr": max_dom or None,
        },
    }
