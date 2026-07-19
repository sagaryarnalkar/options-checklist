"""
OI chain recorder + gap backfill — the data source behind the OI Flow tab.

LIVE PATH: snapshots NIFTY and BANKNIFTY option chains (nearest expiry,
ATM ± N strikes) once per minute during NSE market hours via APScheduler in
app.py. Stores ts/underlying/spot/expiry/strike/CE|PE/ltp/volume/oi plus the
underlying 1-min candle. Manual one-shot: `python3 -m recorder`.

BACKFILL PATH (#37): the live path only captures while the app is running,
so a 2 PM login used to leave a 09:15→14:00 hole. Kite's
historical_data(oi=True) returns per-minute close/volume/OI per instrument,
letting backfill_day() rebuild missing minutes in the exact chain_snapshot
shape (same ATM±N footprint per minute). Auto-runs for today's gap on the
first market-hours tick after login (background thread, self-dedupes);
manual: POST /oi/backfill. Idempotent via the UNIQUE constraint. Limit: a
past day whose weekly contracts already expired cannot be rebuilt (tokens
leave the instruments dump). Data is gap-free since 2026-06-30.

FORWARD-RETURN TRACKING: after each snapshot, _refresh_marker_outcomes()
recomputes today's OI-Flow score markers under a PINNED definition (absolute
threshold, writing basis, no episode collapse, flow_basis='oi', trend 1,
score_baseline 0 — see oi_flow.py header for why pinning matters) and
upserts +5/15/30-min forward returns into score_marker_outcomes.

Failures: logged to recorder_log; the loop never raises into APScheduler.
Silent no-op reasons: no valid Kite session, outside market hours, weekend.
"""
from __future__ import annotations

import sys
import threading
import time
from datetime import datetime, timedelta, timezone, date as date_cls
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


def _nearest_expiry_for(instruments: list, name: str, day) -> Optional[object]:
    """Nearest option expiry on/after `day` (vs `_nearest_expiry` which anchors
    on today). Used by the backfill. NOTE: only expiries still present in the
    current NFO instruments dump are visible — a past day whose weekly expiry has
    already lapsed cannot be reconstructed (its tokens are gone)."""
    seen = set()
    for ins in instruments:
        if (
            ins.get("name") == name
            and ins.get("instrument_type") in ("CE", "PE")
            and ins.get("expiry")
            and ins["expiry"] >= day
        ):
            seen.add(ins["expiry"])
    return sorted(seen)[0] if seen else None


def _bar_minute_iso(bar_date) -> str:
    """Normalise a historical_data bar's `date` to an IST minute-start ISO
    string, matching the live recorder's ts bucketing."""
    bd = bar_date
    bd = bd.astimezone(IST) if bd.tzinfo else bd.replace(tzinfo=IST)
    return bd.replace(second=0, microsecond=0).isoformat()


def _current_nifty_future(instruments, today) -> Optional[dict]:
    """Current-month NIFTY futures contract, auto-rolled to next month within
    2 days of expiry (LLT spec rule 1). Returns token/symbol/lot/expiry."""
    futs = sorted(
        (ins for ins in instruments
         if ins.get("name") == "NIFTY" and ins.get("instrument_type") == "FUT"
         and ins.get("expiry") and ins["expiry"] >= today),
        key=lambda i: i["expiry"])
    if not futs:
        return None
    pick = futs[0]
    if (pick["expiry"] - today).days <= 2 and len(futs) > 1:
        pick = futs[1]
    return {"token": pick["instrument_token"], "symbol": pick["tradingsymbol"],
            "lot_size": int(pick.get("lot_size") or 75), "expiry": pick["expiry"]}


def _snapshot_futures(kite, instruments, now) -> Optional[str]:
    """One futures_minute row for the current minute + incremental LLT
    detection for today. Cheap (one quote); never raises."""
    import llt
    try:
        fut = _current_nifty_future(instruments, now.date())
        if not fut:
            return "no futures contract"
        q = kite.quote([f"NFO:{fut['symbol']}"]).get(f"NFO:{fut['symbol']}") or {}
        ts = now.replace(second=0, microsecond=0).isoformat()
        with db.get_conn() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO futures_minute (ts, symbol, ltp, volume, oi)"
                " VALUES (?,?,?,?,?)",
                (ts, fut["symbol"], q.get("last_price"), q.get("volume"), q.get("oi")))
            conn.commit()
            llt.detect_day(conn, now.date().isoformat(), fut["lot_size"])
        return None
    except Exception as e:
        return f"{type(e).__name__}: {e}"


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


