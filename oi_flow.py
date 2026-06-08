"""
OI Flow aggregation.

For one (underlying, trading_day), this module walks the per-minute chain_snapshot
rows and produces:

  - candles[]      — minute-by-minute spot of the underlying (line series; the
                     recorder snapshots spot once per minute so we don't have
                     real OHLC bars — only close)
  - histogram[]    — net writing-pressure per minute, in INR-crore, signed
                     (positive = bullish = put-writing > call-writing)
  - big_writing_markers[]  — per-minute markers for BIG put-writing /
                              call-writing prints (for the candle pane)
  - big_prints_top10[]     — that day's top 10 BIG prints across all actions
                              (for the right-hand panel)

Classification per (strike, minute) — from the spec:
    ΔOI > 0 & Δprice < 0  → WRITING  (CE = call writing / bearish;
                                       PE = put writing / bullish)
    ΔOI > 0 & Δprice > 0  → BUYING   (CE = call buying / bullish;
                                       PE = put buying / bearish)
    ΔOI < 0 & Δprice > 0  → SHORT COVERING
    ΔOI < 0 & Δprice < 0  → LONG UNWINDING

Amount of |ΔOI|, three modes (UI configurable, default: notional):
    premium  = |ΔOI| × lot_size × option_LTP
    notional = |ΔOI| × lot_size × spot
    margin   = notional × MARGIN_PCT   (default 0.12, label as "estimate")

Kite's `oi` field is reported in CONTRACTS (lots), so we multiply by lot_size
to get share-equivalent quantity before pricing.

A strike-minute is BIG when its chosen-mode amount ≥ BIG_CR × 1e7.
"""
from __future__ import annotations

import sqlite3
from collections import defaultdict
from datetime import datetime
from typing import Optional

CRORE = 1e7
DEFAULT_MARGIN_PCT = 0.12

# Known NIFTY/BANKNIFTY lot sizes as a fallback only — actual size is read
# from instruments data at recorder/run time and surfaced here via the
# `lot_size` argument. Update if the exchange revises.
KNOWN_LOT_SIZES = {"NIFTY": 75, "BANKNIFTY": 15}


def _ts_to_unix(ts_iso: str) -> int:
    """Convert IST ISO timestamp to UTC unix seconds for the chart library."""
    return int(datetime.fromisoformat(ts_iso).timestamp())


