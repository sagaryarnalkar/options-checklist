"""
Kite Connect session management.

Two modes:

1. CLI mode (your Mac): `get_kite()` does an interactive browser login through
   a local HTTP listener on 127.0.0.1:5010. Used by the bare `python3 compute.py`
   workflow.

2. Web mode (VPS): the FastAPI app uses the building blocks (load_env,
   read_cached_session, write_cached_session, get_kite_from_cache) and handles
   the redirect itself at /callback.

Token cache: `.kite_session.json` next to this file. Tokens expire daily at
6 AM IST (00:30 UTC) per SEBI rule.
"""
from __future__ import annotations

import json
import os
import sys
import threading
import webbrowser
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from kiteconnect import KiteConnect
from kiteconnect.exceptions import TokenException

from storage import load_session, store_session

ROOT = Path(__file__).parent
SESSION_FILE = ROOT / ".kite_session.json"  # legacy reference; storage handles writes now
ENV_FILE = ROOT / ".env"

# CLI-mode local listener config
REDIRECT_HOST = "127.0.0.1"
REDIRECT_PORT = 5010
LOCAL_REDIRECT = f"http://{REDIRECT_HOST}:{REDIRECT_PORT}"


# ---------------- env / config ----------------

def load_env() -> tuple[str, str]:
    """Read KITE_API_KEY and KITE_API_SECRET from environment, falling back to .env."""
    api_key = os.environ.get("KITE_API_KEY")
    api_secret = os.environ.get("KITE_API_SECRET")
    if (not api_key or not api_secret) and ENV_FILE.exists():
        for line in ENV_FILE.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            k, v = k.strip(), v.strip().strip('"').strip("'")
            if k == "KITE_API_KEY" and not api_key:
                api_key = v
            elif k == "KITE_API_SECRET" and not api_secret:
                api_secret = v
    if not api_key or not api_secret:
        sys.exit(
            "ERROR: KITE_API_KEY / KITE_API_SECRET not set in env or .env.\n"
            "  Create a .env file in this folder with:\n"
            "    KITE_API_KEY=xxxxxxxx\n"
            "    KITE_API_SECRET=xxxxxxxx"
        )
    return api_key, api_secret


# ---------------- token cache ----------------

def read_cached_session() -> dict | None:
    """Return cached session dict if it's still fresh (token not yet expired)."""
    data = load_session()
    if not data:
        return None
    saved = data.get("saved_at", "")
    try:
        saved_dt = datetime.fromisoformat(saved)
    except Exception:
        return None
    # Token expires at 6 AM IST = 00:30 UTC. A token is valid if it was
    # generated AFTER the most recent 00:30 UTC.
    now = datetime.utcnow()
    today_cutoff = now.replace(hour=0, minute=30, second=0, microsecond=0)
    cutoff = today_cutoff if now >= today_cutoff else today_cutoff - timedelta(days=1)
    if saved_dt < cutoff:
        return None
    return data


def write_cached_session(access_token: str, user_id: str = "") -> None:
    store_session(access_token, user_id)


# ---------------- web/headless helpers ----------------

def get_kite_from_cache() -> KiteConnect | None:
    """Return an authenticated KiteConnect using only the cached token, or None."""
    api_key, _ = load_env()
    cached = read_cached_session()
    if not cached:
        return None
    kite = KiteConnect(api_key=api_key)
    kite.set_access_token(cached["access_token"])
    try:
        kite.profile()
        return kite
    except (TokenException, Exception):
        return None


def exchange_request_token(request_token: str) -> dict:
    """Exchange a request_token for an access_token. Used by the web /callback."""
    api_key, api_secret = load_env()
    kite = KiteConnect(api_key=api_key)
    return kite.generate_session(request_token, api_secret=api_secret)


def login_url() -> str:
    """Return the Kite Connect login URL (uses redirect URL configured in app)."""
    api_key, _ = load_env()
    kite = KiteConnect(api_key=api_key)
    return kite.login_url()


# ---------------- CLI-mode local listener flow ----------------

class _RedirectHandler(BaseHTTPRequestHandler):
    captured: dict = {}

    def do_GET(self):  # noqa: N802
        qs = parse_qs(urlparse(self.path).query)
        if "request_token" in qs:
            _RedirectHandler.captured["request_token"] = qs["request_token"][0]
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(
                b"<html><body style='font-family:system-ui;padding:40px'>"
                b"<h2>&check; Login captured</h2>"
                b"<p>You can close this tab.</p></body></html>"
            )
        else:
            self.send_response(400); self.end_headers()
            self.wfile.write(b"missing request_token")

    def log_message(self, *a, **k):
        return


def _capture_request_token(login_url_str: str, open_browser: bool = False) -> str:
    server = HTTPServer((REDIRECT_HOST, REDIRECT_PORT), _RedirectHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    print("\n" + "=" * 70)
    print("  KITE LOGIN — open this URL in any browser:\n")
    print(f"  {login_url_str}\n")
    print("=" * 70)
    print(f"  Listening for redirect on {LOCAL_REDIRECT} ...")
    if open_browser:
        webbrowser.open(login_url_str)
    while "request_token" not in _RedirectHandler.captured:
        pass
    server.shutdown()
    return _RedirectHandler.captured["request_token"]


def get_kite(force_login: bool = False) -> KiteConnect:
    """Interactive CLI login flow with local listener. Used on your Mac."""
    api_key, api_secret = load_env()
    if not force_login:
        kite = get_kite_from_cache()
        if kite is not None:
            return kite
    kite = KiteConnect(api_key=api_key)
    request_token = _capture_request_token(kite.login_url())
    session = kite.generate_session(request_token, api_secret=api_secret)
    kite.set_access_token(session["access_token"])
    write_cached_session(session["access_token"], session.get("user_id", ""))
    print(f"  Logged in as {session.get('user_id')}. Token cached at {SESSION_FILE}")
    return kite


if __name__ == "__main__":
    k = get_kite(force_login="--force" in sys.argv)
    p = k.profile()
    print(f"OK — logged in as {p['user_id']} ({p['user_name']})")
