#!/usr/bin/env bash
# One-shot deploy for the Options Checklist on a fresh Ubuntu 24.04 VPS.
# Run as root from inside /opt/options after the source files are in place.
set -euo pipefail

cd "$(dirname "$(readlink -f "$0")")"

if [[ "$EUID" -ne 0 ]]; then
  echo "Run as root (use sudo)." >&2
  exit 1
fi

INSTALL_DIR="$(pwd)"
echo "==> Deploying from $INSTALL_DIR"

# 1. apt packages
echo "==> apt update + install python, caddy"
export DEBIAN_FRONTEND=noninteractive
apt-get update -y -qq
apt-get install -y -qq python3 python3-venv python3-pip curl debian-keyring debian-archive-keyring apt-transport-https cron

if ! command -v caddy >/dev/null 2>&1; then
  echo "==> Installing Caddy"
  curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' | gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
  curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' > /etc/apt/sources.list.d/caddy-stable.list
  apt-get update -y -qq
  apt-get install -y -qq caddy
fi

# 2. venv + python deps
echo "==> Setting up Python venv"
if [[ ! -d "$INSTALL_DIR/venv" ]]; then
  python3 -m venv "$INSTALL_DIR/venv"
fi
"$INSTALL_DIR/venv/bin/pip" install --upgrade pip --quiet
"$INSTALL_DIR/venv/bin/pip" install -r "$INSTALL_DIR/requirements.txt" --quiet

# 3. .env sanity
if [[ ! -f "$INSTALL_DIR/.env" ]]; then
  echo "ERROR: $INSTALL_DIR/.env missing — create it with KITE_API_KEY and KITE_API_SECRET" >&2
  exit 2
fi
chmod 600 "$INSTALL_DIR/.env"

# 4. Caddy reverse proxy on :80 (no HTTPS — raw IP setup)
echo "==> Writing /etc/caddy/Caddyfile"
cat > /etc/caddy/Caddyfile <<'EOF'
{
    auto_https off
}

:80 {
    encode gzip
    reverse_proxy 127.0.0.1:8000
}
EOF

# 5. systemd unit for the FastAPI app
echo "==> Writing /etc/systemd/system/options-app.service"
cat > /etc/systemd/system/options-app.service <<EOF
[Unit]
Description=Options Checklist FastAPI app
After=network.target

[Service]
Type=simple
WorkingDirectory=$INSTALL_DIR
EnvironmentFile=$INSTALL_DIR/.env
Environment=OPTIONS_HEADLESS=1
ExecStart=$INSTALL_DIR/venv/bin/uvicorn app:app --host 127.0.0.1 --port 8000
Restart=always
RestartSec=5
User=root
Group=root

[Install]
WantedBy=multi-user.target
EOF

# 6. cron for daily 3:15 PM IST = 9:45 UTC (Mon-Fri)
echo "==> Installing cron"
CRON_LINE="45 9 * * 1-5 cd $INSTALL_DIR && OPTIONS_HEADLESS=1 $INSTALL_DIR/venv/bin/python3 compute.py >> /var/log/options-cron.log 2>&1"
TMPCRON="$(mktemp)"
crontab -l 2>/dev/null | grep -v 'OptionsStrats\|options/compute.py' > "$TMPCRON" || true
echo "$CRON_LINE" >> "$TMPCRON"
crontab "$TMPCRON"
rm -f "$TMPCRON"
touch /var/log/options-cron.log

# 7. enable + start everything
echo "==> Reloading systemd"
systemctl daemon-reload
systemctl enable options-app caddy >/dev/null
systemctl restart options-app
systemctl restart caddy

# 8. brief smoke test
sleep 2
echo
echo "==> Smoke test"
if curl -fsS http://127.0.0.1:8000/healthz >/dev/null; then
  echo "  app on :8000 OK"
else
  echo "  WARNING: app on :8000 not responding"
  systemctl status options-app --no-pager -l | head -20
fi
if curl -fsS http://127.0.0.1/healthz >/dev/null; then
  echo "  caddy on :80 OK"
else
  echo "  WARNING: caddy on :80 not responding"
fi

PUBLIC_IP="$(curl -fsS https://api.ipify.org 2>/dev/null || hostname -I | awk '{print $1}')"
echo
echo "==================================================="
echo "  Deploy complete."
echo
echo "  Next steps:"
echo "    1. Update Kite Connect app redirect URL to:"
echo "         http://$PUBLIC_IP/callback"
echo "       (https://developers.kite.trade/apps -> your app -> edit)"
echo "    2. Visit  http://$PUBLIC_IP/login  in any browser"
echo "    3. Login, authorize, redirect captures token."
echo "    4. Trigger first data fetch:"
echo "         curl -X POST http://$PUBLIC_IP/refresh"
echo "    5. Open  http://$PUBLIC_IP/  on phone or laptop."
echo
echo "  Cron will refresh data automatically at 09:45 UTC (15:15 IST) Mon-Fri."
echo "==================================================="
