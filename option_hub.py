"""Option Hub tab — Mr. Chartist's open-source options terminal, self-hosted beside this app.

Option Hub (https://github.com/MrChartist/india-s-best-option-hub) is a React app plus a Node data
proxy. deploy/option_hub.sh builds it on the droplet OUTSIDE this repo (~/option-hub, pinned commit,
its files never edited) and serves it at hub.<domain>; the dashboard's "Option Hub" tab frames it.

OWNER-ONLY, on purpose:
  * Its licence (Option Hub Personal Use Licence 1.0, s.3.3) forbids running it as a website "used
    by anyone other than you". This dashboard is public, so the hub must not be.
  * Its proxy can hold broker credentials and its UI can place REAL orders once a broker is
    connected in its Broker Settings.

The lock is a cookie checked by CADDY itself, not by this app: hub.<domain> answers 401 unless the
request carries hz_hub=<token>, token = HMAC-SHA256(ASSISTANT_KEY, "option-hub-cookie-v1"). The
setup script writes the token into the Caddyfile; POST /hub/session (X-App-Key, same key as the
Positions tab) sets the cookie on the parent domain so hub.<domain> receives it. Caddy-side rather
than forward_auth: every hub asset (≈210 precached files) would otherwise be a round trip into this
app, and forward_auth relays a WebSocket upgrade to the auth endpoint, which uvicorn[standard]
would try to upgrade — the hub's live feed (/ws) would never pass. duckdns.org is on the Public
Suffix List, so sagy-options.duckdns.org is the registrable domain: hub.* is same-site and a
SameSite=Lax cookie reaches it inside the iframe.

Changing ASSISTANT_KEY changes the token → re-run deploy/option_hub.sh to update Caddy.

CLI: python3 option_hub.py token   (prints the token for the current .env's ASSISTANT_KEY)
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import sys
import urllib.request
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

COOKIE = "hz_hub"
COOKIE_MAX_AGE = 30 * 86400
_TOKEN_MSG = b"option-hub-cookie-v1"

STATIC_PORT = int(os.environ.get("OPTION_HUB_PORT", "4001"))
PROXY_PORT = int(os.environ.get("OPTION_HUB_PROXY_PORT", "4002"))


def hub_url() -> Optional[str]:
    """OPTION_HUB_URL from .env (e.g. https://hub.sagy-options.duckdns.org), or None = not set up."""
    u = (os.environ.get("OPTION_HUB_URL") or "").strip().rstrip("/")
    p = urlparse(u)
    if p.scheme not in ("http", "https") or not p.hostname:
        return None
    return u


def cookie_token(key: str) -> str:
    return hmac.new(key.encode(), _TOKEN_MSG, hashlib.sha256).hexdigest()


def cookie_domain(request_host: str, hub: Optional[str]) -> Optional[str]:
    """The Domain attribute that lets the dashboard's cookie reach the hub host.

    Dashboard on sagy-options.duckdns.org + hub on hub.sagy-options.duckdns.org → the dashboard's own
    host. Anything else (localhost testing, hub on an unrelated host) → None = host-only cookie.
    Cookies ignore ports, so localhost:8000 → localhost:4001 needs no Domain at all."""
    rh = (request_host or "").split(":")[0].strip().lower().rstrip(".")
    hh = (urlparse(hub).hostname or "").lower() if hub else ""
    if rh and "." in rh and hh.endswith("." + rh):
        return rh
    return None


def _probe(url: str, timeout: float = 1.5) -> Optional[dict]:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            body = r.read(4096)
            try:
                return {"status": r.status, "json": json.loads(body)}
            except ValueError:
                return {"status": r.status}
    except Exception as e:  # noqa: BLE001 — any failure = not reachable
        return {"error": type(e).__name__}


def status() -> dict:
    """What the tab needs before it frames the hub. Probes the two local processes directly (not
    through Caddy, which would 401 without the cookie)."""
    u = hub_url()
    out: dict = {"configured": bool(u), "url": u}
    if not u:
        return out
    st = _probe(f"http://127.0.0.1:{STATIC_PORT}/healthz")
    px = _probe(f"http://127.0.0.1:{PROXY_PORT}/health")
    out["frontend_ok"] = bool(st and st.get("status") == 200)
    out["proxy_ok"] = bool(px and px.get("status") == 200)
    j = (px or {}).get("json") or {}
    if out["proxy_ok"]:
        out["proxy"] = {"version": j.get("version"), "uptime_s": j.get("uptime")}
    return out


def _env_key() -> str:
    """ASSISTANT_KEY from the environment, else from this repo's .env (the CLI runs outside systemd)."""
    k = os.environ.get("ASSISTANT_KEY", "")
    if k:
        return k
    env = Path(__file__).resolve().parent / ".env"
    try:
        for line in env.read_text().splitlines():
            line = line.strip()
            if line.startswith("ASSISTANT_KEY=") or line.startswith("export ASSISTANT_KEY="):
                v = line.split("=", 1)[1].strip()
                if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
                    v = v[1:-1]
                return v
    except OSError:
        pass
    return ""


if __name__ == "__main__":
    if sys.argv[1:] == ["token"]:
        k = _env_key()
        if not k:
            sys.exit("ASSISTANT_KEY is not set (environment or .env) — add it first")
        print(cookie_token(k))
    else:
        sys.exit("usage: python3 option_hub.py token")