# ====================================================================
# Backfill — reconstruct missing chain_snapshot rows from Kite history
# ====================================================================
# The live recorder only captures minutes while the app is running, so logging
# in at (say) 2 PM leaves a 09:15–14:00 hole. Kite's historical_data(oi=True)
# returns per-minute close/volume/OI for each option instrument, which lets us
# rebuild those minutes exactly in the chain_snapshot shape. Idempotent: the
# UNIQUE(ts, underlying, strike, expiry, opt_type) constraint dedups any minute
# the live recorder already captured.

# Throttle: Kite historical_data allows ~3 requests/sec. One call per option
# instrument; sleep keeps us comfortably under the cap.
_HIST_THROTTLE_S = 0.34

# Auto-backfill guard — fill today's gap once per (process, day, underlying).
_backfilled: set = set()
_backfill_lock = threading.Lock()


def _existing_expiry_for_day(conn, underlying: str, day_iso: str) -> Optional[str]:
    """If the day already has partial live data, reuse its expiry so the
    backfill stays on the same contract series."""
    row = conn.execute(
        "SELECT expiry FROM chain_snapshot WHERE underlying = ? AND DATE(ts) = ? LIMIT 1",
        (underlying, day_iso),
    ).fetchone()
    return row["expiry"] if row else None


def _backfill_futures(kite, instruments, day, start, end) -> dict:
    """Futures minute bars + LLT detection for one day — INDEPENDENT of the
    options-chain backfill: a past day's weekly option contracts may have
    lapsed from the instruments dump, but the monthly future lives on (this
    is exactly the Jul-14 LLT verification case)."""
    out = {"futures_minutes": 0, "llt_prints_found": 0}
    try:
        import llt
        fut = _current_nifty_future(instruments, day)
        if not fut:
            out["futures_error"] = "no live futures contract for that day"
            return out
        bars = kite.historical_data(
            fut["token"], start.strftime("%Y-%m-%d %H:%M:%S"),
            end.strftime("%Y-%m-%d %H:%M:%S"), "minute", oi=True)
        with db.get_conn() as conn:
            for b in bars:
                conn.execute(
                    "INSERT OR IGNORE INTO futures_minute (ts, symbol, ltp, volume, oi)"
                    " VALUES (?,?,?,?,?)",
                    (_bar_minute_iso(b["date"]), fut["symbol"], b.get("close"),
                     b.get("volume"), b.get("oi")))
            conn.commit()
            out["futures_minutes"] = len(bars)
            out["llt_prints_found"] = llt.detect_day(conn, day.isoformat(), fut["lot_size"])
    except Exception as e:
        out["futures_error"] = f"{type(e).__name__}: {e}"
    return out


