#!/usr/bin/env bash
# One-shot deploy for the Options Checklist on a fresh Ubuntu 24.04 droplet
# running as a regular user (not root). Uses Caddy for auto-HTTPS via
# Let's Encrypt + a systemd --user unit for the FastAPI app.
#
# Pre-reqs:
#   - This repo cloned to ~/options-checklist (or wherever pwd is when run)
#   - A .env file in the same directory with KITE_API_KEY, KITE_API_SECRET,
#     and REDIS_URL
#   - A DuckDNS subdomain (or any FQDN) already pointing to this droplet's IP
#
# Run:
#   DOMAIN=sagy-options.duckdns.org bash deploy.sh
#
# (sudo will be requested for package install + Caddy + UFW)

set -euo pipefail

INSTALL_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$INSTALL_DIR"

# ---- guards ----
if [[ "$EUID" -eq 0 ]]; then
  echo "ERROR: run as your regular user (e.g. 'sagar'), not root." >&2
  exit 1
fi

if [[ -z "${DOMAIN:-}" ]]; then
  read -r -p "Enter your DuckDNS domain (e.g. sagy-options.duckdns.org): " DOMAIN
fi
if [[ -z "$DOMAIN" ]]; then
  echo "ERROR: DOMAIN is required." >&2
  exit 1
fi

if [[ ! -f "$INSTALL_DIR/.env" ]]; then
  echo "ERROR: $INSTALL_DIR/.env not found." >&2
  echo "       Create it with:" >&2
  echo "         KITE_API_KEY=..." >&2
  echo "         KITE_API_SECRET=..." >&2
  echo "         REDIS_URL=rediss://..." >&2
  exit 1
fi
chmod 600 "$INSTALL_DIR/.env"

echo "==> Deploying $INSTALL_DIR as user $USER, domain $DOMAIN"

# ---- 1. apt + Caddy install ----
echo "==> Installing system packages (sudo)"
sudo apt-get update -qq
sudo apt-get install -y -qq python3-venv python3-pip curl cron debian-keyring debian-archive-keyring apt-transport-https gnupg

if ! command -v caddy >/dev/null 2>&1; then
  echo "==> Installing Caddy"
  curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' \
    | sudo gpg --batch --yes --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
  curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' \
    | sudo tee /etc/apt/sources.list.d/caddy-stable.list >/dev/null
  sudo apt-get update -qq
  sudo apt-get install -y -qq caddy
fi

# ---- 2. UFW (open 80 + 443) ----
if command -v ufw >/dev/null 2>&1; then
  echo "==> Opening UFW ports 80 + 443"
  sudo ufw allow 80/tcp  comment 'Caddy HTTP'  >/dev/null 2>&1 || true
  sudo ufw allow 443/tcp comment 'Caddy HTTPS' >/dev/null 2>&1 || true
fi

# ---- 3. Python venv + deps ----
echo "==> Setting up Python venv"
if [[ ! -d "$INSTALL_DIR/venv" ]]; then
  python3 -m venv "$INSTALL_DIR/venv"
fi
"$INSTALL_DIR/venv/bin/pip" install --upgrade pip --quiet
"$INSTALL_DIR/venv/bin/pip" install -r "$INSTALL_DIR/requirements.txt" --quiet

# ---- 4. Caddy reverse proxy + auto-HTTPS ----
echo "==> Writing /etc/caddy/Caddyfile (HTTPS for $DOMAIN)"
sudo tee /etc/caddy/Caddyfile >/dev/null <<EOF
{
    email admin@$DOMAIN
}

$DOMAIN {
    encode gzip
    reverse_proxy 127.0.0.1:8000
}
EOF
sudo systemctl enable --now caddy >/dev/null 2>&1 || true
sudo systemctl reload caddy

# ---- 5. systemd --user unit for the FastAPI app ----
echo "==> Writing ~/.config/systemd/user/options-app.service"
mkdir -p ~/.config/systemd/user
cat > ~/.config/systemd/user/options-app.service <<EOF
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

[Install]
WantedBy=default.target
EOF

systemctl --user daemon-reload
systemctl --user enable --now options-app

# Linger lets the user service survive logout / start on boot
sudo loginctl enable-linger "$USER" >/dev/null 2>&1 || true

# ---- 6. crontab (daily 09:45 UTC = 15:15 IST, Mon-Fri) ----
echo "==> Installing cron"
CRON_LINE="45 9 * * 1-5 cd $INSTALL_DIR && OPTIONS_HEADLESS=1 $INSTALL_DIR/venv/bin/python3 compute.py >> $HOME/options-cron.log 2>&1"
TMPCRON="$(mktemp)"
crontab -l 2>/dev/null | grep -v 'options-checklist/compute.py\|OptionsStrats' > "$TMPCRON" || true
echo "$CRON_LINE" >> "$TMPCRON"
crontab "$TMPCRON"
rm -f "$TMPCRON"
touch "$HOME/options-cron.log"

# ---- 7. Smoke test ----
echo
echo "==> Smoke test (give Caddy a moment to provision the cert on first run)"
sleep 5

echo -n "  app on :8000  "
if curl -fsS http://127.0.0.1:8000/healthz >/dev/null; then
  echo "OK"
else
  echo "FAIL — checking status..."
  systemctl --user status options-app --no-pager -l | tail -20
fi

echo -n "  https://$DOMAIN  "
if curl -fsS "https://$DOMAIN/healthz" >/dev/null; then
  echo "OK"
else
  echo "PENDING (cert may still be provisioning — retry in 30s)"
fi

echo
echo "================================================================"
echo "  Deploy complete."
echo
echo "  Public URL:  https://$DOMAIN/"
echo
echo "  Next steps:"
echo "    1. Update Kite Connect redirect URL to:"
echo "         https://$DOMAIN/callback"
echo "       (https://developers.kite.trade/apps)"
echo "    2. Visit  https://$DOMAIN/login  and complete the OAuth"
echo "    3. Click Refresh on the dashboard to seed Redis"
echo
echo "  Logs:"
echo "    App:   journalctl --user -u options-app -f"
echo "    Cron:  tail -f $HOME/options-cron.log"
echo "    Caddy: sudo journalctl -u caddy -f"
echo "================================================================"
