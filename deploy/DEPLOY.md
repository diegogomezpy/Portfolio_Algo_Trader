# Deploying sharpe-engine to Google Cloud (always-on paper trading)

A single **Compute Engine VM** runs the engine and dashboard as `systemd` services with
`Restart=always`, so it survives crashes, reboots, and GCP maintenance — independent of
your laptop's power/network. This is a near-lift-and-shift: `run_eod --serve` is already
built for a long-running host (APScheduler + SIGTERM handling).

```
┌─────────────────────────  GCE VM  (e2-small, Ubuntu 24.04, us-east4)  ─────────────────────────┐
│  systemd (Restart=always):                                                                      │
│    • sharpe-eod.service        run_eod.py --serve   (16:10-ET rebalance branch + 60s monitor)   │
│    • sharpe-dashboard.service  run_dashboard.py     (FastAPI on 127.0.0.1:8000, localhost only)  │
│    • sharpe-backup.timer       nightly pg_dump → GCS                                             │
│  Postgres (local, peer auth)   +   data/ Parquet store   +   .venv                              │
└─────────────────────────────────────────────────────────────────────────────────────────────┘
        ▲ secrets via Secret Manager (attached SA)        ▲ dashboard via SSH tunnel (never public)
```

Everything below is copy-pasteable. Run **Steps 0–6** from your **laptop**; **Steps 7–9** on the **VM**.

---

## Step 0 — Variables (laptop)

```bash
export PROJECT="$(gcloud config get-value project)"   # or set explicitly: export PROJECT=my-proj
export REGION=us-east4                                 # close to NYSE/Alpaca (low latency)
export ZONE=us-east4-c
export VM=sharpe-engine
export SA_NAME=sharpe-vm
export SA_EMAIL="$SA_NAME@$PROJECT.iam.gserviceaccount.com"
export BUCKET="$PROJECT-sharpe-backups"               # globally-unique GCS bucket name
gcloud config set project "$PROJECT"
```

## Step 1 — Enable APIs (laptop)

```bash
gcloud services enable compute.googleapis.com secretmanager.googleapis.com storage.googleapis.com
```

## Step 2 — Put secrets in Secret Manager (laptop)

Reads the three real secrets straight from your local `.env.paper` (so keys never land in
shell history). Run from the repo root:

```bash
cd "/Users/diegogomezpy/Software Projects/Portfolio_Algo_Trader"   # repo root with .env.paper
for pair in alpaca-api-key:ALPACA_API_KEY alpaca-secret-key:ALPACA_SECRET_KEY smtp-password:SMTP_PASSWORD; do
  name="${pair%%:*}"; var="${pair##*:}"
  val="$(grep -E "^${var}=" .env.paper | cut -d= -f2-)"
  gcloud secrets create "$name" --replication-policy=automatic 2>/dev/null || true
  printf %s "$val" | gcloud secrets versions add "$name" --data-file=-
  echo "  set secret $name"
done
```

(`DATABASE_URL` is **not** a secret here — the VM's Postgres uses local peer auth over the
Unix socket, so `fetch_secrets.sh` writes `DATABASE_URL=postgresql:///sharpe_engine` directly.)

## Step 3 — Backup bucket + 30-day lifecycle (laptop)

```bash
gcloud storage buckets create "gs://$BUCKET" --location="$REGION" --uniform-bucket-level-access
printf '{"rule":[{"action":{"type":"Delete"},"condition":{"age":30}}]}' > /tmp/lc.json
gcloud storage buckets update "gs://$BUCKET" --lifecycle-file=/tmp/lc.json
```

## Step 4 — Service account for the VM (laptop)

Least privilege: read secrets, write backups. Nothing else.

```bash
gcloud iam service-accounts create "$SA_NAME" --display-name="sharpe-engine VM"
gcloud projects add-iam-policy-binding "$PROJECT" \
  --member="serviceAccount:$SA_EMAIL" --role="roles/secretmanager.secretAccessor"
gcloud storage buckets add-iam-policy-binding "gs://$BUCKET" \
  --member="serviceAccount:$SA_EMAIL" --role="roles/storage.objectAdmin"
```

## Step 5 — Create the VM (laptop)

```bash
gcloud compute instances create "$VM" \
  --zone="$ZONE" --machine-type=e2-small \
  --image-family=ubuntu-2404-lts-amd64 --image-project=ubuntu-os-cloud \
  --boot-disk-size=30GB --boot-disk-type=pd-balanced \
  --service-account="$SA_EMAIL" --scopes=cloud-platform
```

> Do **not** add a firewall rule for port 8000 — the dashboard binds to localhost and you
> reach it through an SSH tunnel (Step 9). SSH (tcp:22) is already allowed on the default network.

## Step 6 — Get the code + data onto the VM (laptop)

