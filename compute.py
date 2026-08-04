"""
NIFTY Options dashboard — the data builder. One run = one full refresh.

Pipeline (main()):
  1. Fetch NIFTY / BANKNIFTY / INDIA VIX spot + candles from Kite Connect.
  2. Compute the four strategy indicators — no external TA library:
       BB-21 centerline (daily, index)   → Golden Goose, GG-LEAPS
       EMA-53           (daily, index)   → Nidhi Kalash
       CMF-21           (daily, FUTURES — the index has no volume) → Panther
       VWMA-21          (2h resample of 60m futures) → Ocean Treasure
  3. derive_signals(): price-vs-reference crossovers → bull/bear/hold per
     strategy; compute_calendar_flags(): rollover/build-day booleans.
  4. recommend.build_recommendations(): exact structures with live premiums
     for all 9 strategies (incl. always-on triple_calendar).
  5. paper.sync(): the paper-trading ledger — every actionable rec is assumed
     EXECUTED at 10 lots; marks open positions, executes monthly hedge rolls
     (OT T-4 / GG-LEAPS 18th), applies exit rules, settles expired legs;
     P&L is NET of a full transaction-cost model.
  6. Write ./data.json (+ Redis when REDIS_URL set) for index.html.

Invoked by: manual `python3 compute.py` (browser login if no cached token),
POST /refresh (headless subprocess), the dashboard's stale auto-refresh, and
the 15:16 IST Mon–Fri scheduler in app.py. HEADLESS via OPTIONS_HEADLESS=1
(exits 2 when no Kite session is cached — that day's run is simply skipped).
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd

from kite_auth import get_kite, get_kite_from_cache
from recommend import build_recommendations
from storage import store_data

HEADLESS = os.environ.get("OPTIONS_HEADLESS", "").lower() in ("1", "true", "yes")

ROOT = Path(__file__).parent
OUT = ROOT / "data.json"
IST = timezone(timedelta(hours=5, minutes=30))

# Kite tradingsymbols for the indices we need.
# `fut_name` is the NFO `name` field for the corresponding nearest-expiry
# future, used when an indicator (CMF, VWMA) needs volume.
INDICES = {
    "nifty":     ("NSE", "NIFTY 50",   "NIFTY"),
    "banknifty": ("NSE", "NIFTY BANK", "BANKNIFTY"),
    "indiavix":  ("NSE", "INDIA VIX",  None),
}


# ---------- indicator computation (no external TA library) ----------

def bb21_mid(close: pd.Series) -> pd.Series:
    """Bollinger middle band = 21-period SMA."""
    return close.rolling(window=21, min_periods=21).mean()


def ema53(close: pd.Series) -> pd.Series:
    return close.ewm(span=53, adjust=False, min_periods=53).mean()


def cmf21(high: pd.Series, low: pd.Series, close: pd.Series, vol: pd.Series) -> pd.Series:
    """Chaikin Money Flow, period 21."""
    rng = (high - low).replace(0, pd.NA)
    mfm = ((close - low) - (high - close)) / rng
    mfv = (mfm * vol).fillna(0)
    return mfv.rolling(window=21, min_periods=21).sum() / vol.rolling(window=21, min_periods=21).sum()


def vwma21(close: pd.Series, vol: pd.Series) -> pd.Series:
    """Volume-weighted moving average, period 21."""
    pv = (close * vol).rolling(window=21, min_periods=21).sum()
    v = vol.rolling(window=21, min_periods=21).sum()
    return pv / v


# ---------- Hilega Milega / SRT (directional read) ----------
# Source: podcast transcript of the indicator's author (Bihar-based trader,
# ~25 yrs; "Hilega Milega" released free in 2020, widely used on TradingView).
# Transcribed rules, implemented verbatim — NOT tuned by us:
#
#   RSI period 9 (he uses 9-10, not the standard 14, "to get the data a
#     little earlier than the world"; drops to 7 only when SRT <= 0.85).
#   RED LINE = WMA-21 *of the RSI series* (not of price). He tested all 11
#     MA types and picked weighted because it tracks the volume average
#     most closely — that is the whole point of the indicator.
#   STATE: RSI above the red line = buy side ("red line went inside");
#     RSI below it = sell side ("red line came up") -> stop buying.
#   FRESH-BUY FILTER: RSI must be > 55 AND have completed a V-turn up
#     through it. Below 55 is "in the water" — no buying at all. He is
#     emphatic that jumping in before the curve completes is THE reason
#     people say it doesn't work.
#   86 = profit-book line on whatever timeframe you trade.
#   OLD-LOW-BREAK: if it flips to buy and IMMEDIATELY flips back to sell,
#     the previous swing low breaks, "and badly" (his backtest: 8/10).
#   TIMEFRAME: day and above is reliable; intraday needs expertise.
#
#   SRT = last price / 124-period average (124 = half of ~248 trading days
#     = 6 months). DAILY timeframe only for the NIFTY zones. Range ~0.55-1.5.
#     <= 0.60 generational buy zone (once in 10-14 years; "don't short even
#     if paid"); ~0.85 = positional buy zone (~17% off the top); weekly SRT
#     (124 *weeks*) > 1.24 = distribution, exit to InvIT/gold.
#
# We compute and display it; we do NOT auto-trade it. Treat as one opinion.

HM_RSI_PERIOD = 9
HM_WMA_PERIOD = 21
HM_BUY_LEVEL = 55.0        # his changed "oversold" setting — fresh-buy floor
HM_BOOK_LEVEL = 86.0       # profit-book / resistance line on the RSI
HM_FLIP_BARS = 3           # "immediately" back to sell, in bars
HM_EMA_SPAN = 3            # GREEN line: EMA-3 of the RSI (his fast structure line)
HM_EXIT_SMA = 20           # exit/trailing = 20-SMA OF PRICE (100 on 1h)
HM_FAST_RSI = 7            # he drops to RSI 7 once SRT <= 0.85
SRT_PERIOD = 124


def _rma(s: pd.Series, period: int) -> pd.Series:
    """Wilder's smoothing SEEDED WITH THE SMA of the first `period` values.

    This seeding is not cosmetic: pandas' bare ewm() seeds from the first
    observation instead, which left our RSI up to ~3 points off the textbook
    value on a 35-bar sample — enough to flip a verdict around the 55 line.
    ta.rma() (what TradingView's RSI, and therefore the published Hilega
    Milega, actually uses) seeds with the SMA, so we match it exactly.
    """
    s = s.dropna()
    if len(s) < period:
        return pd.Series(index=s.index, dtype="float64")
    seed = float(s.iloc[:period].mean())
    tail = s.iloc[period:]
    seeded = pd.concat([pd.Series([seed], index=[s.index[period - 1]]), tail])
    return seeded.ewm(alpha=1 / period, adjust=False).mean()


def rsi(close: pd.Series, period: int = HM_RSI_PERIOD) -> pd.Series:
    """Wilder's RSI — matches TradingView (ta.rsi) bar for bar."""
    delta = close.diff()
    ag = _rma(delta.clip(lower=0), period)
    al = _rma((-delta).clip(lower=0), period)
    rs = ag / al.replace(0, pd.NA)
    out = 100 - (100 / (1 + rs))
    out = out.where(al != 0, 100.0)
    return out.reindex(close.index)


