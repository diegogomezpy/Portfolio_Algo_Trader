#!/usr/bin/env bash
# Dump the operational Postgres DB and upload to GCS. Invoked by sharpe-backup.timer.
# Runs as the app user (peer auth over the local socket → no DB password needed).
# Keeps the GCS side cheap: lifecycle-delete old dumps via a bucket rule (see DEPLOY.md).
set -euo pipefail

BUCKET="${SHARPE_BACKUP_BUCKET:?set SHARPE_BACKUP_BUCKET (the GCS bucket name, no gs:// prefix)}"
TS="$(date -u +%Y%m%dT%H%M%SZ)"
TMP="$(mktemp /tmp/sharpe_pg_XXXXXX.sql.gz)"
trap 'rm -f "$TMP"' EXIT

pg_dump sharpe_engine | gzip > "$TMP"
gcloud storage cp "$TMP" "gs://$BUCKET/pg/sharpe_engine_${TS}.sql.gz"
echo "backed up sharpe_engine -> gs://$BUCKET/pg/sharpe_engine_${TS}.sql.gz"
