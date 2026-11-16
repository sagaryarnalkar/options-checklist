#!/usr/bin/env bash
# Option Hub (Mr. Chartist's options terminal) on the droplet, owner-only, framed by the
# dashboard's "Option Hub" tab. See option_hub.py for why it is locked and how.
#
#   bash deploy/option_hub.sh                 # first install, or re-apply after a git pull
#   HUB_REF=main bash deploy/option_hub.sh    # update to his latest (default: the tested commit)
#
# Re-run after changing ASSISTANT_KEY (the hub's lock cookie is derived from it).
#
# What it does (sudo only for the Caddyfile, and optional swap):
#   1. checks Node >= 20, memory for the build (~1.2 GB peak), ASSISTANT_KEY
#   2. clones/updates ~/option-hub at HUB_REF — OUTSIDE this repo, his files never edited
#   3. npm ci + vite build (frontend talks to https://hub.<domain>), prunes dev deps
#   4. user-systemd unit option-hub → deploy/option_hub/serve.mjs (127.0.0.1:4001 + proxy :4002)
#   5. Caddy site block for hub.<domain>: 401 without the owner cookie, else route to 4001/4002
#   6. OPTION_HUB_URL in .env, restarts options-app, smoke-tests the lock and the NSE data path
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
HUB_DIR="${HUB_DIR:-$HOME/option-hub}"
HUB_REPO="https://github.com/MrChartist/india-s-best-option-hub.git"
HUB_REF="${HUB_REF:-18e1d52cd0ecdb99538fd4ca97b98c045ed89a95}"   # tested 2026-11-16
CADDYFILE="${CADDYFILE:-/etc/caddy/Caddyfile}"
MARK_BEGIN="# >>> option-hub (managed by options-checklist/deploy/option_hub.sh — edits here are overwritten)"
MARK_END="# <<< option-hub"
PY="$REPO_DIR/venv/bin/python3"; [[ -x "$PY" ]] || PY="python3"

say()  { printf '\n==> %s\n' "$*"; }
die()  { printf '\nERROR: %s\n' "$*" >&2; exit 1; }
ask()  { local a; read -r -p "$1 [y/N] " a; [[ "$a" =~ ^[Yy] ]]; }

[[ "$EUID" -ne 0 ]] || die "run as your regular user, not root"

# ---- domain --------------------------------------------------------------------------
if [[ -z "${DOMAIN:-}" && -r "$CADDYFILE" ]]; then
  DOMAIN="$(grep -E '^[A-Za-z0-9.-]+\.[A-Za-z]+[[:space:]]*\{' "$CADDYFILE" | grep -v '^hub\.' | head -1 | awk '{print $1}' || true)"
fi
[[ -n "${DOMAIN:-}" ]] || read -r -p "Dashboard domain (e.g. sagy-options.duckdns.org): " DOMAIN
[[ -n "${DOMAIN:-}" ]] || die "DOMAIN is required"
HUB_HOST="hub.$DOMAIN"
say "Option Hub → https://$HUB_HOST (framed by https://$DOMAIN), checkout $HUB_DIR @ ${HUB_REF:0:12}"

