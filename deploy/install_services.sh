#!/usr/bin/env bash
# Render the systemd units for this host (substitute user/dir/bucket), install them,
# and enable+start the EOD scheduler, dashboard, and nightly backup timer.
#
#   bash deploy/install_services.sh <backup-bucket-name>
#
# Re-run after pulling code changes to pick up unit edits (it re-renders + restarts).
set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
APP_USER="$(whoami)"
BACKUP_BUCKET="${1:?usage: install_services.sh <backup-bucket-name>}"
SRC="$APP_DIR/deploy/systemd"
DST="/etc/systemd/system"

render() {  # render <unit> by substituting placeholders, write to /etc/systemd/system
  sed -e "s|__APP_USER__|$APP_USER|g" \
      -e "s|__APP_DIR__|$APP_DIR|g" \
      -e "s|__BACKUP_BUCKET__|$BACKUP_BUCKET|g" \
      "$SRC/$1" | sudo tee "$DST/$1" >/dev/null
  echo "installed $DST/$1"
}

for u in sharpe-eod.service sharpe-dashboard.service sharpe-backup.service sharpe-backup.timer \
         sharpe-watchdog.service sharpe-watchdog.timer; do
  render "$u"
done

sudo systemctl daemon-reload
sudo systemctl enable --now sharpe-eod.service sharpe-dashboard.service \
     sharpe-backup.timer sharpe-watchdog.timer
echo "== services up. Check: systemctl status sharpe-eod sharpe-dashboard;"
echo "   systemctl list-timers sharpe-backup sharpe-watchdog =="
