"""Option Hub tab — Mr. Chartist's options terminal, part of this repo and run BY this app.

Option Hub (upstream https://github.com/MrChartist/india-s-best-option-hub) is a React app plus a
Node data proxy. Its source lives in optionhub/ (a git subtree; our changes are listed in
optionhub/OPTIONS-CHECKLIST-CHANGES.md) and we edit it like our own code. There is no second service
to maintain: this app builds and supervises it (the RUNTIME section below), so a normal
`git pull && systemctl --user restart options-app` deploys both. A change under optionhub/ changes
its content hash, and the next restart rebuilds it (outside market hours, or Build now).
Taking upstream updates: git subtree pull --prefix=optionhub <upstream> <ref> --squash.
Licence (optionhub/LICENSE, Option Hub Personal Use Licence 1.0): personal use, changes allowed,
sharing allowed with the LICENSE file and every copyright / "Required Notice" line kept (s.2.4,
s.3.7 — never remove them); s.3.3 forbids running it as a site used by anyone but its owner.

The only one-time, root-level step is the hub's Caddy site (deploy/option_hub.sh, sudo): the hub
must sit at the ROOT of its own host — his React router has no base path — so it lives at
hub.<domain>, which needs its own Caddy block and certificate.

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
import platform
import shutil
import signal
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

COOKIE = "hz_hub"
COOKIE_MAX_AGE = 30 * 86400
_TOKEN_MSG = b"option-hub-cookie-v1"

STATIC_PORT = int(os.environ.get("OPTION_HUB_PORT", "4001"))
PROXY_PORT = int(os.environ.get("OPTION_HUB_PROXY_PORT", "4002"))

# ---- versions: the hub's is the content hash of optionhub/ (source_version) -----------------------
NODE_VERSION = "v22.23.4"      # downloaded only when the server has no Node >= NODE_MIN_MAJOR
NODE_MIN_MAJOR = 20

REPO_DIR = Path(__file__).resolve().parent
SOURCE_DIR = REPO_DIR / "optionhub"
# not needed to build or run it (docs = 7 MB of screenshots); also never copied into a build
SOURCE_SKIP = {"node_modules", "dist", ".cache", ".git", "docs", "e2e"}
SERVE_MJS = REPO_DIR / "deploy" / "option_hub" / "serve.mjs"
IST = timezone(timedelta(hours=5, minutes=30))


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
            body = r.read(262144)  # his /health lists brokers + risk engine: more than a few KB
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
        # His own view of each data source: last success / last error (why a panel shows sample data).
        now_ms = time.time() * 1000
        ups = {}
        for src in ("nse", "yahoo", "bse"):
            h = (j.get("upstreams") or {}).get(src) or {}
            if any(h.get(k) for k in ("lastSuccess", "lastFailure")):
                ups[src] = {
                    "last_ok_age_s": round((now_ms - h["lastSuccess"]) / 1000) if h.get("lastSuccess") else None,
                    "last_fail_age_s": round((now_ms - h["lastFailure"]) / 1000) if h.get("lastFailure") else None,
                    "last_error": h.get("lastError"),
                }
        out["proxy"]["upstreams"] = ups
    out["runtime"] = RUNTIME.snapshot()
    return out


# =============================================================================== RUNTIME ===
# This app installs, builds and supervises the hub — one service to deploy, not two.
#
#   DATA_DIR/option-hub/
#     node/<NODE_VERSION>/     Node, only if the server has no Node >= 20 (sha256-checked download)
#     builds/<ref[:12]>/       his repo at that commit, built for OPTION_HUB_URL (stamp file)
#     build.log, serve.log     the tab shows their tail when something fails
#
# Rules the code below keeps:
#   * The build (npm ci + vite build) peaks at ~1.2 GB RAM and a minute of both CPUs, so it never
#     starts in market hours (the recorder runs every minute) unless the owner presses "Build now"
#     in the tab, and never without MEM_NEEDED_MB of RAM + swap headroom.
#   * A changed optionhub/ builds BESIDE the running one; the swap is a restart of serve.mjs. The old
#     build keeps serving until then, so an update never takes the hub down for the build.
#   * One supervisor per server (flock): a second app worker leaves it alone.
#   * serve.mjs runs in its own process group; stopping the app stops it and his proxy. systemd's
#     cgroup also contains both, so `systemctl --user restart options-app` restarts the hub.

MEM_NEEDED_MB = 1600
BUILD_BLOCK_IST = ((9, 0), (15, 45))          # weekday window with no automatic build
FAILED_RETRY_S = 30 * 60
STEP_TIMEOUT_S = {"install": 1200, "build": 1200, "prune": 600}


def _home() -> Path:
    from db import DATA_DIR
    return Path(DATA_DIR) / "option-hub"


def _source_files(src: Path = SOURCE_DIR):
    """Every file under optionhub/ that a build needs, as (relative posix path, Path), sorted."""
    out = []
    for root, dirs, files in os.walk(src):
        rel_root = Path(root).relative_to(src)
        dirs[:] = sorted(d for d in dirs if not (rel_root == Path(".") and d in SOURCE_SKIP)
                         and d not in ("node_modules", ".git"))
        for f in files:
            p = Path(root) / f
            out.append(((rel_root / f).as_posix(), p))
    out.sort()
    return out


def source_version(src: Path = SOURCE_DIR) -> str:
    """Content hash (sha256 of paths + bytes) of the hub's source. ~24 MB → well under a second.
    Content, not mtimes: a fresh clone or a checkout that rewrites files must not force a rebuild."""
    if not (src / "package.json").exists():
        raise FileNotFoundError(f"{src}/package.json missing")
    h = hashlib.sha256()
    for rel, p in _source_files(src):
        h.update(rel.encode() + b"\0")
        h.update(p.read_bytes())
        h.update(b"\0")
    return h.hexdigest()


def copy_source(dst: Path, src: Path = SOURCE_DIR) -> None:
    """Copy the files source_version() hashes into a fresh build directory."""
    for rel, p in _source_files(src):
        t = dst / rel
        t.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(p, t)


def _market_busy(now: Optional[datetime] = None) -> bool:
    now = now or datetime.now(IST)
    (h0, m0), (h1, m1) = BUILD_BLOCK_IST
    return now.weekday() < 5 and (h0, m0) <= (now.hour, now.minute) <= (h1, m1)


def _mem_headroom_mb() -> Optional[int]:
    """MemAvailable + SwapFree in MB; None where /proc/meminfo does not exist (not Linux)."""
    try:
        info = {}
        for line in Path("/proc/meminfo").read_text().splitlines():
            k, v = line.split(":", 1)
            info[k] = int(v.split()[0]) // 1024
        return info.get("MemAvailable", 0) + info.get("SwapFree", 0)
    except (OSError, ValueError):
        return None


def _node_major(node: str) -> int:
    try:
        out = subprocess.run([node, "-p", "process.versions.node"], capture_output=True, text=True,
                             timeout=15).stdout.strip()
        return int(out.split(".")[0])
    except (OSError, ValueError, subprocess.SubprocessError):
        return 0


def _node_platform() -> Optional[str]:
    osname = {"Linux": "linux", "Darwin": "darwin"}.get(platform.system())
    arch = {"x86_64": "x64", "amd64": "x64", "aarch64": "arm64", "arm64": "arm64"}.get(platform.machine().lower())
    return f"{osname}-{arch}" if osname and arch else None


def find_node(home: Optional[Path] = None) -> Optional[dict]:
    """{"node": path, "bin": dir holding node+npm, "source": "system"|"bundled"} or None."""
    if os.environ.get("OPTION_HUB_NODE") != "bundled":
        sysnode = shutil.which("node")
        if sysnode and shutil.which("npm") and _node_major(sysnode) >= NODE_MIN_MAJOR:
            return {"node": sysnode, "bin": str(Path(sysnode).parent), "source": "system"}
    home = home or _home()
    plat = _node_platform()
    if plat:
        b = home / "node" / NODE_VERSION / "bin"
        if (b / "node").exists() and (b / "npm").exists():
            return {"node": str(b / "node"), "bin": str(b), "source": "bundled"}
    return None


def _download(url: str, dest: Path, timeout: int = 120) -> None:
    req = urllib.request.Request(url, headers={"User-Agent": "options-checklist-option-hub"})
    with urllib.request.urlopen(req, timeout=timeout) as r, open(dest, "wb") as f:
        shutil.copyfileobj(r, f, 1 << 20)


def install_node(home: Optional[Path] = None) -> dict:
    """Official Node tarball → home/node/NODE_VERSION, verified against nodejs.org's SHASUMS256.
    No sudo, nothing outside DATA_DIR."""
    home = home or _home()
    plat = _node_platform()
    if not plat:
        raise RuntimeError(f"no Node build for {platform.system()} {platform.machine()} — install Node {NODE_MIN_MAJOR}+")
    name = f"node-{NODE_VERSION}-{plat}.tar.gz"
    base = f"https://nodejs.org/dist/{NODE_VERSION}"
    dst = home / "node" / NODE_VERSION
    tmp = Path(tempfile.mkdtemp(prefix=".node-", dir=str(home)))
    try:
        _download(f"{base}/SHASUMS256.txt", tmp / "SHASUMS256.txt", timeout=30)
        want = next((ln.split()[0] for ln in (tmp / "SHASUMS256.txt").read_text().splitlines()
                     if ln.strip().endswith("  " + name)), None)
        if not want:
            raise RuntimeError(f"{name} missing from SHASUMS256.txt")
        _download(f"{base}/{name}", tmp / name, timeout=300)
        h = hashlib.sha256()
        with open(tmp / name, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
        if h.hexdigest() != want:
            raise RuntimeError(f"{name}: sha256 mismatch — refusing to install")
        out = tmp / "x"
        out.mkdir()
        root = out.resolve()
        with tarfile.open(tmp / name, "r:gz") as t:
            for m in t.getmembers():       # no absolute paths / .. / device files
                p = (out / m.name).resolve()
                if not (str(p) == str(root) or str(p).startswith(str(root) + os.sep)) or m.isdev():
                    raise RuntimeError(f"unsafe path in {name}: {m.name}")
                if m.issym() or m.islnk():
                    lt = (p.parent / m.linkname).resolve() if m.issym() else (out / m.linkname).resolve()
                    if not str(lt).startswith(str(root) + os.sep):
                        raise RuntimeError(f"unsafe link in {name}: {m.name} -> {m.linkname}")
            if hasattr(tarfile, "data_filter"):
                t.extractall(out, filter="data")
            else:
                t.extractall(out)
        (dst.parent).mkdir(parents=True, exist_ok=True)
        if dst.exists():
            shutil.rmtree(dst)
        (out / name[:-len(".tar.gz")]).rename(dst)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    found = find_node(home)
    if not found:
        raise RuntimeError("Node installed but node/npm not found in it")
    return found


def _stamp(ref: str, url: str) -> str:
    return f"{ref} {url}"


def build_dir(ref: str, home: Optional[Path] = None) -> Path:
    return (home or _home()) / "builds" / ref[:12]


def is_built(ref: str, url: str, home: Optional[Path] = None) -> bool:
    d = build_dir(ref, home)
    try:
        return (d / "dist" / "index.html").exists() and \
            (d / ".occl-build").read_text().strip() == _stamp(ref, url)
    except OSError:
        return False


def _built_dirs(url: str, home: Optional[Path] = None) -> list:
    """Every finished build for this URL, newest first."""
    root = (home or _home()) / "builds"
    out = []
    for d in root.glob("*") if root.exists() else []:
        try:
            ref, u = (d / ".occl-build").read_text().strip().split(" ", 1)
        except (OSError, ValueError):
            continue
        if u == url and (d / "dist" / "index.html").exists():
            out.append((d.stat().st_mtime, d, ref))
    return [(d, ref) for _, d, ref in sorted(out, reverse=True)]


def _frame_ancestors(url: str) -> str:
    env = os.environ.get("OPTION_HUB_FRAME_ANCESTORS")
    if env:
        return env
    p = urlparse(url)
    host = p.hostname or ""
    if host.startswith("hub."):
        return f"{p.scheme}://{host[4:]}" + (f":{p.port}" if p.port else "")
    return "'self'"


def serve_env(url: str, build: Path, node_bin: str, token: str) -> dict:
    """The environment serve.mjs + his proxy run with. Nothing else of ours leaks in (no Kite/Fyers/
    Anthropic secrets): only the hub token, which optionhub/server/dashboard.mjs needs to reach /hub/feed/*."""
    p = urlparse(url)
    env = {k: v for k, v in os.environ.items()
           if k in ("HOME", "USER", "LANG", "LC_ALL", "TZ", "SSL_CERT_FILE", "SSL_CERT_DIR")}
    env.update({
        "PATH": node_bin + os.pathsep + os.environ.get("PATH", "/usr/bin:/bin"),
        "HUB_DIR": str(build),
        "PORT": str(STATIC_PORT), "HOST": "127.0.0.1",
        "PROXY_PORT": str(PROXY_PORT), "PROXY_HOST": "127.0.0.1",
        "PROXY_ALLOWED_ORIGINS": f"{p.scheme}://{p.netloc}",
        "PROXY_ALLOWED_HOSTS": p.netloc,
        "HUB_FRAME_ANCESTORS": _frame_ancestors(url),
        "HUB_FEED_URL": os.environ.get("OPTION_HUB_FEED_URL", "http://127.0.0.1:8000"),
        "HUB_FEED_TOKEN": token,
        "NODE_ENV": "production",
        "NODE_OPTIONS": "--max-old-space-size=384",
    })
    return env


class _Runtime:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._lockfile = None
        self.proc: Optional[subprocess.Popen] = None
        self.proc_dir: Optional[Path] = None
        self.build_proc: Optional[subprocess.Popen] = None
        self.force = False
        self.failed_at = 0.0
        self.restarts = 0
        self.version: Optional[str] = None   # content hash of optionhub/ this app wants running
        self.info: dict = {"state": "off", "detail": ""}

    # ---- status ------------------------------------------------------------------------
    def _set(self, state: str, detail: str = "", **kw) -> None:
        with self._lock:
            self.info = {"state": state, "detail": detail, "since": time.time(), **kw}

    def snapshot(self) -> dict:
        with self._lock:
            out = dict(self.info)
        out["since_s"] = round(time.time() - out.pop("since", time.time()))
        out.update({"version": self.version, "pid": self.proc.pid if self.proc and self.proc.poll() is None else None,
                    "running": self.proc_dir.name if self.proc_dir and self.proc else None,
                    "restarts": self.restarts, "market_hours_block": _market_busy()})
        if out["state"] in ("failed", "restarting", "low_memory"):
            log = "build.log" if out["state"] == "failed" and out.get("phase") == "build" else "serve.log"
            out["log_tail"] = _tail(_home() / log, 25)
        return out

    # ---- lifecycle ---------------------------------------------------------------------
    def start(self) -> None:
        if os.environ.get("OPTION_HUB_RUNTIME", "1").lower() in ("0", "false", "no", "off"):
            self._set("external", "OPTION_HUB_RUNTIME=0: this app does not run the hub")
            return
        if not hub_url():
            self._set("not_configured", "OPTION_HUB_URL is not set — run deploy/option_hub.sh once")
            return
        home = _home()
        home.mkdir(parents=True, exist_ok=True)
        try:
            import fcntl
            self._lockfile = open(home / ".runtime.lock", "w")
            fcntl.flock(self._lockfile, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (OSError, ImportError):
            self._set("other_worker", "another app process supervises the hub")
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="option-hub", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        _killpg(self.build_proc)
        _killpg(self.proc)
        if self._thread:
            self._thread.join(timeout=8)
        self.proc = None

    def request_build(self, force: bool) -> None:
        """'Build now' from the tab: retry a failure, and with force, ignore the market-hours rule."""
        self.force = bool(force)
        self.failed_at = 0.0
        self._wake.set()

    # ---- the loop ----------------------------------------------------------------------
    def _sleep(self, s: float) -> None:
        self._wake.wait(s)
        self._wake.clear()

    def _loop(self) -> None:
        url = hub_url()
        key = _env_key()
        if not key:
            self._set("failed", "ASSISTANT_KEY is not set — the hub's lock and feed are derived from it")
            return
        token = cookie_token(key)
        try:
            self.version = source_version()
        except OSError as e:
            self._set("failed", f"cannot read the hub's source ({SOURCE_DIR}): {e}", phase="runtime")
            return
        ver = self.version
        backoff, started = 5, time.time()
        while not self._stop.is_set():
            try:
                node = find_node()
                if not node:
                    self._set("installing", f"downloading Node {NODE_VERSION} (one time)", phase="node")
                    node = install_node()
                want_ready = is_built(ver, url)
                if want_ready:
                    self.force = False
                else:
                    # keep the previous build serving while this version builds (a deploy that
                    # changes optionhub/ would otherwise take the hub down for the whole build)
                    if not (self.proc and self.proc.poll() is None):
                        prev = _built_dirs(url)
                        if prev:
                            _killpg(self.proc)
                            _kill_stale(_home() / "serve.pid")
                            self.proc, self.proc_dir = _spawn(prev[0][0], node, url, token)
                            started = time.time()
                    want_ready = self._maybe_build(url, node)
                if self._stop.is_set():
                    break
                built = _built_dirs(url)
                run = build_dir(ver) if want_ready else (built[0][0] if built else None)
                if run is None:
                    self._sleep(60)
                    continue
                if not (self.proc and self.proc.poll() is None and self.proc_dir == run):
                    _killpg(self.proc)
                    _kill_stale(_home() / "serve.pid")
                    self.proc, self.proc_dir = _spawn(run, node, url, token)
                    started = time.time()
                pending = "" if want_ready else self.info.get("detail", "")
                if self.info.get("state") not in ("running",) or pending:
                    self._set("running", pending and f"running {run.name}; update to {ver[:12]} pending — {pending}",
                              node=node["source"])
                # watch the child; re-evaluate every minute (a pending update may now be buildable)
                for _ in range(30):
                    if self._stop.is_set() or self._wake.is_set():
                        break
                    if self.proc.poll() is not None:
                        break
                    time.sleep(2)
                self._wake.clear()
                if self.proc and self.proc.poll() is not None and not self._stop.is_set():
                    self.restarts += 1
                    code = self.proc.returncode
                    _killpg(self.proc)       # an orphaned proxy would hold its port forever
                    self.proc = None
                    if time.time() - started > 600:
                        backoff = 5
                    self._set("restarting", f"the hub exited ({code}); restarting in {backoff}s")
                    self._sleep(backoff)
                    backoff = min(backoff * 2, 120)
            except Exception as e:  # noqa: BLE001 — keep supervising; the tab shows the reason
                self._set("failed", f"{type(e).__name__}: {str(e)[:300]}", phase="runtime")
                self.failed_at = time.time()
                self._sleep(60)
        _killpg(self.proc)

    def _maybe_build(self, url: str, node: dict) -> bool:
        """Build optionhub/ (this version) if the rules allow it now. Returns True when it is built."""
        have_old = bool(_built_dirs(url))
        if not self.force:
            if _market_busy():
                self._set("waiting", "builds outside market hours (after 15:45 IST) — or press Build now",
                          phase="build")
                return False
            if self.failed_at and time.time() - self.failed_at < FAILED_RETRY_S:
                return False
        head = _mem_headroom_mb()
        if head is not None and head < MEM_NEEDED_MB:
            self._set("low_memory", f"needs {MEM_NEEDED_MB} MB of RAM+swap free for the build, has {head} MB. "
                      "Add swap once: sudo fallocate -l 2G /swapfile && sudo chmod 600 /swapfile && "
                      "sudo mkswap /swapfile && sudo swapon /swapfile && "
                      "echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab", phase="build")
            return False
        self.force = False
        try:
            self._build(url, node, keep_serving=have_old)
        except Exception as e:  # noqa: BLE001
            self.failed_at = time.time()
            self._set("failed", f"build of {self.version[:12]} failed: {str(e)[:300]} (retries in 30 min, or Build now)",
                      phase="build")
            return False
        return True

    def _build(self, url: str, node: dict, keep_serving: bool) -> None:
        home = _home()
        ver = self.version
        final = build_dir(ver, home)
        tmp = final.parent / f".tmp-{ver[:12]}-{os.getpid()}"
        shutil.rmtree(tmp, ignore_errors=True)
        tmp.parent.mkdir(parents=True, exist_ok=True)
        log = home / "build.log"
        log.write_text(f"[{datetime.now(IST):%Y-%m-%d %H:%M:%S}] build optionhub/ {ver} for {url} "
                       f"(node {node['source']} {node['node']})\n")
        env = {**os.environ, "PATH": node["bin"] + os.pathsep + os.environ.get("PATH", ""),
               "VITE_PROXY_URL": url, "VITE_DASHBOARD_KITE": "1", "NODE_OPTIONS": "--max-old-space-size=1536",
               "npm_config_update_notifier": "false", "CI": "1"}
        npm = str(Path(node["bin"]) / "npm")
        steps = [
            ("install", [npm, "ci", "--no-audit", "--no-fund", "--loglevel=error"], tmp),
            ("build", [npm, "run", "build", "--silent"], tmp),
            ("prune", [npm, "prune", "--omit=dev", "--no-audit", "--no-fund", "--loglevel=error"], tmp),
        ]
        try:
            self._set("building", f"copy (1/{len(steps) + 1})" +
                      (" — the current version keeps running meanwhile" if keep_serving else ""), phase="build")
            copy_source(tmp)
            for i, (name, cmd, cwd) in enumerate(steps, 2):
                if self._stop.is_set():
                    raise RuntimeError("stopped")
                self._set("building", f"{name} ({i}/{len(steps) + 1})" +
                          (" — the current version keeps running meanwhile" if keep_serving else ""), phase="build")
                self._run_step(name, cmd, cwd, env, log)
            if not (tmp / "dist" / "index.html").exists():
                raise RuntimeError("the build produced no dist/index.html")
            (tmp / ".occl-build").write_text(_stamp(ver, url) + "\n")
            if final.exists():
                shutil.rmtree(final)
            tmp.rename(final)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
        # keep this build and the one serving right now; anything older goes
        keep = {final.resolve()} | ({self.proc_dir.resolve()} if self.proc_dir else set())
        for d in final.parent.glob("*"):
            if d.is_dir() and d.resolve() not in keep and not d.name.startswith(".tmp-"):
                shutil.rmtree(d, ignore_errors=True)

    def _run_step(self, name: str, cmd: list, cwd: Path, env: dict, log: Path) -> None:
        if shutil.which("nice"):
            cmd = ["nice", "-n", "10", *cmd]
        with open(log, "a") as lf:
            lf.write(f"$ {' '.join(cmd)}\n")
            lf.flush()
            self.build_proc = subprocess.Popen(cmd, cwd=str(cwd), env=env, stdout=lf, stderr=subprocess.STDOUT,
                                               stdin=subprocess.DEVNULL, start_new_session=True)
            try:
                code = self.build_proc.wait(timeout=STEP_TIMEOUT_S[name])
            except subprocess.TimeoutExpired:
                _killpg(self.build_proc)
                raise RuntimeError(f"{name} timed out after {STEP_TIMEOUT_S[name]}s")
            finally:
                self.build_proc = None
        if code != 0:
            raise RuntimeError(f"{name} exited {code} — see build.log")


def _spawn(build: Path, node: dict, url: str, token: str):
    home = _home()
    log = home / "serve.log"
    try:
        if log.exists() and log.stat().st_size > 5_000_000:
            log.replace(home / "serve.log.1")
    except OSError:
        pass
    lf = open(log, "a")
    lf.write(f"\n[{datetime.now(IST):%Y-%m-%d %H:%M:%S}] start {build.name} (node {node['source']})\n")
    lf.flush()
    p = subprocess.Popen([node["node"], str(SERVE_MJS)], cwd=str(build), env=serve_env(url, build, node["bin"], token),
                         stdout=lf, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, start_new_session=True)
    lf.close()
    (home / "serve.pid").write_text(str(p.pid))
    return p, build


def _killpg(p: Optional[subprocess.Popen], grace: float = 5.0) -> None:
    """SIGTERM the process group (serve.mjs + his proxy), SIGKILL after `grace` seconds.

    Works on the GROUP even when its leader is already dead: if serve.mjs dies hard (SIGKILL, OOM)
    its exit hook never runs and his proxy lives on as an orphan holding :PROXY_PORT, so every
    restart would fail on "address in use" (seen in testing). The group id outlives the leader for
    as long as any member does, so killpg still reaches the orphan."""
    if not p:
        return
    pgid = p.pid
    try:
        os.killpg(pgid, signal.SIGTERM)
    except OSError:
        p.poll()
        return                       # no member left
    deadline = time.time() + grace
    while time.time() < deadline:
        p.poll()                     # reap the leader so a zombie does not keep the group "alive"
        try:
            os.killpg(pgid, 0)
        except OSError:
            return
        time.sleep(0.1)
    try:
        os.killpg(pgid, signal.SIGKILL)
    except OSError:
        pass
    p.poll()


def _our_group(pgid: int) -> bool:
    """Is process group `pgid` still made of our hub processes (serve.mjs / his proxy)?"""
    try:
        out = subprocess.run(["ps", "-A", "-o", "pgid=,command="], capture_output=True, text=True,
                             timeout=5).stdout
    except (OSError, subprocess.SubprocessError):
        return False
    cmds = [ln.strip().split(None, 1)[1] for ln in out.splitlines()
            if ln.strip() and ln.split(None, 1)[0] == str(pgid) and len(ln.split(None, 1)) > 1]
    return bool(cmds) and all(("serve.mjs" in c or "proxy-server.mjs" in c) for c in cmds)


def _kill_stale(pidfile: Path) -> None:
    """Hub processes left by a previous app process that died without cleanup — serve.mjs, or his
    proxy orphaned in serve.mjs's process group. Only a group made entirely of our hub processes is
    touched."""
    try:
        pgid = int(pidfile.read_text().strip())
    except (OSError, ValueError):
        return
    if not _our_group(pgid):
        return
    try:
        os.killpg(pgid, signal.SIGTERM)
        for _ in range(50):
            time.sleep(0.1)
            os.killpg(pgid, 0)
        os.killpg(pgid, signal.SIGKILL)
    except OSError:
        pass


def _tail(path: Path, n: int) -> list:
    try:
        return path.read_text(errors="replace").splitlines()[-n:]
    except OSError:
        return []


RUNTIME = _Runtime()


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
