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

ROOT = Path(__file__).parent
INDEX_HTML = ROOT / "index.html"
DATA_JSON = ROOT / "data.json"
COMPUTE_PY = ROOT / "compute.py"

app = FastAPI(title="Options Checklist", docs_url=None, redoc_url=None)


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
    proc = await asyncio.create_subprocess_exec(
        os.environ.get("PYTHON_BIN", "python3"), str(COMPUTE_PY),
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
