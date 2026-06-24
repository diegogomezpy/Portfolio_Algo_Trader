#!/usr/bin/env bash
# One-time setup on a fresh Ubuntu 24.04 LTS GCE VM. Run from the repo root as your
# login user (the one `gcloud compute ssh` created), AFTER the repo + data/ are present:
#
#   cd ~/Portfolio_Algo_Trader && bash deploy/bootstrap.sh
#
# Idempotent: safe to re-run. Ubuntu 24.04 ships Python 3.12 (the pinned version).
set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
APP_USER="$(whoami)"
cd "$APP_DIR"
echo "== bootstrap sharpe-engine  (dir=$APP_DIR user=$APP_USER) =="

echo "== apt deps =="
sudo apt-get update -y
sudo apt-get install -y python3.12 python3.12-venv python3-pip postgresql postgresql-contrib git
# gcloud CLI (for Secret Manager + GCS) if not already present on the image.
if ! command -v gcloud >/dev/null 2>&1; then
  echo "== installing google-cloud-cli =="
  sudo apt-get install -y apt-transport-https ca-certificates gnupg curl
  curl -fsSL https://packages.cloud.google.com/apt/doc/apt-key.gpg | sudo gpg --dearmor -o /usr/share/keyrings/cloud.google.gpg
  echo "deb [signed-by=/usr/share/keyrings/cloud.google.gpg] https://packages.cloud.google.com/apt cloud-sdk main" \
    | sudo tee /etc/apt/sources.list.d/google-cloud-sdk.list >/dev/null
  sudo apt-get update -y && sudo apt-get install -y google-cloud-cli
fi

echo "== Postgres role + db (peer auth over the local socket; no password) =="
sudo -u postgres psql -tAc "SELECT 1 FROM pg_roles WHERE rolname='$APP_USER'" | grep -q 1 \
  || sudo -u postgres createuser "$APP_USER"
sudo -u postgres psql -tAc "SELECT 1 FROM pg_database WHERE datname='sharpe_engine'" | grep -q 1 \
  || sudo -u postgres createdb -O "$APP_USER" sharpe_engine

echo "== Python venv + deps =="
[ -d .venv ] || python3.12 -m venv .venv
./.venv/bin/pip install --upgrade pip
./.venv/bin/pip install -r requirements.txt

echo "== secrets from Secret Manager -> .env.paper =="
bash deploy/fetch_secrets.sh

echo "== create Postgres schema =="
./.venv/bin/python scripts/init_db.py --env paper

echo "== bootstrap complete. Next: bash deploy/install_services.sh <backup-bucket> =="
