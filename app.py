"""
FastAPI server for the NIFTY options dashboard — serves the UI, the data
layer, the OI-Flow analytics, and the paper-trading ledger, and hosts BOTH
schedulers (per-minute chain recorder + daily 15:16 IST compute).

Routes:
    GET  /                    -> index.html (single-file dashboard)
    GET  /data.json           -> latest compute.py payload (signals, recs,
                                 portfolio, paper-book summary)
    GET  /login, /callback    -> Kite OAuth flow (token cached via storage.py)
    POST /refresh             -> run compute.py subprocess (auth: X-Auth-Token
                                 REFRESH_TOKEN header OR a cached Kite session)
    GET  /healthz             -> 200 OK
    -- OI Flow --
    GET  /oi/aggregate        -> oi_flow.aggregate_day/range payload (params:
                                 mode, flow_basis, score_basis, threshold_mode,
                                 roll/trend/score_baseline windows, days, ...)
    GET  /oi/days, /oi/status -> data presence / recorder health
    GET  /oi/marker_analysis  -> forward-return stats per (side, score)
    POST /oi/snapshot-now     -> one recorder tick, ignores market hours
    POST /oi/backfill         -> rebuild a day's missing chain minutes from
                                 kite historical_data(oi=True) (login gaps)
    GET  /oi/dbdump           -> whole SQLite DB (analysis aid; PENDING REMOVAL)
    -- Paper ledger --
    GET  /paper/ledger        -> full book: open (with latest marks), last 100
                                 closed, per-strategy summary. Claude polls
                                 this to review performance.
    POST /paper/seed          -> one-time idempotent seeding of hold-status
                                 strategies (2026-07-07; entries tagged
                                 'seed-<context>')
    POST /paper/void/{id}     -> close an open paper trade at ₹0 P&L
                                 (ledger maintenance for mis-built structures)

Scheduling (APScheduler, started in _startup):
    - oi_recorder:   every minute 03–10 UTC Mon–Fri; recorder.run_snapshot
                     no-ops outside 09:15–15:30 IST or without a Kite session.
                     First market-hours tick also fires the day's gap backfill.
    - daily_compute: 09:46 UTC (= 15:16 IST) Mon–Fri; runs compute.py so the
                     paper ledger opens/marks/rolls/exits without a manual
                     Refresh. The user's one daily duty is the Kite login.

Deploy: uvicorn app:app --host 127.0.0.1 --port 8000 behind Caddy, user-systemd
unit `options-app`; the user deploys via `git pull && systemctl --user restart
options-app` on the droplet.
"""
from __future__ import annotations

import asyncio
import datetime as _dt
import json
import os
import sys
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import (
    FileResponse, HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse,
)

from kite_auth import (
    exchange_request_token, login_url, write_cached_session,
)
from storage import load_data_text, storage_info

# OI chain recorder (PR A)
import db
import recorder
import oi_flow
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

_scheduler: Optional[AsyncIOScheduler] = None

ROOT = Path(__file__).parent
INDEX_HTML = ROOT / "index.html"
DATA_JSON = ROOT / "data.json"
COMPUTE_PY = ROOT / "compute.py"

app = FastAPI(title="Options Checklist", docs_url=None, redoc_url=None)


@app.on_event("startup")
async def _startup():
    """Initialise the SQLite store and start the in-process minute scheduler.

    NSE F&O hours are 09:15–15:30 IST = 03:45–10:00 UTC. We schedule every
    minute across hours 3–10 UTC, Mon–Fri, and the recorder no-ops outside
    the precise 09:15–15:30 IST window (and when no Kite session is cached).
    """
    global _scheduler
    db.init_db()
    if os.environ.get("DISABLE_OI_RECORDER", "").lower() in ("1", "true", "yes"):
        return
    _scheduler = AsyncIOScheduler(timezone="UTC")
    _scheduler.add_job(
        recorder.run_snapshot,
        trigger=CronTrigger(minute="*", hour="3-10", day_of_week="mon-fri", timezone="UTC"),
        id="oi_recorder",
        max_instances=1,
        coalesce=True,
        misfire_grace_time=30,
    )
    # Daily auto-refresh at 15:16 IST (09:46 UTC) — runs compute.py so the
    # paper ledger opens/marks/exits on schedule days without a manual Refresh.
    _scheduler.add_job(
        _scheduled_refresh,
        trigger=CronTrigger(hour=9, minute=46, day_of_week="mon-fri", timezone="UTC"),
        id="daily_compute",
        max_instances=1,
        coalesce=True,
        misfire_grace_time=600,
    )
    _scheduler.start()


