"""
Storage abstraction.

If REDIS_URL env var is set (e.g. an Upstash Redis URL), data.json and the
Kite session are stored there — this is required on Render free tier so
state survives deploys (ephemeral filesystem).

If REDIS_URL is empty, falls back to local files in this directory.

The local file is always written too (when possible) so a local CLI run
still works the same way.
"""
from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).parent
DATA_FILE = ROOT / "data.json"
SESSION_FILE = ROOT / ".kite_session.json"

REDIS_URL = os.environ.get("REDIS_URL", "").strip()
_redis = None

if REDIS_URL:
    try:
        import redis  # type: ignore
        _redis = redis.from_url(REDIS_URL, decode_responses=True, socket_connect_timeout=5)
        _redis.ping()
    except Exception as e:
        print(f"WARN: Redis unavailable, falling back to local files: {e}")
        _redis = None


# ---------------- data.json ----------------

def store_data(payload: dict) -> None:
    """Save latest compute output to both Redis and local file."""
    text = json.dumps(payload, indent=2, default=str)
    try:
        DATA_FILE.write_text(text)
    except Exception:
        pass
    if _redis is not None:
        try:
            _redis.set("options:data:latest", text)
        except Exception as e:
            print(f"WARN: Redis set data failed: {e}")


def load_data_text() -> Optional[str]:
    """Return latest data.json text. Prefers Redis, falls back to local."""
    if _redis is not None:
        try:
            v = _redis.get("options:data:latest")
            if v:
                return v
        except Exception:
            pass
    if DATA_FILE.exists():
        try:
            return DATA_FILE.read_text()
        except Exception:
            pass
    return None


def load_data() -> Optional[dict]:
    text = load_data_text()
    if not text:
        return None
    try:
        return json.loads(text)
    except Exception:
        return None


# ---------------- kite session ----------------

def store_session(access_token: str, user_id: str = "") -> None:
    payload = {
        "access_token": access_token,
        "user_id": user_id,
        "saved_at": datetime.utcnow().isoformat(),
    }
    text = json.dumps(payload)
    try:
        SESSION_FILE.write_text(text)
        try:
            os.chmod(SESSION_FILE, 0o600)
        except Exception:
            pass
    except Exception:
        pass
    if _redis is not None:
        try:
            # 24h TTL — Kite token expires daily at 6 AM IST anyway
            _redis.set("options:session:kite", text, ex=24 * 3600)
        except Exception as e:
            print(f"WARN: Redis set session failed: {e}")


def load_session() -> Optional[dict]:
    if _redis is not None:
        try:
            v = _redis.get("options:session:kite")
            if v:
                return json.loads(v)
        except Exception:
            pass
    if SESSION_FILE.exists():
        try:
            return json.loads(SESSION_FILE.read_text())
        except Exception:
            pass
    return None


def storage_info() -> dict:
    return {
        "redis_configured": bool(REDIS_URL),
        "redis_connected": _redis is not None,
    }