# ---- market hours: the build is CPU/RAM heavy and the recorder runs every minute --------
dow="$(TZ=Asia/Kolkata date +%u)"; hm="$(TZ=Asia/Kolkata date +%H%M)"
if (( dow <= 5 )) && (( 10#$hm >= 900 && 10#$hm <= 1545 )); then
  echo "It is market hours (IST). The build takes ~1 min of CPU and ~1.2 GB RAM next to the recorder."
  ask "Continue anyway?" || exit 1
fi

# ---- node -------------------------------------------------------------------------------
NODE_BIN="$(command -v node || true)"
major="$([[ -n "$NODE_BIN" ]] && "$NODE_BIN" -p 'process.versions.node.split(".")[0]' || echo 0)"
if (( major < 20 )); then
  cat >&2 <<'EOF'

Node.js 20+ is required (Ubuntu's apt ships 18). Install Node 22 from NodeSource, then re-run:

  curl -fsSL https://deb.nodesource.com/setup_22.x | sudo -E bash -
  sudo apt-get install -y nodejs
EOF
  exit 1
fi
echo "node $("$NODE_BIN" -v) at $NODE_BIN"

# ---- memory for the build ---------------------------------------------------------------
avail=$(awk '/MemAvailable/ {print int($2/1024)}' /proc/meminfo)
swapf=$(awk '/SwapFree/ {print int($2/1024)}' /proc/meminfo)
echo "memory: ${avail} MB available, ${swapf} MB swap free (build peaks ~1200 MB)"
if (( avail + swapf < 1600 )); then
  echo "Not enough headroom for the build. A 2 GB swap file fixes it:"
  echo "  sudo fallocate -l 2G /swapfile && sudo chmod 600 /swapfile && sudo mkswap /swapfile && sudo swapon /swapfile"
  echo "  echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab"
  if ask "Create it now?"; then
    [[ -e /swapfile ]] && die "/swapfile already exists — enable it with: sudo swapon /swapfile"
    sudo fallocate -l 2G /swapfile && sudo chmod 600 /swapfile && sudo mkswap /swapfile >/dev/null && sudo swapon /swapfile
    grep -q '^/swapfile ' /etc/fstab || echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab >/dev/null
  else
    exit 1
  fi
fi

# ---- lock token -------------------------------------------------------------------------
TOKEN="$("$PY" "$REPO_DIR/option_hub.py" token)" || die "could not derive the lock token (ASSISTANT_KEY in .env?)"
[[ "$TOKEN" =~ ^[0-9a-f]{64}$ ]] || die "unexpected token format"

# ---- checkout ---------------------------------------------------------------------------
say "Fetching Option Hub"
if [[ ! -d "$HUB_DIR/.git" ]]; then
  git clone --quiet "$HUB_REPO" "$HUB_DIR"
fi
if [[ -n "$(git -C "$HUB_DIR" status --porcelain --untracked-files=no)" ]]; then
  die "$HUB_DIR has local edits to tracked files — this setup never edits them; stash or discard first"
fi
git -C "$HUB_DIR" fetch --quiet origin
ref="$HUB_REF"; [[ "$ref" == "main" ]] && ref="origin/main"
git -C "$HUB_DIR" checkout --quiet --detach "$ref"
echo "at $(git -C "$HUB_DIR" log -1 --format='%h %ci %s')"

# ---- build ------------------------------------------------------------------------------
say "Installing dependencies + building (a minute or two)"
(
  cd "$HUB_DIR"
  npm ci --no-audit --no-fund --loglevel=error
  VITE_PROXY_URL="https://$HUB_HOST" NODE_OPTIONS="--max-old-space-size=1536" nice -n 10 npm run build --silent
  npm prune --omit=dev --no-audit --no-fund --loglevel=error
)
[[ -f "$HUB_DIR/dist/index.html" ]] || die "build produced no dist/index.html"

# ---- systemd user unit ------------------------------------------------------------------
say "Installing user service option-hub"
mkdir -p "$HOME/.config/systemd/user"
cat > "$HOME/.config/systemd/user/option-hub.service" <<EOF
[Unit]
Description=Option Hub by Mr. Chartist (owner-only; framed by options-checklist)
After=network-online.target

[Service]
Type=simple
WorkingDirectory=$HUB_DIR
Environment=HUB_DIR=$HUB_DIR
Environment=PORT=4001
Environment=HOST=127.0.0.1
Environment=PROXY_PORT=4002
Environment=PROXY_HOST=127.0.0.1
Environment=PROXY_ALLOWED_ORIGINS=https://$HUB_HOST
Environment=PROXY_ALLOWED_HOSTS=$HUB_HOST
Environment=HUB_FRAME_ANCESTORS=https://$DOMAIN
Environment=NODE_ENV=production
Environment=NODE_OPTIONS=--max-old-space-size=384
ExecStart=$NODE_BIN $REPO_DIR/deploy/option_hub/serve.mjs
Restart=always
RestartSec=5
Nice=5

[Install]
WantedBy=default.target
EOF
systemctl --user daemon-reload
systemctl --user enable --quiet option-hub
systemctl --user restart option-hub

# ---- Caddy ------------------------------------------------------------------------------
say "Caddy site block for $HUB_HOST (sudo)"
block="$(cat <<EOF
$MARK_BEGIN
$HUB_HOST {
	encode gzip
	# Owner-only (licence: personal use; the hub can place real orders). The cookie is set by
	# the dashboard POST /hub/session for holders of ASSISTANT_KEY. route = literal order.
	@locked not header Cookie *hz_hub=$TOKEN*
	@data path /api/* /ws /health
	route {
		respond @locked "Option Hub is private. Open it from the Option Hub tab on https://$DOMAIN" 401
		reverse_proxy @data 127.0.0.1:4002
		reverse_proxy 127.0.0.1:4001
	}
}
$MARK_END
EOF
)"
tmp="$(mktemp)"
sudo cat "$CADDYFILE" | BLOCK="$block" B="$MARK_BEGIN" E="$MARK_END" "$PY" -c '
import os, sys
src = sys.stdin.read(); b, e, blk = os.environ["B"], os.environ["E"], os.environ["BLOCK"]
if b in src and e in src:
    pre, rest = src.split(b, 1); post = rest.split(e, 1)[1]
    out = pre.rstrip("\n") + "\n\n" + blk + post
else:
    out = src.rstrip("\n") + "\n\n" + blk + "\n"
sys.stdout.write(out)' > "$tmp"
backup="$CADDYFILE.bak-optionhub-$(date +%Y%m%d%H%M%S)"
sudo cp "$CADDYFILE" "$backup"
sudo cp "$tmp" "$CADDYFILE"; rm -f "$tmp"
if ! sudo caddy validate --config "$CADDYFILE" --adapter caddyfile >/dev/null 2>&1; then
  sudo cp "$backup" "$CADDYFILE"
  sudo caddy validate --config "$CADDYFILE" --adapter caddyfile || true
  die "the new Caddyfile did not validate — restored $backup"
fi
sudo systemctl reload caddy
echo "Caddyfile updated (backup: $backup)"

# ---- dashboard config -------------------------------------------------------------------
say "OPTION_HUB_URL in $REPO_DIR/.env + restart options-app"
if grep -q '^OPTION_HUB_URL=' "$REPO_DIR/.env"; then
  sed -i "s|^OPTION_HUB_URL=.*|OPTION_HUB_URL=https://$HUB_HOST|" "$REPO_DIR/.env"
else
  printf '\nOPTION_HUB_URL=https://%s\n' "$HUB_HOST" >> "$REPO_DIR/.env"
fi
systemctl --user restart options-app

# ---- smoke test -------------------------------------------------------------------------
say "Smoke test"
fails=0
check() {
  local name="$1" want="$2" got="$3"
  if [[ "$got" == "$want" ]]; then printf '  %-50s OK (%s)\n' "$name" "$got"
  else printf '  %-50s FAIL (got %s, want %s)\n' "$name" "$got" "$want"; fails=$((fails + 1)); fi
}
for _ in $(seq 1 15); do curl -fsS -o /dev/null http://127.0.0.1:4002/health 2>/dev/null && break; sleep 1; done
check "frontend :4001/healthz"            200 "$(curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:4001/healthz)"
check "proxy :4002/health"                200 "$(curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:4002/health)"
nse="$(curl -s -m 40 -o /dev/null -w '%{http_code}' -H "Origin: https://$HUB_HOST" -H "Host: $HUB_HOST" 'http://127.0.0.1:4002/api/nse-proxy?endpoint=option-chain&symbol=NIFTY' || true)"
check "NSE option chain via the hub proxy"  200 "$nse"
for _ in $(seq 1 20); do c="$(curl -s -o /dev/null -w '%{http_code}' "https://$HUB_HOST/" || true)"; [[ "$c" == "000" ]] || break; sleep 3; done
check "https://$HUB_HOST/ without cookie (locked)" 401 "$c"
check "https://$HUB_HOST/ with cookie"            200 "$(curl -s -o /dev/null -w '%{http_code}' --cookie "hz_hub=$TOKEN" "https://$HUB_HOST/")"
check "https://$HUB_HOST/api/x/ban-list with cookie" 200 "$(curl -s -o /dev/null -w '%{http_code}' --cookie "hz_hub=$TOKEN" -H "Origin: https://$HUB_HOST" "https://$HUB_HOST/api/x/ban-list")"
for _ in $(seq 1 15); do curl -fsS -o /dev/null http://127.0.0.1:8000/healthz 2>/dev/null && break; sleep 1; done
check "dashboard /hub/status configured" true "$(curl -s http://127.0.0.1:8000/hub/status | "$PY" -c 'import json,sys; print(str(json.load(sys.stdin).get("configured")).lower())' 2>/dev/null || echo error)"

echo
if (( fails == 0 )); then
  echo "Done. Open https://$DOMAIN → Option Hub tab (same app key as Positions)."
else
  echo "$fails check(s) failed. Logs: journalctl --user -u option-hub -n 80 --no-pager ; sudo journalctl -u caddy -n 50 --no-pager"
  if [[ "$nse" != "200" ]]; then
    echo "NSE failing alone only means the hub shows its labelled SAMPLE data until NSE answers (or you connect a broker in its Broker Settings)."
  fi
fi
