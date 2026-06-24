#!/usr/bin/env bash
# Health watchdog — runs as root every 5 min (sharpe-watchdog.timer). systemd's
# Restart=always handles plain crashes; this is the second line of defense for:
#   - a unit that ended up inactive/failed and stopped retrying
#   - a dashboard process that is alive but no longer answering (wedged)
# On any intervention it restarts the unit AND emails via deploy/notify.py.
set -uo pipefail

APP_DIR="${APP_DIR:?APP_DIR not set}"
APP_USER="${APP_USER:?APP_USER not set}"

alert() {  # send as the app user so .env.paper (mode 600) is readable
  sudo -u "$APP_USER" "$APP_DIR/.venv/bin/python" "$APP_DIR/deploy/notify.py" "$1" || true
}

for svc in sharpe-eod sharpe-dashboard; do
  if ! systemctl is-active --quiet "$svc"; then
    echo "watchdog: $svc inactive -> restart"
    systemctl restart "$svc" && alert "sharpe-engine: watchdog restarted $svc (was inactive)"
  fi
done

# Dashboard liveness: process up but not serving -> restart it.
if ! curl -fsS --max-time 5 http://localhost:8000/api/state >/dev/null 2>&1; then
  echo "watchdog: dashboard not answering -> restart"
  systemctl restart sharpe-dashboard && alert "sharpe-engine: watchdog restarted dashboard (not answering)"
fi
