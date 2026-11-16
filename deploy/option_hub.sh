#!/usr/bin/env bash
# ONE-TIME setup of the Option Hub tab (Mr. Chartist's options terminal, owner-only).
#
#   bash deploy/option_hub.sh
#
# After this, there is nothing separate to maintain: the dashboard app itself installs, builds
# and runs the hub (option_hub.py RUNTIME), so the usual deploy covers both:
#   git pull && systemctl --user restart options-app
# The hub's source is optionhub/ in this repo; the app rebuilds it whenever that folder changes.
#
# This script only does what the app cannot do for itself (sudo):
#   1. a Caddy site block for hub.<domain> — the hub must sit at the root of its own host (his
#      React router has no base path). 401 without the owner cookie, else routed to 4001/4002.
#   2. an optional 2 GB swap file if the server lacks RAM headroom for the build (~1.2 GB peak).
# plus: OPTION_HUB_URL in .env, removes the old separate `option-hub` service if an earlier
# version of this script installed it, restarts options-app, then follows the app's first build
# and smoke-tests the lock, the NSE path and the Kite feed.
#
# Re-run after changing ASSISTANT_KEY (the Caddy lock cookie is derived from it).
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CADDYFILE="${CADDYFILE:-/etc/caddy/Caddyfile}"
MARK_BEGIN="# >>> option-hub (managed by options-checklist/deploy/option_hub.sh — edits here are overwritten)"
MARK_END="# <<< option-hub"
PY="$REPO_DIR/venv/bin/python3"; [[ -x "$PY" ]] || PY="python3"
APP="http://127.0.0.1:8000"
DATA_DIR="${OPTIONS_DATA_DIR:-$REPO_DIR/data}"

say()  { printf '\n==> %s\n' "$*"; }
die()  { printf '\nERROR: %s\n' "$*" >&2; exit 1; }
ask()  { local a; read -r -p "$1 [y/N] " a; [[ "$a" =~ ^[Yy] ]]; }

[[ "$EUID" -ne 0 ]] || die "run as your regular user, not root"
command -v git >/dev/null || die "git is required: sudo apt-get install -y git"

# ---- domain --------------------------------------------------------------------------
if [[ -z "${DOMAIN:-}" && -r "$CADDYFILE" ]]; then
  DOMAIN="$(grep -E '^[A-Za-z0-9.-]+\.[A-Za-z]+[[:space:]]*\{' "$CADDYFILE" | grep -v '^hub\.' | head -1 | awk '{print $1}' || true)"
fi
[[ -n "${DOMAIN:-}" ]] || read -r -p "Dashboard domain (e.g. sagy-options.duckdns.org): " DOMAIN
[[ -n "${DOMAIN:-}" ]] || die "DOMAIN is required"
HUB_HOST="hub.$DOMAIN"
say "Option Hub → https://$HUB_HOST, run by options-app (framed by https://$DOMAIN)"

# ---- lock token -------------------------------------------------------------------------
TOKEN="$("$PY" "$REPO_DIR/option_hub.py" token)" || die "could not derive the lock token (ASSISTANT_KEY in .env?)"
[[ "$TOKEN" =~ ^[0-9a-f]{64}$ ]] || die "unexpected token format"

# ---- the old separate service (earlier version of this script) ---------------------------
if [[ -f "$HOME/.config/systemd/user/option-hub.service" ]]; then
  say "Removing the old separate option-hub service (the dashboard app runs the hub now)"
  systemctl --user disable --now option-hub 2>/dev/null || true
  rm -f "$HOME/.config/systemd/user/option-hub.service"
  systemctl --user daemon-reload
  [[ -d "$HOME/option-hub" ]] && echo "The old checkout $HOME/option-hub is no longer used — delete it whenever you like."
fi

# ---- memory headroom for the build ------------------------------------------------------
avail=$(awk '/MemAvailable/ {print int($2/1024)}' /proc/meminfo)
swapf=$(awk '/SwapFree/ {print int($2/1024)}' /proc/meminfo)
if (( avail + swapf < 1600 )); then
  echo "Memory: ${avail} MB available + ${swapf} MB swap free; the hub's build peaks at ~1200 MB."
  echo "A 2 GB swap file gives it room (the app will not build without it):"
  echo "  sudo fallocate -l 2G /swapfile && sudo chmod 600 /swapfile && sudo mkswap /swapfile && sudo swapon /swapfile"
  echo "  echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab"
  if ask "Create it now?"; then
    [[ -e /swapfile ]] && die "/swapfile already exists — enable it with: sudo swapon /swapfile"
    sudo fallocate -l 2G /swapfile && sudo chmod 600 /swapfile && sudo mkswap /swapfile >/dev/null && sudo swapon /swapfile
    grep -q '^/swapfile ' /etc/fstab || echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab >/dev/null
  fi
fi

