"""
OI chain recorder. Snapshots NIFTY and BANKNIFTY option chains (nearest
expiry, ATM ± N strikes) once per minute during NSE market hours.

How it's invoked: APScheduler inside the FastAPI process (see app.py).
Manual one-shot: `python3 -m recorder` from the venv.

What's stored: timestamp, underlying, spot, expiry, strike, CE/PE, ltp, volume, oi.
NOT stored: tradingsymbol or instrument_token (recoverable from the strike+expiry
since the contract identity is fully determined by them).

Failures: logged to recorder_log; the loop never raises out into APScheduler.
Reasons recording might silently no-op: no valid Kite session (user hasn't
logged in today), outside market hours, weekend.
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from typing import Optional

import db
import oi_flow
from kite_auth import get_kite_from_cache

IST = timezone(timedelta(hours=5, minutes=30))

# ---- Underlyings to record (NIFTY + BANKNIFTY only, per spec) ----
# instrument_token is needed for Kite historical_data() (1-min OHLC for the
# candlestick chart). These are stable NSE indices tokens.
UNDERLYINGS = {
    "NIFTY": {
        "exchange": "NSE",
        "tradingsymbol": "NIFTY 50",
        "nfo_name": "NIFTY",
        "instrument_token": 256265,
        "atm_n": 10,
    },
    "BANKNIFTY": {
        "exchange": "NSE",
        "tradingsymbol": "NIFTY BANK",
        "nfo_name": "BANKNIFTY",
        "instrument_token": 260105,
        "atm_n": 10,
    },
}

# Instruments dump is large (~10 MB / a few thousand rows). Refresh once per
# IST trading day, not every minute.
_instruments_cache = None
_instruments_cache_date: Optional[datetime] = None


def _get_instruments(kite) -> list:
    global _instruments_cache, _instruments_cache_date
    today = datetime.now(IST).date()
    if _instruments_cache is None or _instruments_cache_date != today:
        _instruments_cache = kite.instruments("NFO")
        _instruments_cache_date = today
    return _instruments_cache


def _nearest_expiry(instruments: list, name: str) -> Optional[object]:
    today = datetime.now(IST).date()
    seen = set()
    for ins in instruments:
        if (
            ins.get("name") == name
            and ins.get("instrument_type") in ("CE", "PE")
            and ins.get("expiry")
            and ins["expiry"] >= today
        ):
            seen.add(ins["expiry"])
    if not seen:
        return None
    return sorted(seen)[0]


def is_market_hours(now: Optional[datetime] = None) -> bool:
    """True if the given moment (default: now) is inside NSE F&O hours
    on a weekday. NSE F&O: 09:15–15:30 IST, Monday–Friday."""
    now = now or datetime.now(IST)
    if now.weekday() >= 5:  # Sat=5, Sun=6
        return False
    minutes = now.hour * 60 + now.minute
    return (9 * 60 + 15) <= minutes <= (15 * 60 + 30)


def snapshot_one(kite, name: str, conf: dict) -> dict:
    """Take one snapshot for one underlying. Returns a stats dict (also written
    to recorder_log). Raises on hard failure — callers should catch."""
    instruments = _get_instruments(kite)
    expiry = _nearest_expiry(instruments, conf["nfo_name"])
    if expiry is None:
        return {"ok": False, "error": "no upcoming expiry found"}

    # Spot via LTP
    ltp_key = f"{conf['exchange']}:{conf['tradingsymbol']}"
    ltp_resp = kite.ltp([ltp_key])
    spot = ltp_resp[ltp_key]["last_price"]

    # Filter chain for this name + expiry
    chain = [
        ins for ins in instruments
        if ins.get("name") == conf["nfo_name"]
        and ins.get("expiry") == expiry
        and ins.get("instrument_type") in ("CE", "PE")
    ]
    strikes = sorted({int(ins["strike"]) for ins in chain})
    if not strikes:
        return {"ok": False, "error": "no strikes in chain", "expiry": str(expiry)}

    # ATM ± atm_n strikes (spec: detect strike step, don't hardcode lot size)
    atm = min(strikes, key=lambda s: abs(s - spot))
    atm_idx = strikes.index(atm)
    lo = max(0, atm_idx - conf["atm_n"])
    hi = min(len(strikes), atm_idx + conf["atm_n"] + 1)
    target_strikes = set(strikes[lo:hi])

    # Build the list of contracts (strike, opt_type, tradingsymbol)
    targets = [
        (int(ins["strike"]), ins["instrument_type"], ins["tradingsymbol"])
        for ins in chain
        if int(ins["strike"]) in target_strikes
    ]
    if not targets:
        return {"ok": False, "error": "no target instruments", "expiry": str(expiry)}

    # Kite quote() returns LTP, volume, OI in one call. Up to 500 syms per call;
    # we have ~42, so a single call.
    quote_keys = [f"NFO:{t[2]}" for t in targets]
    quotes: dict = {}
    for i in range(0, len(quote_keys), 200):
        chunk = quote_keys[i : i + 200]
        try:
            quotes.update(kite.quote(chunk))
        except Exception:
            # Best-effort: try one at a time so a single bad symbol doesn't kill the batch.
            for s in chunk:
                try:
                    quotes.update(kite.quote([s]))
                except Exception:
                    pass

    # Bucket timestamp to the start of the current IST minute so re-runs in the
    # same minute are de-duped by the UNIQUE constraint.
    ts = datetime.now(IST).replace(second=0, microsecond=0).isoformat()
    rows = []
    for strike, opt_type, ts_sym in targets:
        q = quotes.get(f"NFO:{ts_sym}", {}) or {}
        rows.append({
            "ts": ts,
            "underlying": name,
            "spot": float(spot),
            "expiry": str(expiry),
            "strike": int(strike),
            "opt_type": opt_type,
            "ltp": q.get("last_price"),
            "volume": q.get("volume"),
            "oi": q.get("oi"),
        })

    # Underlying 1-min OHLC for the candlestick chart. Best-effort: a fetch
    # failure here doesn't fail the chain snapshot.
    candle_inserted = 0
    try:
        candle = _fetch_last_completed_candle(kite, conf["instrument_token"])
        if candle is not None:
            with db.get_conn() as conn:
                candle_inserted = db.insert_candles(conn, [{
                    "ts": candle["ts"],
                    "underlying": name,
                    "open": candle["open"],
                    "high": candle["high"],
                    "low": candle["low"],
                    "close": candle["close"],
                    "volume": candle.get("volume") or 0,
                }])
    except Exception as e:
        # Logged below; chain insert still proceeds.
        candle_err = f"candle: {type(e).__name__}: {e}"
    else:
        candle_err = None

    with db.get_conn() as conn:
        inserted = db.insert_snapshots(conn, rows)
        db.log_recorder_run(conn, name, str(expiry), inserted, strikes=len(target_strikes), error=candle_err)

    return {
        "ok": True,
        "underlying": name,
        "expiry": str(expiry),
        "strikes": len(target_strikes),
        "rows_inserted": inserted,
        "candle_inserted": candle_inserted,
        "ts": ts,
    }


def _fetch_last_completed_candle(kite, instrument_token: int) -> Optional[dict]:
    """Fetch the most recent COMPLETED 1-min candle for the underlying.

    At t = 13:46:30 we want the 13:45 bar (fully formed). We request the
    most recent ~3 minutes of 1-min data and pick the most recent bar
    whose minute is strictly less than 'now'."""
    now = datetime.now(IST)
    from_dt = now - timedelta(minutes=5)
    bars = kite.historical_data(
        instrument_token=instrument_token,
        from_date=from_dt.strftime("%Y-%m-%d %H:%M:%S"),
        to_date=now.strftime("%Y-%m-%d %H:%M:%S"),
        interval="minute",
    )
    if not bars:
        return None
    current_minute_start = now.replace(second=0, microsecond=0)
    # Bars arrive oldest-first; pick the newest bar that is strictly before
    # current_minute_start (i.e. fully complete).
    completed = None
    for b in bars:
        bar_dt = b["date"]
        # Normalise to IST if needed
        if bar_dt.tzinfo is None:
            bar_dt = bar_dt.replace(tzinfo=IST)
        else:
            bar_dt = bar_dt.astimezone(IST)
        if bar_dt < current_minute_start:
            completed = (bar_dt, b)
    if completed is None:
        return None
    bar_dt, b = completed
    return {
        "ts": bar_dt.replace(second=0, microsecond=0).isoformat(),
        "open": b.get("open"),
        "high": b.get("high"),
        "low": b.get("low"),
        "close": b.get("close"),
        "volume": b.get("volume"),
    }


def run_snapshot(force: bool = False) -> dict:
    """One scheduler tick. No-ops outside market hours or when not logged in.
    Returns a small per-underlying status dict; never raises."""
    now = datetime.now(IST)
    if not force and not is_market_hours(now):
        return {"skipped": "outside_market_hours", "ts": now.isoformat()}

    kite = get_kite_from_cache()
    if kite is None:
        return {"skipped": "no_kite_session", "ts": now.isoformat()}

    results: dict = {"ts": now.isoformat(), "underlyings": {}}
    for name, conf in UNDERLYINGS.items():
        try:
            results["underlyings"][name] = snapshot_one(kite, name, conf)
        except Exception as e:
            err = f"{type(e).__name__}: {e}"
            results["underlyings"][name] = {"ok": False, "error": err}
            try:
                with db.get_conn() as conn:
                    db.log_recorder_run(conn, name, None, 0, 0, err)
            except Exception:
                pass

    # After the snapshot, refresh today's score-marker outcomes for each
    # underlying. Cheap (single SQLite query + a small Python loop), idempotent
    # via the UNIQUE constraint, and fills in forward-return columns as the
    # 5/15/30-min spot points become available.
    try:
        today = now.date().isoformat()
        with db.get_conn() as conn:
            for name in UNDERLYINGS:
                try:
                    _refresh_marker_outcomes(conn, name, today)
                except Exception as e:
                    print(f"[recorder] marker outcomes refresh {name}: {type(e).__name__}: {e}")
    except Exception as e:
        print(f"[recorder] marker outcomes overall: {type(e).__name__}: {e}")

    return results


# ---- Forward-return tracking ----
# Default settings to track. Mirrors the UI defaults so we're tracking what the
# user actually sees by default; we can extend to multiple parameter sets later
# without breaking anything because the UNIQUE key on score_marker_outcomes
# includes (mode, atm_band, threshold_cr).
TRACK_MODE = "premium"
TRACK_THRESHOLD_CR = 10.0
TRACK_ATM_BAND = 2


def _refresh_marker_outcomes(conn, underlying: str, date_str: str) -> int:
    """Recompute today's markers and upsert with whatever forward-return data
    is currently available. Returns number of rows written."""
    result = oi_flow.aggregate_day(
        conn,
        underlying=underlying,
        date=date_str,
        mode=TRACK_MODE,
        score_threshold_cr=TRACK_THRESHOLD_CR,
        n=10,
        atm_band=TRACK_ATM_BAND,
        # Keep tracking on a FIXED absolute threshold + writing-only basis,
        # even though the UI defaults to adaptive + combined. An adaptive
        # threshold changes through the day as the mean/σ evolve, and a basis
        # change renames marker sides — either would make the tracked marker
        # set unstable under the (ts, …, threshold_cr) UNIQUE key and pollute
        # the forward-return analysis with a moving definition of "marker".
        threshold_mode="absolute",
        score_basis="writing",
    )
    markers = result.get("score_markers") or []
    candles = result.get("candles") or []
    if not markers:
        return 0

    # Build a time -> close lookup. Time values are IST-shifted unix seconds
    # (oi_flow._ts_to_unix). Adding 300/900/1800 still works since the shift
    # is constant.
    spot_by_time = {c["time"]: c["close"] for c in candles}

    rows = []
    for m in markers:
        t = m["time"]
        spot0 = spot_by_time.get(t)
        if spot0 is None or spot0 <= 0:
            continue

        def _ret(spot_future):
            if spot_future is None or spot_future <= 0:
                return None
            return (spot_future - spot0) / spot0 * 10000.0  # bps

        spot5 = spot_by_time.get(t + 5 * 60)
        spot15 = spot_by_time.get(t + 15 * 60)
        spot30 = spot_by_time.get(t + 30 * 60)

        rows.append({
            "ts": m["ts_iso"],
            "underlying": underlying,
            "side": m["side"],
            "score": m["score"],
            "amount_cr": m["amount_cr"],
            "mode": TRACK_MODE,
            "atm_band": TRACK_ATM_BAND,
            "threshold_cr": TRACK_THRESHOLD_CR,
            "spot_at_marker": spot0,
            "spot_5min": spot5,
            "spot_15min": spot15,
            "spot_30min": spot30,
            "return_5min_bps": _ret(spot5),
            "return_15min_bps": _ret(spot15),
            "return_30min_bps": _ret(spot30),
        })

    return db.upsert_marker_outcomes(conn, rows)


if __name__ == "__main__":
    out = run_snapshot(force="--force" in sys.argv)
    print(out)
