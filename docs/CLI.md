# CLI reference

Every command-line entry point in the repo. These are operational scripts, not a
packaged console app — run them with the project virtualenv's interpreter.

## Conventions

- **Interpreter** — always invoke through the project venv so dependencies
  resolve: `./.venv/bin/python …` (the global Python is intentionally kept clean).
- **`--env {paper,live}`** — selects which secrets file to load. `paper` loads
  `.env.paper`, `live` loads `.env.live` (both git-ignored). Defaults to `paper`
  everywhere. Copy `.env.example` to create them; required keys:

  | Variable | Used by |
  |---|---|
  | `ALPACA_API_KEY`, `ALPACA_SECRET_KEY` | Alpaca price data + trading |
  | `ALPACA_BASE_URL`, `ALPACA_DATA_URL` | Alpaca endpoints |
  | `DATABASE_URL` | PostgreSQL operational store |
  | `SMTP_PASSWORD` | alert email delivery |

- **Trading safety** — the Alpaca client is **paper-only** (hardcoded
  `paper=True`) until Phase 7. `--env live` selects the live *data/DB* config but
  does not place live trades.
- **Data feed** — the default Alpaca `iex` feed (free) gives consolidated history
  from ~mid-2020 and IEX-exchange volume only; full 2016+ history and the
  consolidated tape need the paid SIP feed. See the README "Data-feed note".

### First-time setup order

```bash
# 1. Create the database itself (init_db only makes TABLES, not the database)
createdb sharpe_engine                      # Postgres.app / local Postgres

# 2. Create the operational tables
./.venv/bin/python scripts/init_db.py --env paper

# 3. One-time historical backfill into the Parquet store
./.venv/bin/python scripts/backfill.py --env paper

# 4. Thereafter, the daily incremental ingest
./.venv/bin/python -m engine.ingest --env paper
```

---

## `scripts/init_db.py` — create the database schema

Idempotent migration: creates every operational table defined in
[`engine/db.py`](../engine/db.py) that does not already exist. Reads
`DATABASE_URL` from the chosen env file (or an explicit override). **Creates
tables, not the database** — the target database must already exist.

```bash
./.venv/bin/python scripts/init_db.py --env paper
```

| Flag | Default | Description |
|---|---|---|
| `--env {paper,live}` | `paper` | Which `.env.<env>` to load `DATABASE_URL` from. |
| `--database-url URL` | — | Override `DATABASE_URL` directly (skips loading the env file). |
| `--drop` | off | **Destructive.** `DROP` all tables before recreating. Dev resets only. |

**Examples**

```bash
# Recreate from scratch in dev (drops then creates)
./.venv/bin/python scripts/init_db.py --env paper --drop

# Verify the schema materializes against a throwaway sqlite file (no Postgres)
./.venv/bin/python scripts/init_db.py --database-url sqlite:///./dev.db
```

---

## `scripts/backfill.py` — one-time historical backfill

Populates the Parquet research store: full-universe daily OHLCV from Alpaca
(per-date files) and quarterly fundamentals derived from yfinance (per-quarter
files). Runs once before the daily pipeline; **not** part of it.

**Idempotent and resumable** — safe to re-run:
- *Equities* resume at **year** granularity — fully-elapsed years already on disk
  are skipped; the current year is always re-pulled.
- *Fundamentals* resume at **symbol** granularity, checkpointing to disk every 50
  symbols (merging, not overwriting). An interrupted run loses at most ~50
  symbols of work and a re-run continues where it stopped.
- Pass `--no-resume` to ignore on-disk data and force a full re-pull.

```bash
./.venv/bin/python scripts/backfill.py --env paper
```

| Flag | Default | Description |
|---|---|---|
| `--env {paper,live}` | `paper` | Secrets file to load. |
| `--start ISO` | `settings.ingest.backfill_start` | First date to pull (e.g. `2020-07-27`). |
| `--end ISO` | today | Last date to pull. |
| `--symbols CSV` | full tradable universe | Restrict to a comma-separated subset, e.g. `AAPL,MSFT`. |
| `--skip-equities` | off | Skip the equities pull. |
| `--skip-fundamentals` | off | Skip the fundamentals pull. |
| `--fundamentals-universe {all,liquid}` | `all` | `liquid` scopes fundamentals to names passing the SPEC liquidity filter (ADV > $1M, price > $5) from the latest backfilled equities — skips the ~13k illiquid/ETF names we never trade (~2,700 vs ~13,000). |
| `--no-resume` | off | Re-pull everything, ignoring data already on disk. |

**Examples**

```bash
# Prove the pipeline on a handful of names first
./.venv/bin/python scripts/backfill.py --env paper --symbols AAPL,MSFT,SPY

# Fundamentals only, scoped to the liquid universe (the long ~2h run)
./.venv/bin/python scripts/backfill.py --env paper --skip-equities --fundamentals-universe liquid

# Equities only, for an explicit window
./.venv/bin/python scripts/backfill.py --env paper --skip-fundamentals --start 2024-01-01 --end 2024-12-31
```