@app.on_event("shutdown")
async def _shutdown():
    global _scheduler
    if _scheduler is not None:
        _scheduler.shutdown(wait=False)
        _scheduler = None


@app.get("/healthz")
async def healthz():
    return PlainTextResponse("ok")


@app.get("/")
async def root():
    if INDEX_HTML.exists():
        return FileResponse(INDEX_HTML, media_type="text/html")
    return PlainTextResponse("index.html missing", status_code=500)


@app.get("/data.json")
async def data():
    text = load_data_text()
    if text:
        return JSONResponse(content=json.loads(text))
    return JSONResponse(
        {"error": "no snapshot yet. Visit /login then POST /refresh."},
        status_code=404,
    )


@app.get("/storage-info")
async def storage_info_route():
    return JSONResponse(storage_info())


@app.get("/oi/dbdump")
async def oi_dbdump():
    """Serve the full SQLite OI database for offline analysis. Checkpoints the
    WAL first so the served file is complete. Contains only market OI data
    (no account/position data) — safe to download. Intended for ad-hoc
    analysis; can be removed once done."""
    try:
        with db.get_conn() as conn:
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    except Exception:
        pass
    if db.DB_PATH.exists():
        return FileResponse(str(db.DB_PATH), media_type="application/octet-stream",
                            filename="oi_chain.db")
    return JSONResponse({"error": "db not found"}, status_code=404)


# ---------- OI chain recorder status / control ----------

@app.get("/oi/status")
async def oi_status():
    """Inspection endpoint for the OI recorder. Shows row counts per
    (underlying, day), last few recorder runs, market-hours flag, and whether
    a Kite session is currently cached."""
    from kite_auth import get_kite_from_cache
    with db.get_conn() as conn:
        summary = db.day_summary(conn)
        runs = db.recent_recorder_runs(conn, limit=10)
    return JSONResponse({
        "market_hours_now": recorder.is_market_hours(),
        "kite_session_ok": get_kite_from_cache() is not None,
        "scheduler_running": _scheduler is not None and _scheduler.running,
        "day_summary": summary,
        "recent_runs": runs,
    })


SUPPORTED_UNDERLYINGS = ("NIFTY", "BANKNIFTY")


@app.get("/oi/days")
async def oi_days(underlying: str = "NIFTY"):
    """List trading days (newest first, max 30) that have stored chain data for
    the given underlying. Empty list means the OI Flow tab will show its empty
    state."""
    underlying = underlying.upper()
    if underlying not in SUPPORTED_UNDERLYINGS:
        raise HTTPException(status_code=400, detail=f"underlying must be one of {SUPPORTED_UNDERLYINGS}")
    with db.get_conn() as conn:
        days = db.available_days(conn, underlying=underlying, limit=30)
    return JSONResponse({"underlying": underlying, "days": days})