def aggregate_day(
    conn: sqlite3.Connection,
    underlying: str,
    date: str,
    mode: str = "notional",
    big_cr: float = 50.0,
    n: int = 10,
    margin_pct: float = DEFAULT_MARGIN_PCT,
    lot_size: Optional[int] = None,
) -> dict:
    """Build minute-aggregates + BIG-print list for one (underlying, date)."""
    if mode not in ("premium", "notional", "margin"):
        raise ValueError(f"unknown mode: {mode}")
    if lot_size is None:
        lot_size = KNOWN_LOT_SIZES.get(underlying, 75)

    big_threshold = float(big_cr) * CRORE

    cur = conn.execute(
        "SELECT ts, spot, strike, opt_type, ltp, oi "
        "FROM chain_snapshot "
        "WHERE underlying = ? AND DATE(ts) = ? "
        "ORDER BY ts, strike, opt_type",
        (underlying, date),
    )
    rows = cur.fetchall()

    empty_result = {
        "underlying": underlying, "date": date, "mode": mode,
        "big_cr": big_cr, "n": n, "lot_size": lot_size, "strike_step": None,
        "candles": [], "histogram": [], "big_writing_markers": [],
        "big_prints_top10": [],
        "summary": {"total_minutes": 0, "n_unique_strikes": 0, "n_big_prints": 0},
    }
    if not rows:
        return empty_result

    # Group by minute
    minutes: dict = defaultdict(list)
    spot_at_minute: dict = {}
    for r in rows:
        minutes[r["ts"]].append(r)
        spot_at_minute[r["ts"]] = r["spot"]

    sorted_minutes = sorted(minutes.keys())
    all_strikes = sorted({r["strike"] for r in rows})

    # Detect strike step from sorted strike list
    strike_step = 50
    if len(all_strikes) >= 2:
        diffs = sorted({all_strikes[i + 1] - all_strikes[i] for i in range(len(all_strikes) - 1)})
        diffs = [d for d in diffs if d > 0]
        if diffs:
            strike_step = diffs[0]

    # Stateful walk: track per-contract previous OI + LTP to compute deltas
    prev_oi: dict = {}    # (strike, opt_type) -> oi
    prev_ltp: dict = {}   # (strike, opt_type) -> ltp

    candles = []
    histogram = []
    big_prints: list = []   # all BIG prints (any action)

    for ts in sorted_minutes:
        rows_this_min = minutes[ts]
        spot = spot_at_minute[ts]
        ts_unix = _ts_to_unix(ts)

        # ATM ± N strikes for THIS minute (ATM can shift through the day)
        atm = min(all_strikes, key=lambda s: abs(s - spot))
        atm_idx = all_strikes.index(atm)
        lo = max(0, atm_idx - n)
        hi = min(len(all_strikes), atm_idx + n + 1)
        target_strikes = set(all_strikes[lo:hi])

        bullish_writing_amt = 0.0  # Σ put-writing amounts
        bearish_writing_amt = 0.0  # Σ call-writing amounts

        for r in rows_this_min:
            strike = r["strike"]
            if strike not in target_strikes:
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
                # First observation of this contract today = baseline
                continue

            d_oi = oi - p_oi
            d_ltp = ltp - p_ltp
            if d_oi == 0 or d_ltp == 0:
                continue  # ambiguous — skip

            # ΔOI is in contracts; convert to share-equivalent for ₹ amounts
            abs_d_oi_shares = abs(d_oi) * lot_size
            premium = abs_d_oi_shares * ltp
            notional = abs_d_oi_shares * spot
            margin = notional * margin_pct

            amount = {"premium": premium, "notional": notional, "margin": margin}[mode]

            # Classify
            if d_oi > 0 and d_ltp < 0:
                action = "put_writing" if opt_type == "PE" else "call_writing"
                if opt_type == "PE":
                    bullish_writing_amt += amount
                else:
                    bearish_writing_amt += amount
            elif d_oi > 0 and d_ltp > 0:
                action = "put_buying" if opt_type == "PE" else "call_buying"
            elif d_oi < 0 and d_ltp > 0:
                action = "short_covering"
            else:
                action = "long_unwinding"

            if amount >= big_threshold:
                big_prints.append({
                    "time": ts_unix,
                    "ts_iso": ts,
                    "strike": strike,
                    "opt_type": opt_type,
                    "action": action,
                    "delta_oi_lots": int(d_oi),
                    "amount_rs": amount,
                    "amount_cr": amount / CRORE,
                    "premium": premium,
                    "notional": notional,
                    "margin": margin,
                    "spot": spot,
                    "ltp": ltp,
                })

        net_cr = (bullish_writing_amt - bearish_writing_amt) / CRORE

        candles.append({"time": ts_unix, "value": spot})
        histogram.append({
            "time": ts_unix,
            "value": net_cr,
            "bullish_cr": bullish_writing_amt / CRORE,
            "bearish_cr": bearish_writing_amt / CRORE,
        })

    # Markers on the candle pane: only BIG writing prints (per spec)
    big_writing_markers = [
        {
            "time": b["time"],
            "ts_iso": b["ts_iso"],
            "strike": b["strike"],
            "opt_type": b["opt_type"],
            "action": b["action"],
            "delta_oi_lots": b["delta_oi_lots"],
            "amount_cr": b["amount_cr"],
            "premium": b["premium"],
            "notional": b["notional"],
            "margin": b["margin"],
        }
        for b in big_prints
        if b["action"] in ("put_writing", "call_writing")
    ]

    # Top 10 BIG prints by amount for the right-hand panel
    top10 = sorted(big_prints, key=lambda b: -b["amount_rs"])[:10]

    return {
        "underlying": underlying,
        "date": date,
        "mode": mode,
        "big_cr": big_cr,
        "n": n,
        "lot_size": lot_size,
        "strike_step": strike_step,
        "candles": candles,
        "histogram": histogram,
        "big_writing_markers": big_writing_markers,
        "big_prints_top10": top10,
        "summary": {
            "total_minutes": len(sorted_minutes),
            "first_ts": sorted_minutes[0] if sorted_minutes else None,
            "last_ts": sorted_minutes[-1] if sorted_minutes else None,
            "n_unique_strikes": len(all_strikes),
            "n_big_prints": len(big_prints),
        },
    }