Writes to `data/raw/equities/YYYY-MM-DD.parquet` and
`data/raw/fundamentals/YYYY-QN.parquet`.

---

## `engine/ingest.py` — daily incremental ingest

The daily pipeline's data step. Pulls prices for one date always, and
fundamentals only when the date opens a new quarter. **Skips non-trading days**
(logs and exits). Run as a module so package imports resolve.

```bash
./.venv/bin/python -m engine.ingest --env paper
```

| Flag | Default | Description |
|---|---|---|
| `--env {paper,live}` | `paper` | Secrets file to load. |
| `--date ISO` | today | The `as_of` date to ingest. |

**Examples**

```bash
# Ingest today
./.venv/bin/python -m engine.ingest --env paper

# Backfill-style re-ingest of a specific past trading day
./.venv/bin/python -m engine.ingest --env paper --date 2026-06-17
```

Writes the same Parquet layout as the backfill (identical schema by construction
— both use the same `ingest` helpers).

---

## `scripts/run_eod.py` — rebalance driver (the live engine)

The end-of-day orchestrator: `reconcile → holiday gate → compute targets → risk gate →
[overlay close calls] → execute equities → [overlay write calls] → monitor`. Two modes.
The Alpaca client is **paper-only** regardless of `--env`.

```bash
# Run exactly one rebalance cycle now
./.venv/bin/python scripts/run_eod.py --once --env paper

# Run the continuous scheduler (daily 16:10-ET branch + 60s monitor; Ctrl-C / SIGTERM to stop)
./.venv/bin/python scripts/run_eod.py --serve --env paper
```

| Flag | Default | Description |
|---|---|---|
| `--once` / `--serve` | — | **Required, mutually exclusive.** One cycle now, or the continuous APScheduler process. |
| `--env {paper,live}` | — | **Required.** Secrets file to load (`live` adds a 5s abort countdown but still trades paper). |
| `--date ISO` | today | The `as_of` rebalance date (`--once`). |
| `--force` | off | Run even if `--date` is not a trading day (`--once`). |
| `--skip-ingest` | off | Reuse the existing snapshot data instead of pulling first (`--once`). |
| `--no-overlay` | off | Equities only — skip the covered-call close/write legs. |

---

## `scripts/run_dashboard.py` — live dashboard server

Serves the FastAPI dashboard (live tab from Postgres + the Backtest tab) and, unless
disabled, runs the in-process Alpaca→Postgres monitor so the page self-updates without
`run_eod`. Open `http://127.0.0.1:8000`.

```bash
./.venv/bin/python scripts/run_dashboard.py --env paper
```

| Flag | Default | Description |
|---|---|---|
| `--env {paper,live}` | `paper` | Secrets file to load. |
| `--host` | `127.0.0.1` | Bind address. |
| `--port` | `8000` | Bind port. |
| `--no-monitor` | off | Disable the background monitor + live-orders layer (Postgres-only, static). |
| `--monitor-interval` | `60` | Seconds between background monitor snapshots. |

---

## `scripts/build_dashboard.py` — regenerate the Backtest tab

Runs the equity + covered-call + real-premium/VRP backtests and writes the static
`reports/backtest_dashboard.html` that the dashboard's **Backtest** tab serves. No flags;
re-run whenever the backtest inputs change. The DoltHub real-premium pull is cached after
the first run.

```bash
./.venv/bin/python scripts/build_dashboard.py
```

---

## Backtests — `scripts/backtest*.py`

Walk-forward system tests (Phase gates). All default to `2021-07-01 → today`.

```bash
# Phase 1 — factor sanity (quantile spread of the composite)
./.venv/bin/python scripts/backtest_factors.py --quintiles 5

# Phase 2 — equity-only walk-forward (beats SPY on return; Sharpe gate)
./.venv/bin/python scripts/backtest.py

# Phase 2b — covered-call overlay + real-premium/VRP analysis
./.venv/bin/python scripts/backtest_covered_calls.py --validate-bxm
```

| Script | Key flags |
|---|---|
| `backtest_factors.py` | `--start`, `--end`, `--quintiles` (default 5) |
| `backtest.py` | `--start`, `--end`, `--quiet` |
| `backtest_covered_calls.py` | `--start`, `--end`, `--validate-bxm` (cross-check vs ^BXM), `--no-real-premium` (model-only, no network) |

---

## `pytest` — test suite

```bash
# All unit tests
./.venv/bin/python -m pytest tests/unit -q

# A single file or test
./.venv/bin/python -m pytest tests/unit/test_backfill.py -q
./.venv/bin/python -m pytest tests/unit/test_backfill.py::test_backfill_equities_resume_skips_completed_year -q
```

Unit tests are offline (network clients are mocked / injected), so they need no
keys or database.