@app.get("/oi/aggregate")
async def oi_aggregate(
    underlying: str = "NIFTY",
    date: Optional[str] = None,
    mode: str = "premium",      # premium matches the reference indicator's ₹
                                 # scale: fetched same-day (Jun 15) fund-flow
                                 # totals match his within 0.5-1.3x in premium,
                                 # vs ~30x in margin and ~250x in notional.
                                 # (The earlier margin default was a misread of
                                 # a single big print; the table proves premium.)
    score_threshold_cr: float = 10.0,  # absolute floor; see threshold_mode
    n: int = 10,
    atm_band: int = 2,
    threshold_mode: str = "adaptive",  # adaptive (trailing 60-min mean+2σ) | absolute
    score_basis: str = "combined",     # combined (write+buy) | writing (write only)
    cooldown_minutes: int = 10,        # min spacing between markers (non-max suppression)
    roll_window_minutes: int = 20,     # trailing window for the adaptive threshold
    days: int = 1,                     # 1 = single day; >1 = continuous multi-day view
    flow_basis: str = "volume",        # volume (reference method) | oi (net positioning)
    trend_window_minutes: int = 1,     # price-trend window for the write/buy split.
                                       # 1 = raw 1-min tick (DEFAULT; verified the
                                       # closest fit to the reference's Jun-15
                                       # fund-flow). Longer = experimental knob.
    score_baseline_minutes: int = 90,  # stable baseline (trailing median) used as
                                       # the score denominator, decoupled from the
                                       # firing gate. Spreads scores 2-5 like the
                                       # reference; 0 = score off the gate threshold.
):
    """Return minute-aggregates + score markers + BIG-print list.

    days=1 → single day (the `date` param, or the latest available).
    days>1 → the most recent N available days concatenated into one
             continuous series (each day computed independently)."""
    underlying = underlying.upper()
    if underlying not in SUPPORTED_UNDERLYINGS:
        raise HTTPException(status_code=400, detail=f"underlying must be one of {SUPPORTED_UNDERLYINGS}")
    if mode not in ("premium", "notional", "margin"):
        raise HTTPException(status_code=400, detail="mode must be premium|notional|margin")
    if threshold_mode not in ("adaptive", "absolute"):
        raise HTTPException(status_code=400, detail="threshold_mode must be adaptive|absolute")
    if score_basis not in ("combined", "writing"):
        raise HTTPException(status_code=400, detail="score_basis must be combined|writing")
    if flow_basis not in ("volume", "oi"):
        raise HTTPException(status_code=400, detail="flow_basis must be volume|oi")
    try:
        thr = float(score_threshold_cr)
        n_i = int(n)
        atm_i = int(atm_band)
        cooldown_i = int(cooldown_minutes)
        roll_i = int(roll_window_minutes)
        days_i = int(days)
        trend_i = int(trend_window_minutes)
        scorebase_i = int(score_baseline_minutes)
    except Exception:
        raise HTTPException(status_code=400, detail="score_threshold_cr must be a number; n + atm_band + cooldown_minutes + roll_window_minutes + days + trend_window_minutes + score_baseline_minutes must be integers")
    if n_i < 1 or n_i > 50:
        raise HTTPException(status_code=400, detail="n must be 1..50")
    if atm_i < 0 or atm_i > 10:
        raise HTTPException(status_code=400, detail="atm_band must be 0..10")
    if cooldown_i < 0 or cooldown_i > 120:
        raise HTTPException(status_code=400, detail="cooldown_minutes must be 0..120")
    if roll_i < 5 or roll_i > 120:
        raise HTTPException(status_code=400, detail="roll_window_minutes must be 5..120")
    if days_i < 1 or days_i > 30:
        raise HTTPException(status_code=400, detail="days must be 1..30")
    if trend_i < 1 or trend_i > 120:
        raise HTTPException(status_code=400, detail="trend_window_minutes must be 1..120")
    if scorebase_i < 0 or scorebase_i > 375:
        raise HTTPException(status_code=400, detail="score_baseline_minutes must be 0..375")

    params = dict(
        mode=mode, score_threshold_cr=thr, n=n_i, atm_band=atm_i,
        threshold_mode=threshold_mode, score_basis=score_basis,
        cooldown_minutes=cooldown_i, roll_window_minutes=roll_i,
        flow_basis=flow_basis, trend_window_minutes=trend_i,
        score_baseline_minutes=scorebase_i,
    )
    with db.get_conn() as conn:
        if days_i > 1:
            # Continuous multi-day view: most recent N available days, oldest→newest.
            recent = db.available_days(conn, underlying=underlying, limit=days_i)
            recent = list(reversed(recent))  # available_days is newest-first
            if not recent:
                return JSONResponse(oi_flow.aggregate_day(
                    conn, underlying=underlying, date="", **params))
            return JSONResponse(oi_flow.aggregate_range(
                conn, underlying, recent, **params))
        if date is None:
            avail = db.available_days(conn, underlying=underlying, limit=1)
            if not avail:
                return JSONResponse(oi_flow.aggregate_day(
                    conn, underlying=underlying, date="", **params))
            date = avail[0]
        result = oi_flow.aggregate_day(conn, underlying=underlying, date=date, **params)
    return JSONResponse(result)


