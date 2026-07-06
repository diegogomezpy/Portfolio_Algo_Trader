#!/usr/bin/env bash
# Safe in-place update. Pulls main, installs deps, runs the OFFLINE unit suite, and only
# restarts the services if that passes — otherwise it rolls back to the previous commit and
# leaves the running services untouched. A bad push therefore cannot brick live trading.
#
#   bash deploy/update.sh                  # full update (eod + dashboard restart)
#   bash deploy/update.sh --dashboard-only  # dashboard-only change: never touches sharpe-eod
#
# SAFETY: restarting sharpe-eod cancels ALL open orders (its SIGTERM contract). The full
# update refuses to proceed while orders are working unless FORCE_EOD_RESTART=1 is set.
#
# Rollback is automatic on failure; to roll back manually later:
#   git reset --hard <prev-sha> && ./.venv/bin/pip install -q -r requirements.txt \
#     && sudo systemctl restart sharpe-eod sharpe-dashboard
set -uo pipefail

DASH_ONLY=0
[ "${1:-}" = "--dashboard-only" ] && DASH_ONLY=1

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

if [ "$DASH_ONLY" = "1" ]; then
  echo "== green — restarting sharpe-dashboard ONLY (sharpe-eod untouched, no orders cancelled) =="
  sudo systemctl restart sharpe-dashboard
else
  # Guard: an eod restart cancels ALL open orders. Refuse mid-trade unless forced.
  OPEN=$($PY - <<'PYEOF'
import sys
from engine import config
try:
    config.load_env("paper")
    orders = config.get_alpaca_client().get_orders(status="open", limit=50)
    print(len(orders))
    for o in orders:  # detail to stderr so the operator sees WHAT would be cancelled
        coid = str(getattr(o, "client_order_id", "") or "")
        tag = " [console]" if coid.startswith("manual-") else ""
        print(f"   . {getattr(o, 'symbol', '?')} {getattr(o, 'side', '?')} "
              f"{getattr(o, 'qty', '?')} {getattr(o, 'order_type', '?')} "
              f"({getattr(o, 'status', '?')}) coid={coid}{tag}", file=sys.stderr)
except Exception:
    print("unknown")
PYEOF
)
  if [ "$OPEN" != "0" ] && [ "${FORCE_EOD_RESTART:-0}" != "1" ]; then
    echo "!! $OPEN open order(s) working (or Alpaca unreadable) — an eod restart would CANCEL them."
    echo "   Re-run with FORCE_EOD_RESTART=1 to override, or use --dashboard-only. Code is updated;"
    echo "   services were NOT restarted (old code still running)."
    exit 1
  fi
  echo "== green — restarting services =="
  sudo systemctl restart sharpe-eod sharpe-dashboard
fi
sleep 6
if systemctl is-active --quiet sharpe-dashboard \
   && { [ "$DASH_ONLY" = "1" ] || systemctl is-active --quiet sharpe-eod; } \
   && curl -fsS --max-time 5 http://localhost:8000/api/state >/dev/null 2>&1; then
  echo "== UPDATE OK: now at $(git rev-parse --short HEAD) =="
else
  echo "!! services unhealthy after restart — rolling back + restarting"
  git reset --hard "$PREV"; $PY -m pip install -q -r requirements.txt
  sudo systemctl restart sharpe-eod sharpe-dashboard
  exit 1
fi