def backfill_day(kite, name: str, conf: dict, day, upto: Optional[datetime] = None) -> dict:
    """Reconstruct chain_snapshot + underlying_candle rows for `day` (a date)
    from Kite history. Fills only the gap — existing minutes are dedup-ignored.
    `upto` caps the window (defaults to min(15:30, now) so we never fabricate
    future minutes for today)."""
    if isinstance(day, str):
        day = datetime.fromisoformat(day).date()
    start = datetime(day.year, day.month, day.day, 9, 15, tzinfo=IST)
    end = datetime(day.year, day.month, day.day, 15, 30, tzinfo=IST)
    now = datetime.now(IST)
    cap = upto or now
    if end > cap:
        end = cap
    if end <= start:
        return {"ok": False, "underlying": name, "date": str(day), "error": "empty/future window"}

    instruments = _get_instruments(kite)
    day_iso = day.isoformat()
    # Futures + LLT first — never blocked by lapsed option chains (#54 fix)
    fut = _backfill_futures(kite, instruments, day, start, end) if name == "NIFTY" else {}
    with db.get_conn() as conn:
        expiry = _existing_expiry_for_day(conn, name, day_iso)
    if expiry is None:
        exp_obj = _nearest_expiry_for(instruments, conf["nfo_name"], day)
        if exp_obj is None:
            return {"ok": False, **fut, "underlying": name, "date": str(day),
                    "error": "no expiry in instruments dump (contracts may have lapsed)"}
        expiry = str(exp_obj)

    def _hist(token, oi=False):
        return kite.historical_data(
            token,
            start.strftime("%Y-%m-%d %H:%M:%S"),
            end.strftime("%Y-%m-%d %H:%M:%S"),
            "minute",
            oi=oi,
        )

    # 1) Underlying minute series → spot per minute + candle rows.
    try:
        idx_bars = _hist(conf["instrument_token"])
    except Exception as e:
        return {"ok": False, **fut, "underlying": name, "date": str(day),
                "error": f"index history: {type(e).__name__}: {e}"}
    if not idx_bars:
        return {"ok": False, **fut, "underlying": name, "date": str(day), "error": "no index history"}

    spot_by_min: dict = {}
    candle_rows = []
    for b in idx_bars:
        miso = _bar_minute_iso(b["date"])
        if b.get("close") is None:
            continue
        spot_by_min[miso] = b["close"]
        candle_rows.append({
            "ts": miso, "underlying": name,
            "open": b.get("open"), "high": b.get("high"),
            "low": b.get("low"), "close": b.get("close"),
            "volume": b.get("volume") or 0,
        })
    if not spot_by_min:
        return {"ok": False, **fut, "underlying": name, "date": str(day), "error": "index history had no closes"}

    # 2) Candidate strikes = union band across the day's spot range, so every
    #    minute's ATM ± atm_n is covered.
    chain = [
        ins for ins in instruments
        if ins.get("name") == conf["nfo_name"]
        and str(ins.get("expiry")) == expiry
        and ins.get("instrument_type") in ("CE", "PE")
    ]
    all_strikes = sorted({int(ins["strike"]) for ins in chain})
    if len(all_strikes) < 2:
        return {"ok": False, **fut, "underlying": name, "date": str(day),
                "error": f"no/too-few strikes for expiry {expiry}"}
    steps = [all_strikes[i + 1] - all_strikes[i] for i in range(len(all_strikes) - 1)]
    step = min(s for s in steps if s > 0)
    pad = (conf["atm_n"] + 1) * step
    lo_spot, hi_spot = min(spot_by_min.values()), max(spot_by_min.values())
    band_set = {s for s in all_strikes if lo_spot - pad <= s <= hi_spot + pad}
    targets = [
        (int(ins["strike"]), ins["instrument_type"], ins["instrument_token"])
        for ins in chain if int(ins["strike"]) in band_set
    ]

    # 3) Per-option minute history (oi=True), throttled.
    opt_by_min: dict = {}
    fetched = 0
    for strike, opt_type, token in targets:
        try:
            bars = _hist(token, oi=True)
            fetched += 1
        except Exception:
            bars = []
        d = {}
        for b in bars:
            d[_bar_minute_iso(b["date"])] = (b.get("close"), b.get("volume"), b.get("oi"))
        opt_by_min[(strike, opt_type)] = d
        time.sleep(_HIST_THROTTLE_S)

    # 4) Build chain rows per minute, restricted to that minute's ATM ± atm_n
    #    band (matching the live recorder's footprint exactly).
    rows = []
    for miso, spot in spot_by_min.items():
        atm = min(all_strikes, key=lambda s: abs(s - spot))
        ai = all_strikes.index(atm)
        lo = max(0, ai - conf["atm_n"])
        hi = min(len(all_strikes), ai + conf["atm_n"] + 1)
        minute_band = set(all_strikes[lo:hi])
        for (strike, opt_type), d in opt_by_min.items():
            if strike not in minute_band:
                continue
            bar = d.get(miso)
            if bar is None:
                continue
            close, vol, oi = bar
            rows.append({
                "ts": miso, "underlying": name, "spot": float(spot),
                "expiry": expiry, "strike": int(strike), "opt_type": opt_type,
                "ltp": close, "volume": vol, "oi": oi,
            })

    with db.get_conn() as conn:
        ins_chain = db.insert_snapshots(conn, rows)
        ins_candle = db.insert_candles(conn, candle_rows)
        db.log_recorder_run(conn, name, expiry, ins_chain,
                            strikes=len(band_set), error=f"backfill {day_iso} ({end.strftime('%H:%M')})")
    return {
        "ok": True, "underlying": name, "date": str(day), "expiry": expiry,
        "window": f"09:15–{end.strftime('%H:%M')}", "instruments": len(targets),
        "instruments_fetched": fetched, "chain_rows_inserted": ins_chain,
        "candle_rows_inserted": ins_candle,
        **fut,
    }


