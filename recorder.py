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
from kite_auth import get_kite_from_cache

IST = timezone(timedelta(hours=5, minutes=30))

# ---- Underlyings to record (NIFTY + BANKNIFTY only, per spec) ----
UNDERLYINGS = {
    "NIFTY": {
        "exchange": "NSE",
        "tradingsymbol": "NIFTY 50",
        "nfo_name": "NIFTY",
        "atm_n": 10,
    },
    "BANKNIFTY": {
        "exchange": "NSE",
        "tradingsymbol": "NIFTY BANK",
        "nfo_name": "BANKNIFTY",
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

    with db.get_conn() as conn:
        inserted = db.insert_snapshots(conn, rows)
        db.log_recorder_run(conn, name, str(expiry), inserted, strikes=len(target_strikes))

    return {
        "ok": True,
        "underlying": name,
        "expiry": str(expiry),
        "strikes": len(target_strikes),
        "rows_inserted": inserted,
        "ts": ts,
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
    return results


if __name__ == "__main__":
    out = run_snapshot(force="--force" in sys.argv)
    print(out)
