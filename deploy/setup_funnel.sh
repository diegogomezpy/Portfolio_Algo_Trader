#!/usr/bin/env bash
# Expose the dashboard publicly behind a password: Caddy basic-auth proxy + Tailscale Funnel.
# Run ON THE VM. Installs Caddy + Tailscale, prompts for a password, configures the auth proxy.
# The two interactive Tailscale steps (account login + enabling Funnel) are printed at the end.
#
#   bash deploy/setup_funnel.sh
set -euo pipefail
APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

echo "== installing Caddy + Tailscale =="
sudo apt-get install -y debian-keyring debian-archive-keyring apt-transport-https
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' | sudo gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' | sudo tee /etc/apt/sources.list.d/caddy-stable.list >/dev/null
sudo apt-get update && sudo apt-get install -y caddy
command -v tailscale >/dev/null 2>&1 || curl -fsSL https://tailscale.com/install.sh | sh

echo "== configuring the password gate (user: viewer) =="
read -rs -p "Pick a dashboard password: " DPW; echo
HASH="$(caddy hash-password --plaintext "$DPW")"
sed "s|__BCRYPT_HASH__|$HASH|" "$APP_DIR/deploy/Caddyfile" | sudo tee /etc/caddy/Caddyfile >/dev/null
sudo systemctl restart caddy && sleep 1
echo -n "  auth check (expect 401 then 200): "
echo "$(curl -s -o /dev/null -w '%{http_code}' localhost:8080/api/meta) / $(curl -s -o /dev/null -w '%{http_code}' -u "viewer:$DPW" localhost:8080/api/meta)"

cat <<'NEXT'

== Caddy is gating the dashboard on :8080. Two interactive Tailscale steps remain: ==
  1. sudo tailscale up            # opens a browser link; sign into a (free) Tailscale account
  2. sudo tailscale funnel --bg 8080
     # if it says Funnel/HTTPS isn't enabled, open the link it prints + enable HTTPS certs at
     #   https://login.tailscale.com/admin/dns , then re-run.
  3. tailscale funnel status      # prints your public https://<host>.<tailnet>.ts.net URL
Then in the Tailscale admin, disable key expiry on this machine so the URL never lapses.
NEXT
