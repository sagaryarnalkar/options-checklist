"""
FastAPI wrapper for the Options Checklist data layer.

Routes:
    GET  /            -> index.html
    GET  /data.json   -> the most recent data
    GET  /login       -> redirects to Kite login
    GET  /callback    -> Kite OAuth redirect lands here
    POST /refresh     -> re-runs compute.py and returns the log
    GET  /healthz     -> 200 OK

Run: uvicorn app:app --host 127.0.0.1 --port 8000
"""
from __future__ import annotations

import asyncio
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
    mode: str = "premium",      # premium matches the reference HFT Algo
                                 # Scanner scale (peak ~10-30 cr/min on NIFTY
                                 # vs ~hundreds-of-cr if we used notional).
    score_threshold_cr: float = 10.0,  # tuned for premium-mode realistic peaks
    n: int = 10,
    atm_band: int = 2,
):
    """Return minute-aggregates + score markers + BIG-print list."""
    underlying = underlying.upper()
    if underlying not in SUPPORTED_UNDERLYINGS:
        raise HTTPException(status_code=400, detail=f"underlying must be one of {SUPPORTED_UNDERLYINGS}")
    if mode not in ("premium", "notional", "margin"):
        raise HTTPException(status_code=400, detail="mode must be premium|notional|margin")
    try:
        thr = float(score_threshold_cr)
        n_i = int(n)
        atm_i = int(atm_band)
    except Exception:
        raise HTTPException(status_code=400, detail="score_threshold_cr must be a number; n + atm_band must be integers")
    if n_i < 1 or n_i > 50:
        raise HTTPException(status_code=400, detail="n must be 1..50")
    if atm_i < 0 or atm_i > 10:
        raise HTTPException(status_code=400, detail="atm_band must be 0..10")

    with db.get_conn() as conn:
        if date is None:
            days = db.available_days(conn, underlying=underlying, limit=1)
            if not days:
                return JSONResponse(oi_flow.aggregate_day(
                    conn, underlying=underlying, date="",
                    mode=mode, score_threshold_cr=thr, n=n_i, atm_band=atm_i,
                ))
            date = days[0]
        result = oi_flow.aggregate_day(
            conn, underlying=underlying, date=date,
            mode=mode, score_threshold_cr=thr, n=n_i, atm_band=atm_i,
        )
    return JSONResponse(result)


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
    body = {
        "exit_code": proc.returncode,
        "log": out.decode(errors="replace"),
    }
    if DATA_JSON.exists():
        try:
            body["signals"] = json.loads(DATA_JSON.read_text()).get("signals", {})
        except Exception:
            pass
    return JSONResponse(body, status_code=200 if proc.returncode == 0 else 500)