**6a. Clone the repo on the VM.** If the GitHub repo is **private**, add a deploy key first:

```bash
gcloud compute ssh "$VM" --zone="$ZONE" --command='ssh-keygen -t ed25519 -N "" -f ~/.ssh/id_ed25519 <<<y >/dev/null; cat ~/.ssh/id_ed25519.pub'
# → copy that public key into GitHub: repo → Settings → Deploy keys → Add (read-only is fine)
gcloud compute ssh "$VM" --zone="$ZONE" --command='ssh-keyscan github.com >> ~/.ssh/known_hosts 2>/dev/null; git clone git@github.com:diegogomezpy/Portfolio_Algo_Trader.git'
```

(Public repo? Skip the key: `git clone https://github.com/diegogomezpy/Portfolio_Algo_Trader.git`.)

**6b. Copy the data store** (git-ignored, ~560 MB) from your laptop to the VM:

```bash
cd "/Users/diegogomezpy/Software Projects/Portfolio_Algo_Trader"
gcloud compute scp --recurse --zone="$ZONE" ./data "$VM:~/Portfolio_Algo_Trader/"
```

## Step 7 — Bootstrap the VM (on the VM)

```bash
gcloud compute ssh "$VM" --zone="$ZONE"     # ← now you're on the VM
# --- on the VM: ---
cd ~/Portfolio_Algo_Trader
chmod +x deploy/*.sh
bash deploy/bootstrap.sh                     # apt deps, Postgres, venv, secrets→.env.paper, init_db
```

`bootstrap.sh` is idempotent. It installs Python 3.12 + Postgres, creates the `sharpe_engine`
DB (peer auth), builds `.venv`, pulls the secrets into `.env.paper` (mode 600), and creates the
schema. It auto-installs the gcloud CLI if the image lacks it.

## Step 8 — Install + start the services (on the VM)

```bash
bash deploy/install_services.sh "$PROJECT-sharpe-backups"   # your bucket name
```

This renders the unit files for this host (user/dir/bucket), enables and starts
`sharpe-eod`, `sharpe-dashboard`, and the nightly `sharpe-backup` timer.

## Step 9 — Verify (on the VM, then laptop)

```bash
# on the VM:
systemctl status sharpe-eod sharpe-dashboard --no-pager
journalctl -u sharpe-eod -n 40 --no-pager        # should show "scheduler started ... EOD 16:10 ET"
curl -s localhost:8000/api/state | head -c 300   # dashboard answering
systemctl list-timers sharpe-backup --no-pager
```

**View the dashboard** from your laptop via an SSH tunnel (no public exposure):

```bash
gcloud compute ssh "$VM" --zone="$ZONE" -- -N -L 8000:localhost:8000
# then open http://localhost:8000  (leave that terminal running; Ctrl-C closes the tunnel)
```

---

## Ongoing operations

| Task | Command (on the VM, in `~/Portfolio_Algo_Trader`) |
|---|---|
| **Deploy code update** | `git pull && ./.venv/bin/pip install -r requirements.txt && bash deploy/install_services.sh "$PROJECT-sharpe-backups"` |
| **Rotate a secret** | update it in Secret Manager, then `bash deploy/fetch_secrets.sh && sudo systemctl restart sharpe-eod sharpe-dashboard` |
| **Tail logs** | `journalctl -u sharpe-eod -f`  ·  `tail -f logs/sharpe-engine.log` |
| **Force a rebalance now** | `sudo systemctl stop sharpe-eod && ./.venv/bin/python scripts/run_eod.py --once --force --env paper && sudo systemctl start sharpe-eod` |
| **Manual backup** | `SHARPE_BACKUP_BUCKET="$PROJECT-sharpe-backups" bash deploy/backup.sh` |
| **Restore a backup** | `gcloud storage cp gs://$BUCKET/pg/<file>.sql.gz - | gunzip | psql sharpe_engine` |
| **Stop everything** | `sudo systemctl stop sharpe-eod sharpe-dashboard` (SIGTERM cancels open orders first) |

**Cadence reminder:** `--serve` only *rebalances* on the **first trading day of the month**; on
other trading days it just reconciles + monitors (+ the covered-call safety pass). To trade
sooner, use the "force a rebalance now" row.

**Cost:** e2-small ≈ \$13–15/mo + ~30 GB pd-balanced ≈ \$1.2/mo + minimal egress/storage. Stop
the VM (`gcloud compute instances stop $VM`) to pause billing (engine stops too).

**Migrating to live (Phase 7) later:** create separate `*-live` secrets, point a second
`.env.live` at them, and run with `--env live` — the Alpaca client is hardcoded paper until then.
Also revisit the 2× leverage and add Cloud SQL if you want managed/HA Postgres.