def wma(series: pd.Series, period: int) -> pd.Series:
    w = pd.Series(range(1, period + 1), dtype="float64")
    return series.rolling(period).apply(
        lambda x: float((x * w.values).sum() / w.sum()), raw=True)


def _swing_low(low: pd.Series, upto: int, lookback: int = 20):
    """Lowest low in the `lookback` bars ending at positional index `upto`."""
    seg = low.iloc[max(0, upto - lookback):upto + 1]
    return (float(seg.min()), str(seg.idxmin().date())) if len(seg) else (None, None)


def hilega_milega(df: pd.DataFrame, period: int = HM_RSI_PERIOD,
                  series_bars: int = 60) -> dict:
    """Full HM read for one timeframe. df needs close (and low for the
    old-low-break target). Returns None-ish dict when history is short."""
    if df is None or df.empty or len(df) < period + HM_WMA_PERIOD + 5:
        return {"ok": False, "reason": "not enough history"}
    r = rsi(df["close"], period)
    red = wma(r, HM_WMA_PERIOD)
    # GREEN line: EMA-3 of the RSI. He adds it "so we catch the price
    # structure faster" — it is the early-warning line that turns before
    # the RSI/red-line cross confirms.
    grn = r.ewm(span=HM_EMA_SPAN, adjust=False).mean()
    # Exit/trailing rule: the 20-SMA OF PRICE (100 on 1h). "Stop loss hi
    # aapka target hai" — the same line trails the trade.
    sma20 = df["close"].rolling(HM_EXIT_SMA).mean()
    state = (r > red)                       # True = buy side
    valid = r.notna() & red.notna()
    if not valid.any():
        return {"ok": False, "reason": "not enough history"}

    i = len(df) - 1
    cur_state = bool(state.iloc[i])
    rsi_now, red_now = float(r.iloc[i]), float(red.iloc[i])

    # bars the current state has lasted, counted 1-based (a state that just
    # began today = 1). Keep one convention everywhere: an off-by-one here
    # silently widens/narrows the "immediately" window of the flip rule.
    j = i
    while j > 0 and bool(state.iloc[j - 1]) == cur_state and valid.iloc[j - 1]:
        j -= 1
    flip_idx = j                            # first bar of the current state
    bars_in_state = i - flip_idx + 1

    # "came into buying then IMMEDIATELY went back to sell" -> old low breaks
    old_low_break = None
    if (not cur_state) and bars_in_state <= HM_FLIP_BARS and flip_idx > 0 \
            and bool(state.iloc[flip_idx - 1]):
        k = flip_idx - 1                    # last bar of the previous (buy) run
        while k > 0 and bool(state.iloc[k - 1]) and valid.iloc[k - 1]:
            k -= 1
        buy_bars = flip_idx - k             # length of that buy run
        if buy_bars <= HM_FLIP_BARS:
            lvl, when = _swing_low(df["low"], k - 1) if "low" in df else (None, None)
            old_low_break = {"level": lvl, "swing_date": when,
                             "buy_bars": buy_bars, "sell_bars": bars_in_state}

    # V-turn: RSI dipped below the buy level within the current buy leg and
    # has since crossed back above it (the curve he insists you wait for).
    v_turn = False
    if cur_state:
        leg = r.iloc[flip_idx:i + 1]
        v_turn = bool((leg < HM_BUY_LEVEL).any() and rsi_now > HM_BUY_LEVEL)

    if cur_state:
        verdict = "buy" if rsi_now > HM_BUY_LEVEL else "buy_weak"
    else:
        verdict = "sell"

    # "How far from the signal firing?" — the distance still to travel on
    # the RSI scale, and which condition is the binding one.
    grn_now = float(grn.iloc[i])
    if not cur_state:
        need_cross = max(0.0, red_now - rsi_now)
        need_level = max(0.0, HM_BUY_LEVEL - rsi_now)
        trigger = {"needs": "cross the red line" + (" and reclaim 55" if need_level > 0 else ""),
                   "rsi_gap": round(max(need_cross, need_level), 2),
                   "to_red": round(need_cross, 2),
                   "to_level": round(need_level, 2),
                   "armed": False}
    elif rsi_now <= HM_BUY_LEVEL:
        trigger = {"needs": f"reclaim {HM_BUY_LEVEL:.0f}", "rsi_gap": round(HM_BUY_LEVEL - rsi_now, 2),
                   "to_red": 0.0, "to_level": round(HM_BUY_LEVEL - rsi_now, 2), "armed": False}
    else:
        trigger = {"needs": "live — trail the 20-SMA", "rsi_gap": 0.0, "to_red": 0.0,
                   "to_level": 0.0, "armed": True,
                   "to_book": round(max(0.0, HM_BOOK_LEVEL - rsi_now), 2)}

    px = float(df["close"].iloc[i])
    sma_now = _f(sma20.iloc[i])
    tail = slice(max(0, i - series_bars + 1), i + 1)
    return {
        "ok": True,
        "state": "buy" if cur_state else "sell",
        "verdict": verdict,
        "rsi_period": period,
        "rsi": round(rsi_now, 2),
        "red_wma": round(red_now, 2),
        "green_ema": round(grn_now, 2),
        "gap": round(rsi_now - red_now, 2),
        "bars_in_state": bars_in_state,
        "above_buy_level": rsi_now > HM_BUY_LEVEL,
        "v_turn": v_turn,
        "book_profit": rsi_now >= HM_BOOK_LEVEL,
        "old_low_break": old_low_break,
        "trigger": trigger,
        "close": round(px, 2),
        "sma20": sma_now,
        "sma20_gap_pct": round((px - sma_now) / sma_now * 100, 2) if sma_now else None,
        "asof": str(df.index[i].date()),
        # compact series for the mini-chart (his TradingView pane)
        "series": {
            "rsi":   [None if pd.isna(v) else round(float(v), 2) for v in r.iloc[tail]],
            "red":   [None if pd.isna(v) else round(float(v), 2) for v in red.iloc[tail]],
            "green": [None if pd.isna(v) else round(float(v), 2) for v in grn.iloc[tail]],
        },
    }


