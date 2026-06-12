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

CLASSIFICATION (per strike, per minute), unchanged:
    ΔOI > 0 & Δprice < 0  → WRITING  (PE = put writing / bullish;
                                       CE = call writing / bearish)
    ΔOI > 0 & Δprice > 0  → BUYING
    ΔOI < 0 & Δprice > 0  → SHORT COVERING
    ΔOI < 0 & Δprice < 0  → LONG UNWINDING

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
    put_writing_rs  = Σ PE-writing amounts at atm_strikes
    call_writing_rs = Σ CE-writing amounts at atm_strikes
    dominant = max(put_writing_rs, call_writing_rs)
    if dominant < threshold_rs: no marker
    else: score = clamp(1 + 9 * (dominant - threshold) / (max_for_10 - threshold), 1, 10)
    max_for_10 is the day's max above threshold (self-calibrating).
"""
from __future__ import annotations

import sqlite3
import statistics
from collections import defaultdict
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
) -> dict:
    """threshold_mode:
        'absolute' — score/print threshold is score_threshold_cr, fixed.
        'adaptive' — threshold = mean + 2σ of the day's per-minute dominant
                     ATM writing (floored at 0.5cr). A quiet day where the
                     biggest minute is ₹6cr still scores its relative spikes;
                     a wild day self-raises so only true outliers mark."""
    if mode not in ("premium", "notional", "margin"):
        raise ValueError(f"unknown mode: {mode}")
    if threshold_mode not in ("absolute", "adaptive"):
        raise ValueError(f"unknown threshold_mode: {threshold_mode}")
    if lot_size is None:
        lot_size = KNOWN_LOT_SIZES.get(underlying, 75)

    threshold_rs = float(score_threshold_cr) * CRORE
    # In adaptive mode we can't know the final threshold until the full walk
    # is done, so collect candidate prints above a low floor and post-filter.
    PRINT_COLLECT_FLOOR_RS = min(threshold_rs, 1.0 * CRORE)

    cur = conn.execute(
        "SELECT ts, spot, strike, opt_type, ltp, oi "
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
        "effective_threshold_cr": score_threshold_cr,
        "n": n, "atm_band": atm_band,
        "lot_size": lot_size, "strike_step": None,
        "candles": [], "score_markers": [], "histogram": [],
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
            if ltp is None or oi is None:
                continue

            key = (strike, opt_type)
            p_oi = prev_oi.get(key)
            p_ltp = prev_ltp.get(key)
            prev_oi[key] = oi
            prev_ltp[key] = ltp
            if p_oi is None or p_ltp is None:
                continue

            d_oi = oi - p_oi
            d_ltp = ltp - p_ltp
            if d_oi == 0 or d_ltp == 0:
                continue

            # ΔOI from Kite is in SHARES (share-equivalent quantity), not
            # contracts. Do NOT multiply by lot_size — that was the bug.
            abs_d_oi = abs(d_oi)
            premium = abs_d_oi * ltp
            notional = abs_d_oi * spot
            margin = notional * margin_pct
            amount = {"premium": premium, "notional": notional, "margin": margin}[mode]

            if d_oi > 0 and d_ltp < 0:
                action = "put_writing" if opt_type == "PE" else "call_writing"
                if strike in score_strikes:
                    if opt_type == "PE":
                        put_writing_atm += amount
                    else:
                        call_writing_atm += amount
            elif d_oi > 0 and d_ltp > 0:
                action = "put_buying" if opt_type == "PE" else "call_buying"
                if strike in score_strikes:
                    if opt_type == "PE":
                        put_buying_atm += amount
                    else:
                        call_buying_atm += amount
            elif d_oi < 0 and d_ltp > 0:
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

    # Effective score threshold. Adaptive = mean + 2σ of the day's per-minute
    # dominant WRITING pressure (score stays writer-driven per the original
    # spec; buying flows are display-only context).
    dom_values = [max(p["put"], p["call"]) for p in pressure_by_minute.values()]
    effective_threshold_rs = threshold_rs
    if threshold_mode == "adaptive" and len(dom_values) >= 5:
        try:
            m = statistics.mean(dom_values)
            sd = statistics.stdev(dom_values)
            effective_threshold_rs = max(m + 2 * sd, 0.5 * CRORE)
        except statistics.StatisticsError:
            pass

    # Post-filter prints by the effective threshold (they were collected
    # above a low floor so adaptive thresholds below the absolute input
    # still have candidates to keep).
    big_prints = [b for b in big_prints if b["amount_rs"] >= effective_threshold_rs]

    # Day-max above threshold → max_for_10 (self-calibrating)
    above = [v for v in dom_values if v >= effective_threshold_rs]
    max_for_10 = max(above) if above else effective_threshold_rs

    # ATM flow histogram — one entry per anchored minute, all four flows in
    # ₹ crore. The UI stacks bullish flows above zero (PE writing light green
    # + CE buying deep green) and bearish flows below zero (CE writing light
    # red + PE buying deep red), matching the reference indicator's language.
    histogram = [
        {
            "time": _ts_to_unix(ts),
            "put_writing_cr": p["put"] / CRORE,
            "call_writing_cr": p["call"] / CRORE,
            "put_buying_cr": p["put_buy"] / CRORE,
            "call_buying_cr": p["call_buy"] / CRORE,
            "bullish_cr": (p["put"] + p["call_buy"]) / CRORE,
            "bearish_cr": (p["call"] + p["put_buy"]) / CRORE,
        }
        for ts, p in sorted(pressure_by_minute.items())
    ]

    score_markers = []
    for ts, p in sorted(pressure_by_minute.items()):
        put_rs = p["put"]
        call_rs = p["call"]
        dominant_side = "put_writing" if put_rs >= call_rs else "call_writing"
        dominant_rs = max(put_rs, call_rs)
        if dominant_rs < effective_threshold_rs:
            continue
        if max_for_10 > effective_threshold_rs:
            raw = 1 + 9 * (dominant_rs - effective_threshold_rs) / (max_for_10 - effective_threshold_rs)
        else:
            raw = 1
        score = max(1, min(10, int(round(raw))))
        score_markers.append({
            "time": _ts_to_unix(ts),
            "ts_iso": ts,
            "score": score,
            "side": dominant_side,
            "amount_cr": dominant_rs / CRORE,
            "put_cr": put_rs / CRORE,
            "call_cr": call_rs / CRORE,
        })

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

    return {
        "underlying": underlying,
        "date": date,
        "mode": mode,
        "score_threshold_cr": score_threshold_cr,
        "threshold_mode": threshold_mode,
        "effective_threshold_cr": round(effective_threshold_rs / CRORE, 2),
        "n": n,
        "atm_band": atm_band,
        "lot_size": lot_size,
        "strike_step": strike_step,
        "candles": candles_out,
        "score_markers": score_markers,
        "histogram": histogram,
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
            "max_pressure_cr": max_for_10 / CRORE if above else None,
        },
    }
