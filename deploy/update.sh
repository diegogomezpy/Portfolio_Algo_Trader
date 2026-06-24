#!/usr/bin/env bash
# Safe in-place update. Pulls main, installs deps, runs the OFFLINE unit suite, and only
# restarts the services if that passes — otherwise it rolls back to the previous commit and
# leaves the running services untouched. A bad push therefore cannot brick live trading.
#
#   bash deploy/update.sh
#
# Rollback is automatic on failure; to roll back manually later:
#   git reset --hard <prev-sha> && ./.venv/bin/pip install -q -r requirements.txt \
#     && sudo systemctl restart sharpe-eod sharpe-dashboard
set -uo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$APP_DIR"
PY=./.venv/bin/python
PREV="$(git rev-parse HEAD)"
echo "== updating from $PREV =="

rollback() {
  echo "!! $1 — rolling back to $PREV (services NOT changed yet unless noted)"
  git reset --hard "$PREV"
  $PY -m pip install -q -r requirements.txt || true
  exit 1
}

# Fresh dump first so there's a known-good restore point (best-effort).
if [ -n "${SHARPE_BACKUP_BUCKET:-}" ]; then bash deploy/backup.sh || echo "(pre-update backup failed, continuing)"; fi

git fetch -q origin || rollback "git fetch failed"
git pull -q --ff-only origin main || rollback "git pull --ff-only failed (diverged? resolve manually)"
$PY -m pip install -q -r requirements.txt || rollback "pip install failed"

echo "== smoke test: offline unit suite =="
$PY -m pytest tests/unit -q || rollback "unit tests failed on the new code"

# New TABLES only (create_all is additive). NOTE: new COLUMNS need a manual ALTER — see the
# last_equity example in DEPLOY.md; column migrations are not auto-applied.
$PY scripts/init_db.py --env paper || rollback "init_db failed"

echo "== green — restarting services =="
sudo systemctl restart sharpe-eod sharpe-dashboard
sleep 6
if systemctl is-active --quiet sharpe-eod && systemctl is-active --quiet sharpe-dashboard \
   && curl -fsS --max-time 5 http://localhost:8000/api/state >/dev/null 2>&1; then
  echo "== UPDATE OK: now at $(git rev-parse --short HEAD) =="
else
  echo "!! services unhealthy after restart — rolling back + restarting"
  git reset --hard "$PREV"; $PY -m pip install -q -r requirements.txt
  sudo systemctl restart sharpe-eod sharpe-dashboard
  exit 1
fi