def srt_read(close: pd.Series, period: int = SRT_PERIOD, weekly: bool = False) -> dict:
    """SRT = last close / N-period average, with his NIFTY zone labels."""
    if close is None or len(close) < period:
        return {"ok": False, "reason": f"need {period} bars"}
    avg = float(close.tail(period).mean())
    if not avg:
        return {"ok": False, "reason": "zero average"}
    val = float(close.iloc[-1]) / avg
    if weekly:
        zone = "distribution" if val > 1.24 else "normal"
    elif val <= 0.60:
        zone = "generational_buy"
    elif val <= 0.87:
        zone = "positional_buy"
    elif val >= 1.24:
        zone = "stretched"
    else:
        zone = "normal"
    return {"ok": True, "srt": round(val, 3), "avg": round(avg, 2), "zone": zone,
            "period": period, "basis": "weekly" if weekly else "daily"}


# ---------- WCash: aggregate option-writer mark-to-market ----------
# From the Vtrender manual's "Writer Cash". Every open contract has exactly
# ONE writer, so when a strike's premium rises the short side of that strike
# loses that much per unit: dWCash = -sum(OI * d_premium) over strikes. That
# needs NO assumption about who the dealer is — which is precisely why this
# is sound where our two gamma attempts (#GEX sign, #HHI concentration) were
# not: both of those required guessing the dealer's side from OI, which OI
# cannot tell you.
#
# WHAT WE VERIFIED on 40 recorded sessions (see BUILD_LOG):
#   corr(WCash, |day move|%)   = -0.62   correct sign, strong
#   corr(WCash, day range%)    = -0.70   correct sign, strong
#   corr(WCash, SIGNED move%)  = +0.20   ~0 as theory demands (hurt both ways)
#   writers profitable 30/40 sessions (75%) — matches how selling behaves
# WHAT IT IS NOT: predictive. WCash at 11:00 vs the afternoon's range is
#   -0.008 (-0.045 after controlling for morning activity). It is a STATE
#   gauge — "is the cohort I trade in being squeezed right now" — not a
#   forecast. The UI says so; do not let it drift into a direction signal.
#
# Kite reports option OI in UNITS, not lots — do NOT multiply by lot size.
# (The first prototype did, inflating every figure 75x.)