@app.post("/paper/seed")
async def paper_seed(x_auth_token: Optional[str] = Header(default=None)):
    """One-time book seeding: open paper positions for hold-status ('late')
    strategies from the latest data.json recs, re-quoted live. Idempotent —
    strategies already open are skipped. Same auth as /refresh."""
    expected = os.environ.get("REFRESH_TOKEN", "")
    if not (expected and x_auth_token == expected):
        from kite_auth import get_kite_from_cache as _gk
        if _gk() is None:
            raise HTTPException(status_code=401, detail="not authorised")
    from kite_auth import get_kite_from_cache
    kite = get_kite_from_cache()
    if kite is None:
        raise HTTPException(status_code=401, detail="no Kite session")
    text = load_data_text()
    if not text:
        raise HTTPException(status_code=400, detail="no data.json yet — run /refresh first")
    import paper as _paper
    with db.get_conn() as conn:
        result = _paper.seed_from_payload(kite, json.loads(text), conn)
    return JSONResponse(result)


@app.post("/paper/void/{trade_id}")
async def paper_void(trade_id: int, reason: str = "manual",
                     x_auth_token: Optional[str] = Header(default=None)):
    """Void an open paper trade (ledger maintenance — e.g. a mis-built
    structure). Closed with realized_pnl=0 and exit_reason='void:<reason>' so
    it never pollutes performance stats. Same auth as /refresh."""
    expected = os.environ.get("REFRESH_TOKEN", "")
    if not (expected and x_auth_token == expected):
        from kite_auth import get_kite_from_cache as _gk
        if _gk() is None:
            raise HTTPException(status_code=401, detail="not authorised")
    now_iso = _dt.datetime.now(recorder.IST).isoformat()
    with db.get_conn() as conn:
        row = conn.execute(
            "SELECT id FROM paper_trades WHERE id=? AND status='open'",
            (trade_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail=f"no open paper trade #{trade_id}")
        conn.execute(
            "UPDATE paper_trades SET status='closed', closed_ts=?,"
            " exit_reason=?, exit_value=0, realized_pnl=0 WHERE id=?",
            (now_iso, f"void:{reason}", trade_id))
        conn.commit()
    return JSONResponse({"voided": trade_id, "reason": reason})


@app.get("/paper/ledger")
async def paper_ledger():
    """Full paper-trading book: open positions, closed trades, per-strategy
    summary, and the latest mark per open trade. Read-only."""
    with db.get_conn() as conn:
        open_rows = [dict(r) for r in conn.execute(
            "SELECT * FROM paper_trades WHERE status='open' ORDER BY strategy")]
        closed_rows = [dict(r) for r in conn.execute(
            "SELECT * FROM paper_trades WHERE status='closed' "
            "ORDER BY closed_ts DESC LIMIT 100")]
        for r in open_rows + closed_rows:
            try:
                r["legs"] = json.loads(r.pop("legs_json"))
                r["meta"] = json.loads(r.pop("meta_json") or "{}")
            except Exception:
                pass
        for r in open_rows:
            m = conn.execute(
                "SELECT ts, mark_value, upnl, spot, note FROM paper_marks "
                "WHERE trade_id=? ORDER BY ts DESC LIMIT 1", (r["id"],)).fetchone()
            r["last_mark"] = dict(m) if m else None
        summary = [dict(r) for r in conn.execute(
            "SELECT strategy, COUNT(*) n, SUM(CASE WHEN realized_pnl>0 THEN 1 ELSE 0 END) wins,"
            " ROUND(COALESCE(SUM(realized_pnl),0)) realized_rs"
            " FROM paper_trades WHERE status='closed' GROUP BY strategy")]
    return JSONResponse({"open": open_rows, "closed": closed_rows,
                         "summary_by_strategy": summary})


@app.get("/oi/llt")
async def oi_llt(days: int = 5, date: Optional[str] = None):
    """Large-Lot-Trader prints on NIFTY current-month futures (minute
    resolution; see llt.py). `days` = most recent N sessions with futures
    data (aligned with the OI Flow chart view), or a single `date`."""
    import llt as _llt
    days = max(1, min(int(days), 30))
    with db.get_conn() as conn:
        if date:
            dates = [date]
        else:
            dates = [r[0] for r in conn.execute(
                "SELECT DISTINCT DATE(ts) FROM futures_minute ORDER BY 1 DESC LIMIT ?",
                (days,)).fetchall()]
            dates.reverse()
        prints = _llt.prints_for_dates(conn, dates)
    return JSONResponse({"dates": dates, "prints": prints,
                         "config": {"min_lots": _llt.LLT_MIN_LOTS,
                                    "mad_k": _llt.LLT_MAD_K,
                                    "conf_window_min": _llt.LLT_CONF_WINDOW}})


@app.get("/oi/marker_analysis")
async def oi_marker_analysis():
    """Aggregate forward-return stats across all stored score markers.

    For each (side, score) bucket and at each horizon (+5/+15/+30 min),
    returns the sample count, mean return in bps, and the directional
    hit-rate (put_writing → return > 0 = win; call_writing → return < 0 = win).
    Also returns per-side aggregates and per-day counts so you can see
    how the sample size is accumulating."""
    with db.get_conn() as conn:
        return JSONResponse(db.marker_outcomes_summary(conn))


@app.post("/oi/snapshot-now")
async def oi_snapshot_now(x_auth_token: Optional[str] = Header(default=None)):
    """Trigger a one-shot snapshot ignoring the market-hours guard. Same auth
    model as /refresh: header token OR valid Kite session."""
    expected = os.environ.get("REFRESH_TOKEN", "")
    if expected and x_auth_token == expected:
        pass
    else:
        from kite_auth import get_kite_from_cache
        if get_kite_from_cache() is None:
            raise HTTPException(status_code=401, detail="not authorised")
    return JSONResponse(recorder.run_snapshot(force=True))


@app.post("/oi/backfill")
async def oi_backfill(
    date: Optional[str] = None,
    underlying: Optional[str] = None,
    x_auth_token: Optional[str] = Header(default=None),
):
    """Reconstruct missing chain_snapshot rows for a day from Kite history
    (fills the gap left when the recorder wasn't running). `date` defaults to
    today IST; `underlying` (NIFTY|BANKNIFTY) defaults to all. Idempotent —
    minutes already captured live are dedup-ignored. Same auth as /refresh."""
    expected = os.environ.get("REFRESH_TOKEN", "")
    if expected and x_auth_token == expected:
        pass
    else:
        from kite_auth import get_kite_from_cache as _gk
        if _gk() is None:
            raise HTTPException(status_code=401, detail="not authorised")
    from kite_auth import get_kite_from_cache
    kite = get_kite_from_cache()
    if kite is None:
        raise HTTPException(status_code=401, detail="no Kite session")

    day = None
    if date:
        try:
            day = _dt.date.fromisoformat(date)
        except ValueError:
            raise HTTPException(status_code=400, detail="date must be YYYY-MM-DD")

    if underlying:
        underlying = underlying.upper()
        if underlying not in recorder.UNDERLYINGS:
            raise HTTPException(status_code=400, detail=f"underlying must be one of {list(recorder.UNDERLYINGS)}")
        conf = recorder.UNDERLYINGS[underlying]
        target_day = day or _dt.datetime.now(recorder.IST).date()
        return JSONResponse(recorder.backfill_day(kite, underlying, conf, target_day))
    return JSONResponse(recorder.backfill_underlyings(kite, day=day))


@app.get("/login")
async def login():
    return RedirectResponse(login_url())


@app.get("/callback")
async def callback(
    request_token: Optional[str] = None,
    status: Optional[str] = None,
    action: Optional[str] = None,
):
    if status and status != "success":
        return PlainTextResponse(f"Login failed: status={status}", status_code=400)
    if not request_token:
        return PlainTextResponse("Missing request_token in callback", status_code=400)
    try:
        session = exchange_request_token(request_token)
    except Exception as e:
        return PlainTextResponse(f"Token exchange failed: {e}", status_code=400)
    write_cached_session(session["access_token"], session.get("user_id", ""))
    user_id = session.get("user_id", "")
    # Show a nice landing page; auto-refresh the dashboard via meta-refresh.
    return HTMLResponse(f"""<!doctype html>
<html><head><meta charset="utf-8"><title>Logged in</title>
<meta http-equiv="refresh" content="2;url=/">
<style>
body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", system-ui, sans-serif;
       background: #0b0e14; color: #e7ecf3; padding: 60px 30px; text-align: center; }}
.box {{ display:inline-block; background:#11151d; border:1px solid #232a36; padding:30px 40px; border-radius:12px; }}
h1 {{ color:#7bd88f; margin:0 0 10px; font-size:22px; }}
.uid {{ color:#8b95a7; font-family: ui-monospace, "SF Mono", monospace; }}
.hint {{ color:#8b95a7; margin-top:14px; font-size:13px; }}
a {{ color:#4ea8ff; }}
</style></head>
<body><div class="box">
<h1>&check; Logged in</h1>
<div class="uid">{user_id}</div>
<div class="hint">Redirecting to dashboard… or <a href="/">click here</a>.</div>
</div></body></html>""")


@app.post("/refresh")
async def refresh(x_auth_token: Optional[str] = Header(default=None)):
    """
    Run compute.py via subprocess. Returns combined stdout+stderr.

    Auth model: requires either (a) an X-Auth-Token header matching the
    REFRESH_TOKEN env var (used by the cron scheduler), OR (b) a valid Kite
    session cached on this server (i.e. the user is logged in). Anonymous
    callers without either are rejected.
    """
    expected = os.environ.get("REFRESH_TOKEN", "")
    token_ok = bool(expected) and x_auth_token == expected
    # Fall back to session-based auth: if a valid Kite session is cached, allow.
    session_ok = False
    if not token_ok:
        try:
            from kite_auth import get_kite_from_cache
            session_ok = get_kite_from_cache() is not None
        except Exception:
            session_ok = False
    if not (token_ok or session_ok):
        raise HTTPException(
            status_code=401,
            detail="refresh requires either X-Auth-Token header or a logged-in Kite session",
        )
    if not COMPUTE_PY.exists():
        return JSONResponse({"error": "compute.py missing"}, status_code=500)
    body = await _run_compute_subprocess()
    if DATA_JSON.exists():
        try:
            body["signals"] = json.loads(DATA_JSON.read_text()).get("signals", {})
        except Exception:
            pass
    return JSONResponse(body, status_code=200 if body.get("exit_code") == 0 else 500)


async def _run_compute_subprocess() -> dict:
    """Run compute.py headless via subprocess; returns exit code + log.
    Shared by POST /refresh and the 15:16 IST scheduled auto-refresh."""
    env = os.environ.copy()
    env["OPTIONS_HEADLESS"] = "1"
    # Use the same Python interpreter that's running uvicorn — guarantees
    # the venv with all deps is picked up, regardless of PATH or env.
    python_bin = os.environ.get("PYTHON_BIN") or sys.executable or "python3"
    proc = await asyncio.create_subprocess_exec(
        python_bin, str(COMPUTE_PY),
        cwd=str(ROOT),
        env=env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    out, _ = await proc.communicate()
    return {"exit_code": proc.returncode, "log": out.decode(errors="replace")}


async def _scheduled_refresh():
    """Daily 15:16 IST auto-refresh so the paper ledger opens/marks/exits on
    schedule days even when nobody clicks Refresh. No-ops (compute exits 2)
    when no Kite session is cached — the user hasn't logged in that day."""
    print("[scheduler] 15:16 IST auto-refresh starting")
    try:
        res = await _run_compute_subprocess()
        tail = (res.get("log") or "").strip().splitlines()[-3:]
        print(f"[scheduler] auto-refresh exit={res.get('exit_code')} · " + " | ".join(tail))
    except Exception as e:
        print(f"[scheduler] auto-refresh failed: {type(e).__name__}: {e}")