# ---- Caddy ------------------------------------------------------------------------------
say "Caddy site block for $HUB_HOST (sudo)"
block="$(cat <<EOF
$MARK_BEGIN
$HUB_HOST {
	encode gzip
	# Owner-only (licence: personal use; the hub can place real orders). The cookie is set by
	# the dashboard POST /hub/session for holders of ASSISTANT_KEY. route = literal order.
	@locked not header Cookie *hz_hub=$TOKEN*
	@ours path /api/_options-checklist/* /api/broker-proxy
	@data path /api/* /ws /health
	route {
		respond @locked "Option Hub is private. Open it from the Option Hub tab on https://$DOMAIN" 401
		reverse_proxy @ours 127.0.0.1:4001
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
if sudo cmp -s "$tmp" "$CADDYFILE"; then
  echo "Caddyfile already up to date"
  rm -f "$tmp"
else
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
fi

# ---- dashboard config + restart ---------------------------------------------------------
say "OPTION_HUB_URL in $REPO_DIR/.env + restart options-app"
if grep -q '^OPTION_HUB_URL=' "$REPO_DIR/.env"; then
  sed -i "s|^OPTION_HUB_URL=.*|OPTION_HUB_URL=https://$HUB_HOST|" "$REPO_DIR/.env"
else
  printf '\nOPTION_HUB_URL=https://%s\n' "$HUB_HOST" >> "$REPO_DIR/.env"
fi
systemctl --user restart options-app

# ---- follow the app while it installs + builds the hub ----------------------------------
say "The app is installing Option Hub (first time: download + build, a few minutes)"
for _ in $(seq 1 30); do curl -fsS -o /dev/null "$APP/healthz" 2>/dev/null && break; sleep 1; done
rt_field() { curl -s "$APP/hub/status" | "$PY" -c "import json,sys; r=json.load(sys.stdin).get('runtime') or {}; print(r.get('$1') or '')" 2>/dev/null || true; }
last=""; state=""
for _ in $(seq 1 240); do     # up to 20 min
  state="$(rt_field state)"; detail="$(rt_field detail)"
  line="$state${detail:+ — $detail}"
  [[ "$line" != "$last" ]] && { echo "  $(date +%H:%M:%S)  $line"; last="$line"; }
  case "$state" in
    running) break ;;
    waiting|low_memory|failed|not_configured|other_worker|external) break ;;
  esac
  sleep 5
done
case "$state" in
  running) ;;
  waiting)
    echo
    echo "It is market hours: the app builds the hub after 15:45 IST on its own (or press 'Build now'"
    echo "in the Option Hub tab). Re-run this script afterwards to run the smoke tests."
    exit 0 ;;
  *)
    echo
    echo "The hub is not running ($state). Last lines of its log:"
    curl -s "$APP/hub/status" | "$PY" -c "import json,sys; [print('  '+l) for l in (json.load(sys.stdin).get('runtime') or {}).get('log_tail') or []]" 2>/dev/null || true
    echo "Full logs: $DATA_DIR/option-hub/build.log and serve.log"
    exit 1 ;;
esac

# ---- smoke test -------------------------------------------------------------------------
say "Smoke test"
fails=0
check() {
  local name="$1" want="$2" got="$3"
  if [[ "$got" == "$want" ]]; then printf '  %-50s OK (%s)\n' "$name" "$got"
  else printf '  %-50s FAIL (got %s, want %s)\n' "$name" "$got" "$want"; fails=$((fails + 1)); fi
}
for _ in $(seq 1 20); do curl -fsS -o /dev/null http://127.0.0.1:4002/health 2>/dev/null && break; sleep 1; done
check "frontend :4001/healthz"            200 "$(curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:4001/healthz)"
check "proxy :4002/health"                200 "$(curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:4002/health)"
nse_body="$(mktemp)"
nse="$(curl -s -m 40 -o "$nse_body" -w '%{http_code}' -H "Origin: https://$HUB_HOST" -H "Host: $HUB_HOST" 'http://127.0.0.1:4002/api/nse-proxy?endpoint=option-chain&symbol=NIFTY' || true)"
check "NSE option chain (hub proxy → app → NSE)"  200 "$nse"
[[ "$nse" == "200" ]] || printf '      NSE said: %s\n' "$(head -c 300 "$nse_body" | tr -d '\n')"
rm -f "$nse_body"
for _ in $(seq 1 20); do c="$(curl -s -o /dev/null -w '%{http_code}' "https://$HUB_HOST/" || true)"; [[ "$c" == "000" ]] || break; sleep 3; done
check "https://$HUB_HOST/ without cookie (locked)" 401 "$c"
check "https://$HUB_HOST/ with cookie"            200 "$(curl -s -o /dev/null -w '%{http_code}' --cookie "hz_hub=$TOKEN" "https://$HUB_HOST/")"
check "https://$HUB_HOST/api/x/ban-list with cookie" 200 "$(curl -s -o /dev/null -w '%{http_code}' --cookie "hz_hub=$TOKEN" -H "Origin: https://$HUB_HOST" "https://$HUB_HOST/api/x/ban-list")"
feed="$(curl -s -o /dev/null -w '%{http_code}' -H "X-Hub-Feed: $TOKEN" "$APP/hub/feed/profile" || true)"
case "$feed" in
  200) printf '  %-50s OK (dashboard logged in to Kite: the chain uses Kite in market hours)\n' "Kite feed for the hub" ;;
  403) printf '  %-50s OK (dashboard not logged in to Kite today: the hub uses NSE until you log in)\n' "Kite feed for the hub" ;;
  *)   printf '  %-50s FAIL (HTTP %s)\n' "Kite feed for the hub" "$feed"; fails=$((fails + 1)) ;;
esac

echo
if (( fails == 0 )); then
  echo "Done. Open https://$DOMAIN → Option Hub tab (same app key as Positions)."
  echo "From now on the normal deploy updates it: git pull && systemctl --user restart options-app"
else
  echo "$fails check(s) failed. Logs: $DATA_DIR/option-hub/serve.log ; sudo journalctl -u caddy -n 50 --no-pager"
  if [[ "$nse" != "200" ]]; then
    echo "NSE failing alone only means the hub shows its labelled SAMPLE data until NSE answers."
  fi
fi