def wcash_read(conn, underlying: str = "NIFTY", day: str | None = None,
               points: int = 80) -> dict:
    """Writer mark-to-market for one session, in Rs crore, from the recorded
    per-minute chain. Read-only; returns a downsampled series for plotting."""
    try:
        if day is None:
            row = conn.execute("SELECT MAX(DATE(ts)) FROM chain_snapshot "
                               "WHERE underlying=?", (underlying,)).fetchone()
            day = row[0] if row else None
        if not day:
            return {"ok": False, "reason": "no chain data"}
        rows = conn.execute(
            "SELECT ts, strike, opt_type, ltp, oi, spot FROM chain_snapshot "
            "WHERE underlying=? AND DATE(ts)=? ORDER BY ts", (underlying, day)).fetchall()
        if len(rows) < 50:
            return {"ok": False, "reason": f"only {len(rows)} rows for {day}"}
        df = pd.DataFrame([dict(r) for r in rows])
        prem = df.pivot_table(index="ts", columns=["strike", "opt_type"], values="ltp").ffill()
        oi = df.pivot_table(index="ts", columns=["strike", "opt_type"], values="oi").ffill()
        cols = prem.columns.intersection(oi.columns)
        if not len(cols):
            return {"ok": False, "reason": "no aligned strikes"}
        step = -(prem[cols].diff() * oi[cols].shift(1)).sum(axis=1) / 1e7   # OI already units
        cum = step.cumsum().dropna()
        if cum.empty:
            return {"ok": False, "reason": "no usable minutes"}
        close = float(cum.iloc[-1])
        # last-hour drift decides "improving" vs "deteriorating"
        tail_n = max(2, len(cum) // 6)
        drift = close - float(cum.iloc[-tail_n])
        if close >= 0:
            state = "comfortable" if drift >= 0 else "comfortable_fading"
        else:
            state = "pressured_easing" if drift >= 0 else "pressured"
        idx = np.linspace(0, len(cum) - 1, min(points, len(cum))).astype(int)
        return {
            "ok": True, "day": str(day), "underlying": underlying,
            "close_cr": round(close, 1),
            "trough_cr": round(float(cum.min()), 1),
            "peak_cr": round(float(cum.max()), 1),
            "drift_cr": round(float(drift), 1),
            "state": state,
            "n_minutes": int(len(cum)),
            "series": [round(float(v), 1) for v in cum.iloc[idx]],
            "labels": [str(t)[11:16] for t in cum.index[idx]],
        }
    except Exception as e:
        return {"ok": False, "reason": f"{type(e).__name__}: {e}"}


def direction_block(kite, token: int) -> dict:
    """Hilega Milega (daily + weekly) + SRT (daily + weekly) for one index.

    One extra historical call; weekly is resampled from the same daily frame.
    The binding constraint is the WEEKLY SRT: it needs 124 weekly bars, and
    900 calendar days yields only ~128 — a four-week margin that a holiday
    stretch can erase, silently blanking the weekly zone. 1100 days (~157
    weeks) leaves real headroom and is still one request, far under Kite's
    2000-day cap for daily candles."""
    out: dict = {"rsi_period": HM_RSI_PERIOD, "wma_period": HM_WMA_PERIOD,
                 "buy_level": HM_BUY_LEVEL, "book_level": HM_BOOK_LEVEL}
    try:
        d = _historical(kite, token, "day", days=1100)
    except Exception as e:
        return {"error": f"history: {type(e).__name__}: {e}"}
    if d.empty:
        return {"error": "no daily history"}
    # SRT first: he switches the RSI to 7 once SRT reaches ~0.85 ("when the
    # market is well down"), so the period is data-dependent, not fixed.
    out["srt_daily"] = srt_read(d["close"])
    srt_val = (out["srt_daily"] or {}).get("srt")
    period = HM_FAST_RSI if (srt_val is not None and srt_val <= 0.85) else HM_RSI_PERIOD
    out["rsi_period"] = period
    out["rsi_period_reason"] = ("SRT <= 0.85 — his fast setting" if period == HM_FAST_RSI
                                else "default")
    out["daily"] = hilega_milega(d, period)
    try:
        w = d.resample("W-FRI").agg({"open": "first", "high": "max",
                                     "low": "min", "close": "last"}).dropna()
        out["weekly"] = hilega_milega(w, period)
        out["srt_weekly"] = srt_read(w["close"], weekly=True)
    except Exception as e:
        out["weekly_error"] = f"{type(e).__name__}: {e}"
    return out


# ---------- data fetching ----------

def _instrument_token(kite, exchange: str, tradingsymbol: str) -> int:
    """Look up an instrument's numeric token from the daily instruments dump."""
    instruments = kite.instruments(exchange)
    for ins in instruments:
        if ins.get("tradingsymbol") == tradingsymbol:
            return int(ins["instrument_token"])
    raise RuntimeError(f"instrument not found: {exchange}:{tradingsymbol}")


def _nearest_future_token(kite, name: str) -> tuple[int, str]:
    """Find the nearest-expiry NFO future for `name` (e.g. 'NIFTY' or 'BANKNIFTY')."""
    today = datetime.now(IST).date()
    instruments = kite.instruments("NFO")
    candidates = [
        ins for ins in instruments
        if ins.get("name") == name
        and ins.get("instrument_type") == "FUT"
        and ins.get("expiry")
        and ins["expiry"] >= today
    ]
    if not candidates:
        raise RuntimeError(f"no live NFO futures for {name}")
    candidates.sort(key=lambda i: i["expiry"])
    nearest = candidates[0]
    return int(nearest["instrument_token"]), nearest["tradingsymbol"]


def _historical(kite, token: int, interval: str, days: int) -> pd.DataFrame:
    end = datetime.now(IST)
    start = end - timedelta(days=days)
    rows = kite.historical_data(
        instrument_token=token,
        from_date=start.strftime("%Y-%m-%d %H:%M:%S"),
        to_date=end.strftime("%Y-%m-%d %H:%M:%S"),
        interval=interval,
    )
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows)
    df["date"] = pd.to_datetime(df["date"])
    return df.set_index("date")


