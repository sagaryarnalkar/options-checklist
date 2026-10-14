"""Fyers API session — used ONLY for the expired-F&O historical archive.

Kite stays the live broker for everything else. Fyers is here for one reason:
it serves 1-minute candles WITH open interest for EXPIRED NSE F&O contracts
back to 2022-01-03, which Kite cannot (Kite drops a contract's token the day
it expires — that is why the 2026-08-24 gap was unrecoverable from Kite).

FLOW (Fyers API v3, read from myapi.fyers.in/docsv3 on 2026-10-14):
  1. GET  /api/v3/generate-authcode?client_id&redirect_uri&response_type=code&state
     -> the account holder logs in in a browser -> Fyers redirects to
        redirect_uri?auth_code=...&state=...
  2. POST /api/v3/validate-authcode {grant_type, appIdHash, code} -> access_token
  3. every request carries   Authorization: <app_id>:<access_token>

  appIdHash = sha256("<app_id>:<app_secret>")

NO UNATTENDED RENEWAL. Access tokens expire daily at 06:30 IST. Fyers' refresh
tokens were discontinued from 1 April 2026 (SEBI retail-algo rules) — and the
refresh call required the user's PIN anyway, which this code must never hold.
So: one browser login per day the crawler runs, and the crawl is resumable
across logins.

SECRETS. FYERS_APP_ID / FYERS_SECRET_KEY / FYERS_REDIRECT_URI come from the
environment or .env (gitignored). The token is cached at
DATA_DIR/.fyers_session.json — under data/, which the COMMITTED .gitignore
already excludes. This repo is public; nothing here may print the secret.
"""
from __future__ import annotations

import hashlib
import json
import os
import secrets as _secrets
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional
from urllib.parse import urlencode

import requests

ROOT = Path(__file__).parent
ENV_FILE = ROOT / ".env"
DATA_DIR = Path(os.environ.get("OPTIONS_DATA_DIR", str(ROOT / "data")))
SESSION_FILE = DATA_DIR / ".fyers_session.json"
STATE_FILE = DATA_DIR / ".fyers_state.json"

API = "https://api-t1.fyers.in"
IST = timezone(timedelta(hours=5, minutes=30))
STATE_TTL_S = 15 * 60          # a login link is good for 15 minutes


def load_env() -> Optional[dict]:
    """App credentials, or None when not configured. Never exits: the web app
    imports this, and a missing key must degrade to 'not configured', not kill
    the worker the way kite_auth.load_env's sys.exit would."""
    vals = {k: os.environ.get(k) for k in
            ("FYERS_APP_ID", "FYERS_SECRET_KEY", "FYERS_REDIRECT_URI")}
    if not all(vals.values()) and ENV_FILE.exists():
        for line in ENV_FILE.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            k, v = k.strip(), v.strip().strip('"').strip("'")
            if k in vals and not vals[k]:
                vals[k] = v
    if not all(vals.values()):
        return None
    return {"app_id": vals["FYERS_APP_ID"], "secret": vals["FYERS_SECRET_KEY"],
            "redirect_uri": vals["FYERS_REDIRECT_URI"]}


def app_id_hash(app_id: str, secret: str) -> str:
    return hashlib.sha256(f"{app_id}:{secret}".encode()).hexdigest()


def _next_expiry(created: datetime) -> datetime:
    """Tokens die at 06:30 IST. One minted at 05:00 dies 90 minutes later; one
    minted at 08:00 lives until 06:30 tomorrow."""
    cut = created.astimezone(IST).replace(hour=6, minute=30, second=0, microsecond=0)
    return cut if created.astimezone(IST) < cut else cut + timedelta(days=1)


def login_url() -> Optional[str]:
    """A fresh login link with a one-time `state`, checked on the callback so
    an auth_code can only complete a login this server actually started."""
    env = load_env()
    if not env:
        return None
    state = _secrets.token_urlsafe(16)
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps({"state": state, "ts": time.time()}))
    return f"{API}/api/v3/generate-authcode?" + urlencode({
        "client_id": env["app_id"], "redirect_uri": env["redirect_uri"],
        "response_type": "code", "state": state})


def check_state(state: Optional[str]) -> bool:
    try:
        d = json.loads(STATE_FILE.read_text())
    except Exception:
        return False
    ok = bool(state) and _secrets.compare_digest(str(state), str(d.get("state")))
    ok = ok and (time.time() - float(d.get("ts", 0))) < STATE_TTL_S
    if ok:
        STATE_FILE.unlink(missing_ok=True)      # one use only
    return ok


def exchange(auth_code: str) -> dict:
    """auth_code -> access_token, cached. Raises with Fyers' own message."""
    env = load_env()
    if not env:
        raise RuntimeError("Fyers not configured (FYERS_APP_ID / FYERS_SECRET_KEY / "
                           "FYERS_REDIRECT_URI missing from env/.env)")
    r = requests.post(f"{API}/api/v3/validate-authcode", timeout=20, json={
        "grant_type": "authorization_code",
        "appIdHash": app_id_hash(env["app_id"], env["secret"]),
        "code": auth_code})
    d = r.json() if r.content else {}
    if d.get("s") != "ok" or not d.get("access_token"):
        raise RuntimeError(f"validate-authcode failed: code={d.get('code')} "
                           f"message={d.get('message')!r}")
    now = datetime.now(IST)
    sess = {"access_token": d["access_token"], "app_id": env["app_id"],
            "created": now.isoformat(), "expires": _next_expiry(now).isoformat()}
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    SESSION_FILE.write_text(json.dumps(sess))
    try:
        os.chmod(SESSION_FILE, 0o600)
    except Exception:
        pass
    return {"ok": True, "expires": sess["expires"]}


def read_session() -> Optional[dict]:
    try:
        s = json.loads(SESSION_FILE.read_text())
    except Exception:
        return None
    try:
        if datetime.fromisoformat(s["expires"]) <= datetime.now(IST):
            return None
    except Exception:
        return None
    return s


def auth_header() -> Optional[str]:
    s = read_session()
    return f"{s['app_id']}:{s['access_token']}" if s else None


def logout() -> None:
    SESSION_FILE.unlink(missing_ok=True)


def status() -> dict:
    """Safe to serve publicly: no token, no secret, no app id."""
    s = read_session()
    return {"configured": load_env() is not None,
            "logged_in": s is not None,
            "expires": s["expires"] if s else None}


if __name__ == "__main__":
    # CLI login for running the crawler somewhere other than the droplet:
    # open the link, log in, then paste the URL the browser lands on.
    url = login_url()
    if not url:
        raise SystemExit("Set FYERS_APP_ID, FYERS_SECRET_KEY, FYERS_REDIRECT_URI in .env first.")
    print("1. Open and log in to Fyers:\n\n   " + url + "\n")
    landed = input("2. Paste the full URL your browser landed on: ").strip()
    from urllib.parse import parse_qs, urlparse
    q = parse_qs(urlparse(landed).query)
    if not check_state((q.get("state") or [None])[0]):
        raise SystemExit("state mismatch or expired link — run again.")
    # NOT `code`: in Fyers' redirect that is a status number (code=200).
    print(exchange((q.get("auth_code") or [""])[0]))
