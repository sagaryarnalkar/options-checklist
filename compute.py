"""
NIFTY Options 3:15 PM checklist — data builder.

Fetches NIFTY/BANKNIFTY/INDIA VIX spot + candles from Kite Connect, computes
BB-21 (daily), EMA-53 (daily), CMF-21 (daily), VWMA-21 (2h), reads positions/
holdings/margins, and writes ./data.json for the static index.html to render.

Run:
    python3 compute.py

The script will perform the daily Kite login if no fresh token is cached.
Output: ./data.json
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

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
        "recommendations": recommendations,
        "portfolio": portfolio,
    }

    store_data(payload)
    print(f"\nWrote {OUT} ({OUT.stat().st_size if OUT.exists() else '?'} bytes) + Redis if configured")
    print("Signals:")
    for k, v in signals.items():
        print(f"  {k:15s} {v}")


if __name__ == "__main__":
    sys.exit(main())