def _resample_to_2h(df_60m: pd.DataFrame) -> pd.DataFrame:
    """
    NSE session is 09:15–15:30 IST. Anchor 2h bars to 09:15 by tagging each
    60-min bar with its session-relative pair index.
    """
    if df_60m.empty:
        return df_60m
    df = df_60m.copy()
    if df.index.tz is None:
        df.index = df.index.tz_localize(IST)
    else:
        df.index = df.index.tz_convert(IST)
    # session-bar index: 9-10 = 0, 10-11 = 1, ... pair them as floor(i/2)
    df["_hour"] = df.index.hour
    df["_pair"] = ((df["_hour"] - 9) // 2)
    df["_bucket"] = df.index.normalize().astype(str) + "_" + df["_pair"].astype(str)
    agg = df.groupby("_bucket").agg(
        open=("open", "first"),
        high=("high", "max"),
        low=("low", "min"),
        close=("close", "last"),
        volume=("volume", "sum"),
        date=("_hour", lambda _: df.loc[_.index].index.min()),
    )
    return agg.set_index("date").sort_index()


# ---------- per-instrument indicator block ----------

def _instrument_block(kite, exchange: str, tradingsymbol: str, fut_name: str | None = None) -> dict:
    """
    Build the indicator block for an instrument.

    BB-21 and EMA-53 are price-based and computed from the index.
    CMF-21 and VWMA-21 require volume — the index has none, so we use the
    nearest-expiry NFO future identified by `fut_name` for those.
    """
    out: dict = {"exchange": exchange, "tradingsymbol": tradingsymbol}
    # Spot via LTP
    try:
        ltp_key = f"{exchange}:{tradingsymbol}"
        ltp = kite.ltp([ltp_key])
        out["spot"] = ltp[ltp_key]["last_price"]
    except Exception as e:
        out["spot_error"] = str(e)
        out["spot"] = None

    token = _instrument_token(kite, exchange, tradingsymbol)
    out["instrument_token"] = token

    # Optional futures source for CMF/VWMA
    fut_token = None
    if fut_name:
        try:
            fut_token, fut_sym = _nearest_future_token(kite, fut_name)
            out["futures_tradingsymbol"] = fut_sym
            out["futures_token"] = fut_token
        except Exception as e:
            out["futures_error"] = str(e)

    # Daily candles from INDEX (for BB-21, EMA-53)
    daily = _historical(kite, token, "day", days=240)
    # Daily candles from FUTURES (for CMF — needs volume)
    fut_daily = pd.DataFrame()
    if fut_token:
        try:
            fut_daily = _historical(kite, fut_token, "day", days=120)
        except Exception as e:
            out["futures_daily_error"] = str(e)

    if not daily.empty:
        d = daily.tail(120).copy()
        d["bb21_mid"] = bb21_mid(d["close"])
        d["ema53"] = ema53(d["close"])
        # CMF from futures, aligned by date
        cmf_series = None
        if not fut_daily.empty and fut_daily["volume"].sum() > 0:
            f = fut_daily.copy()
            f["cmf21"] = cmf21(f["high"], f["low"], f["close"], f["volume"])
            # align by date (futures rolls to a different contract on expiry, so the
            # nearest-future series may not span the full 120 days — that's OK,
            # we only need the last two values)
            cmf_series = f["cmf21"]
        last = d.iloc[-1]
        prev = d.iloc[-2] if len(d) >= 2 else last
        out["daily"] = {
            "date_prev": str(prev.name.date()),
            "date": str(last.name.date()),
            "close_prev": float(prev["close"]),
            "close": float(last["close"]),
            "bb21_mid_prev": _f(prev.get("bb21_mid")),
            "bb21_mid": _f(last.get("bb21_mid")),
            "ema53_prev": _f(prev.get("ema53")),
            "ema53": _f(last.get("ema53")),
            "cmf21_prev": _f(cmf_series.iloc[-2]) if cmf_series is not None and len(cmf_series) >= 2 else None,
            "cmf21": _f(cmf_series.iloc[-1]) if cmf_series is not None and len(cmf_series) >= 1 else None,
        }

    # 2h VWMA — from FUTURES (volume needed)
    vwma_source_token = fut_token or token
    h60 = _historical(kite, vwma_source_token, "60minute", days=45)
    if not h60.empty:
        h2 = _resample_to_2h(h60)
        if not h2.empty and h2["volume"].sum() > 0:
            h2["vwma21"] = vwma21(h2["close"], h2["volume"])
            last = h2.iloc[-1]
            prev = h2.iloc[-2] if len(h2) >= 2 else last
            out["h2"] = {
                "ts_prev": prev.name.isoformat() if hasattr(prev.name, "isoformat") else str(prev.name),
                "ts": last.name.isoformat() if hasattr(last.name, "isoformat") else str(last.name),
                "close_prev": float(prev["close"]),
                "close": float(last["close"]),
                "vwma21_prev": _f(prev.get("vwma21")),
                "vwma21": _f(last.get("vwma21")),
                "source": "futures" if fut_token else "index",
            }
    return out


def _f(x) -> float | None:
    try:
        if x is None or pd.isna(x):
            return None
        return float(x)
    except Exception:
        return None


# ---------- signal derivation ----------

def _signal_from_cross(close_prev, close, ref_prev, ref) -> str:
    """Return one of: bull_cross, bear_cross, bull_hold, bear_hold, unknown."""
    if None in (close_prev, close, ref_prev, ref):
        return "unknown"
    prev_above = close_prev > ref_prev
    curr_above = close > ref
    if curr_above and not prev_above:
        return "bull_cross"
    if prev_above and not curr_above:
        return "bear_cross"
    return "bull_hold" if curr_above else "bear_hold"


def _cmf_signal(prev, curr) -> str:
    if prev is None or curr is None:
        return "unknown"
    prev_pos = prev > 0
    curr_pos = curr > 0
    if curr_pos and not prev_pos:
        return "bull_cross"
    if prev_pos and not curr_pos:
        return "bear_cross"
    return "bull_hold" if curr_pos else "bear_hold"


def derive_signals(blocks: dict) -> dict:
    nifty = blocks.get("nifty", {})
    daily = nifty.get("daily", {}) or {}
    h2 = nifty.get("h2", {}) or {}
    return {
        "golden_goose": _signal_from_cross(
            daily.get("close_prev"), daily.get("close"),
            daily.get("bb21_mid_prev"), daily.get("bb21_mid"),
        ),
        "gg_leaps": _signal_from_cross(
            daily.get("close_prev"), daily.get("close"),
            daily.get("bb21_mid_prev"), daily.get("bb21_mid"),
        ),
        "nidhi_kalash": _signal_from_cross(
            daily.get("close_prev"), daily.get("close"),
            daily.get("ema53_prev"), daily.get("ema53"),
        ),
        "panther": _cmf_signal(daily.get("cmf21_prev"), daily.get("cmf21")),
        "ocean_treasure": _signal_from_cross(
            h2.get("close_prev"), h2.get("close"),
            h2.get("vwma21_prev"), h2.get("vwma21"),
        ),
    }


# ---------- portfolio summary ----------

def compute_calendar_flags(today) -> dict:
    """IST calendar flags used by schedule-driven recommendation builders.
    Mirrors the JS logic in index.html (computeFlags) so server-side and
    client-side stay in agreement.

    NIFTY weekly expiry = Tuesday; NIFTY monthly expiry = last Tuesday."""
    from datetime import date as _date, timedelta as _timedelta

    def _last_weekday_of_month(y, m, weekday):
        if m == 12:
            first_next = _date(y + 1, 1, 1)
        else:
            first_next = _date(y, m + 1, 1)
        last_day = first_next - _timedelta(days=1)
        for offset in range(7):
            d = last_day - _timedelta(days=offset)
            if d.weekday() == weekday:
                return d
        return None

    weekday = today.weekday()  # Mon=0 .. Sun=6

    # Days until next Tuesday (=weekly NIFTY expiry). 0 means today is Tue.
    days_to_tue = (1 - weekday) % 7

    # Monthly expiry = last Tuesday of (this month / next month if past)
    last_tue_this = _last_weekday_of_month(today.year, today.month, 1)
    if today > last_tue_this:
        next_m = today.month + 1
        next_y = today.year + (1 if next_m > 12 else 0)
        next_m = 1 if next_m > 12 else next_m
        next_monthly = _last_weekday_of_month(next_y, next_m, 1)
    else:
        next_monthly = last_tue_this
    days_to_monthly = (next_monthly - today).days if next_monthly else None

    last_friday_this = _last_weekday_of_month(today.year, today.month, 4)
    is_last_friday = (today == last_friday_this)

    # Second-last Wednesday of the sold-leg expiry month (= next_monthly's month)
    second_last_wed = None
    if next_monthly:
        last_wed = _last_weekday_of_month(next_monthly.year, next_monthly.month, 2)
        if last_wed:
            second_last_wed = last_wed - _timedelta(days=7)

    return {
        "is_mon_before_weekly":   weekday == 0 and days_to_tue == 1,
        "is_tuesday_expiry":      weekday == 1 and days_to_tue == 0,
        "is_last_friday":         is_last_friday,
        "is_18th":                today.day == 18,
        "is_second_last_wed_of_expiry_month":
                                  (second_last_wed is not None
                                   and today == second_last_wed),
        "days_to_monthly":        days_to_monthly,
        "days_to_weekly":         days_to_tue,
        "weekday":                weekday,
        "today_iso":              today.isoformat(),
        "next_monthly_iso":       next_monthly.isoformat() if next_monthly else None,
        "is_t7":                  days_to_monthly == 7,
        "is_t8":                  days_to_monthly == 8,
        "is_t9":                  days_to_monthly == 9,
        "is_t4":                  days_to_monthly == 4,
    }


def portfolio_summary(kite) -> dict:
    out: dict = {}
    try:
        positions = kite.positions()
        net = positions.get("net", []) or []
        out["positions_count"] = len([p for p in net if p.get("quantity")])
        out["positions_pnl"] = sum(float(p.get("pnl") or 0) for p in net)
        out["positions"] = [
            {
                "tradingsymbol": p.get("tradingsymbol"),
                "exchange": p.get("exchange"),
                "quantity": p.get("quantity"),
                "average_price": p.get("average_price"),
                "last_price": p.get("last_price"),
                "pnl": p.get("pnl"),
            }
            for p in net if p.get("quantity")
        ]
    except Exception as e:
        out["positions_error"] = str(e)
    try:
        margins = kite.margins()
        eq = margins.get("equity", {}) or {}
        out["available_cash"] = float(eq.get("available", {}).get("cash") or 0)
        out["used_margin"] = float(eq.get("utilised", {}).get("debits") or 0)
    except Exception as e:
        out["margins_error"] = str(e)
    return out


# ---------- main ----------

def main() -> None:
    if HEADLESS:
        kite = get_kite_from_cache()
        if kite is None:
            print("ERROR: no valid Kite session. Login at /login first.", file=sys.stderr)
            sys.exit(2)
    else:
        kite = get_kite()
    print("Fetching market data...")
    blocks = {}
    for name, (ex, sym, fut) in INDICES.items():
        try:
            blocks[name] = _instrument_block(kite, ex, sym, fut_name=fut)
            spot = blocks[name].get("spot")
            fut_sym = blocks[name].get("futures_tradingsymbol", "")
            extra = f" (fut: {fut_sym})" if fut_sym else ""
            print(f"  {name:11s} spot={spot}{extra}")
        except Exception as e:
            blocks[name] = {"error": str(e)}
            print(f"  {name:11s} ERROR: {e}")

    print("Fetching portfolio...")
    portfolio = portfolio_summary(kite)

    signals = derive_signals(blocks)
    calendar_flags = compute_calendar_flags(datetime.now(IST).date())

    print("Computing directional read (Hilega Milega + SRT)...")
    # Keys come from INDICES (lowercase: "nifty"/"banknifty") — hardcoding
    # "NIFTY" here silently produced an EMPTY direction block in #69, because
    # the lookup never matched and the loop just `continue`d. Derive the
    # targets from the same dict the blocks are built from.
    direction = {}
    for name in [k for k in INDICES if k != "indiavix"]:
        tok = (blocks.get(name) or {}).get("instrument_token")
        if not tok:
            direction[name] = {"error": "no instrument_token in block"}
            continue
        try:
            direction[name] = direction_block(kite, tok)
            dd = (direction[name].get("daily") or {})
            ww = (direction[name].get("weekly") or {})
            srt = (direction[name].get("srt_daily") or {})
            print(f"  {name:11s} HM day={dd.get('verdict','?')} "
                  f"week={ww.get('verdict','?')} SRT={srt.get('srt','?')} "
                  f"({srt.get('zone','?')})")
        except Exception as e:
            direction[name] = {"error": str(e)}
            print(f"  {name:11s} direction ERROR: {e}")

    print("Computing writer cash (WCash)...")
    wcash = {}
    try:
        import db as _dbw
        with _dbw.get_conn() as _c:
            for u in ("NIFTY", "BANKNIFTY"):
                wcash[u] = wcash_read(_c, u)
                w = wcash[u]
                print(f"  {u:11s} " + (f"close Rs {w['close_cr']:+.1f}cr ({w['state']}) "
                      f"trough {w['trough_cr']:+.1f}" if w.get("ok") else f"n/a: {w.get('reason')}"))
    except Exception as e:
        print(f"  WCash ERROR: {e}")

    print("Building trade recommendations for fresh signals + calendar...")
    recommendations = build_recommendations(kite, signals, blocks, calendar_flags)
    for strat, rec in recommendations.items():
        if not rec:
            continue
        if "error" in rec:
            print(f"  {strat:15s} ERROR: {rec.get('error')}")
        elif "note" in rec:
            print(f"  {strat:15s} {rec.get('note')}")
        else:
            legs = rec.get("legs", [])
            credit = rec.get("credit_per_unit")
            margin = rec.get("margin_total")
            print(f"  {strat:15s} {rec.get('structure')} · expiry {rec.get('expiry')}")
            for leg in legs:
                print(f"    {leg['action']:4s} {leg['tradingsymbol']:25s} @ {leg['premium']}")
            print(f"    credit/unit={credit}  margin={margin}")

    payload = {
        "as_of": datetime.now(IST).isoformat(),
        "instruments": blocks,
        "signals": signals,
        "calendar_flags": calendar_flags,
        "direction": direction,
        "wcash": wcash,
        "recommendations": recommendations,
        "portfolio": portfolio,
    }

    # Paper-trading ledger: assume every actionable recommendation is executed
    # at paper.PAPER_LOTS lots; mark & manage existing paper positions.
    print("Syncing paper-trading ledger...")
    try:
        import db as _db
        import paper as _paper
        with _db.get_conn() as conn:
            payload["paper"] = _paper.sync(kite, payload, conn)
        t = payload["paper"].get("totals", {})
        print(f"  paper book: realized Rs {t.get('realized_rs', 0):,.0f} · "
              f"open uP&L Rs {t.get('open_upnl_rs', 0):,.0f}")
    except Exception as e:
        payload["paper"] = {"error": f"{type(e).__name__}: {e}"}
        print(f"  paper ledger ERROR: {e}")

    store_data(payload)
    print(f"\nWrote {OUT} ({OUT.stat().st_size if OUT.exists() else '?'} bytes) + Redis if configured")
    print("Signals:")
    for k, v in signals.items():
        print(f"  {k:15s} {v}")


if __name__ == "__main__":
    sys.exit(main())