def backfill_underlyings(kite, day=None, upto: Optional[datetime] = None) -> dict:
    """Backfill every configured underlying for `day` (default: today IST)."""
    if day is None:
        day = datetime.now(IST).date()
    out = {"date": str(day), "underlyings": {}}
    for name, conf in UNDERLYINGS.items():
        try:
            out["underlyings"][name] = backfill_day(kite, name, conf, day, upto=upto)
        except Exception as e:
            out["underlyings"][name] = {"ok": False, "error": f"{type(e).__name__}: {e}"}
    return out


def _maybe_backfill_today(kite) -> None:
    """Once per (process, day), fill today's morning gap in a background thread
    so the per-minute snapshot loop is never blocked by the (slow) history
    fetches. Safe to call every tick — it self-dedupes."""
    today = datetime.now(IST).date().isoformat()
    to_run = []
    with _backfill_lock:
        for name in UNDERLYINGS:
            key = (today, name)
            if key not in _backfilled:
                _backfilled.add(key)
                to_run.append(name)
    if not to_run:
        return

    def _work():
        for name in to_run:
            try:
                res = backfill_day(kite, name, UNDERLYINGS[name], datetime.now(IST).date())
                print(f"[recorder] auto-backfill {name}: {res}")
            except Exception as e:
                print(f"[recorder] auto-backfill {name}: {type(e).__name__}: {e}")
                with _backfill_lock:
                    _backfilled.discard((today, name))  # allow a retry next tick

    threading.Thread(target=_work, name="oi-backfill", daemon=True).start()


def run_snapshot(force: bool = False) -> dict:
    """One scheduler tick. No-ops outside market hours or when not logged in.
    Returns a small per-underlying status dict; never raises."""
    now = datetime.now(IST)
    if not force and not is_market_hours(now):
        return {"skipped": "outside_market_hours", "ts": now.isoformat()}

    kite = get_kite_from_cache()
    if kite is None:
        return {"skipped": "no_kite_session", "ts": now.isoformat()}

    # First valid tick of the day kicks a background backfill of today's gap
    # (e.g. 09:15 → login time). Self-dedupes; never blocks this snapshot.
    if is_market_hours(now):
        _maybe_backfill_today(kite)

    results: dict = {"ts": now.isoformat(), "underlyings": {}}
    # NIFTY futures minute-bar + LLT large-print detection (piggybacks the
    # same tick; failures logged into the result, never fatal)
    fut_err = _snapshot_futures(kite, _get_instruments(kite), now)
    if fut_err:
        results["futures_llt_error"] = fut_err
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
        # Raw per-minute markers — episode collapsing would change which
        # minutes get tracked depending on neighbours, making the series
        # definition path-dependent.
        collapse_episodes=False,
        # Pin the tracked flow basis to OI (net positioning) for series
        # stability, independent of the UI default (volume). Switching basis
        # mid-week would redefine what a "marker" is.
        flow_basis="oi",
        # Pin the trend window to 1 minute so the write/buy split uses the raw
        # tick (trend == d_ltp). The longer trend lens is a UI-display choice
        # for matching the reference indicator; the tracked series must stay on
        # the original definition so forward-return stats aren't redefined.
        trend_window_minutes=1,
        # Pin the score baseline to 0 (= score off the gate threshold, the
        # original definition). The stable 90-min baseline that the UI uses to
        # spread scores 2-5 would otherwise redefine every tracked score and
        # break the (side, score) forward-return buckets mid-series.
        score_baseline_minutes=0,
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
