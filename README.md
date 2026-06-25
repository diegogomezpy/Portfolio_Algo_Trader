# sharpe-engine

A systematic **factor-equity portfolio** with a **covered-call income overlay**,
run as a proprietary book for the firm and paper-traded on Alpaca.
The goal is uncorrelated, risk-adjusted USD returns that diversify a core
brokerage business. **Capital preservation is the primary mandate.**

This is not an alpha-seeking ML system. It is a disciplined implementation of
well-documented factor premia (quality, value, momentum, low-volatility)
combined with a volatility-risk-premium harvest via covered calls.

> ⚠️ **Status: early build (BUILD_ORDER Phase 0).** Paper trading only. Nothing
> here is investment advice. The execution layer is intentionally gated behind a
> validated backtest — see [docs/BUILD_ORDER.md](docs/BUILD_ORDER.md).

📊 **Live dashboard:** **https://sharpe-engine.tailc7136e.ts.net** (password-gated, user `viewer`).
Hosting details in [deploy/DEPLOY.md](deploy/DEPLOY.md#share-it-publicly-tailscale-funnel--password).

---

## Strategy at a glance

| Aspect | Choice |
|---|---|
| Universe | Full Alpaca US-equity universe, ADV > $1M, price > $5 |
| Factors | Quality, Value, Momentum, Low-Vol — equal weight to start |
| Objective | Max-Sharpe, long-only, capital-preservation constraints |
| Constraints | No shorting, sector cap, min position size, no direct leverage |
| Rebalancing | Monthly primary + L1 drift threshold secondary |
| Income overlay | Covered calls, delta ≈ 0.25, 30–45 DTE, roll at 21 DTE |
| Covariance | Fama-French 5-factor model |
| Broker | Alpaca (paper → live) |

The full rationale for every choice lives in [docs/DECISIONS.md](docs/DECISIONS.md),
which is the source of truth for *why* the system is built the way it is.

---

## How it works

A daily, idempotent pipeline:

```
ingest → factors → optimize → covered_calls → risk → execute
                                                   ↓
                          PostgreSQL (operational state) + dashboard
```

- **Data layer.** Raw daily OHLCV (Alpaca, `adjustment='all'`) and quarterly
  fundamentals (yfinance) are written to a **Parquet** store. **PostgreSQL**
  holds operational state (orders, fills, snapshots, lifecycle, audit). Alpaca
  is the source of truth and is reconciled at every startup.
- **Research vs. live share code.** Backfill, daily ingest, and the backtest
  reuse the same factor, optimizer, and execution logic — so what is validated
  is what runs.
- **Defensive by construction.** Structured JSON logging, retries with
  exponential backoff, market-calendar awareness, and pre-trade risk gating.

See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for the full design.

---

## Monitoring & operations

A live **dashboard** (FastAPI + one dark-themed page) serves current state from Postgres and
self-updates via an in-process Alpaca→Postgres monitor:

- **Overview** — NAV, day P&L, leverage gauge, cash, premium, and a global health bar (engine
  heartbeat, market clock, next rebalance, risk gate, drift, alerts).
- **Portfolio** — holdings vs target with drift, a nested **sector/ticker donut** + concentration
  metrics (HHI, effective names, sector caps), the **factor tilt** (book-weighted
  Quality/Value/Momentum/Low-Vol vs the universe), covered calls, and recent activity.
- **Performance** — growth vs SPY / covered-call-ETF benchmarks, a **Risk** sub-tab (drawdown,
  volatility, 1-day 95% VaR, rolling volatility, leverage), and execution slippage.
- **Backtest** — the static walk-forward + covered-call / variance-risk-premium analytics.

Deployed on a GCP VM as `systemd` services, shared read-only behind a password (Tailscale Funnel
+ Caddy basic auth) — see [deploy/DEPLOY.md](deploy/DEPLOY.md).

**Safety controls.** The monthly rebalance **catches up** automatically if a scheduled run is
missed/blocked (never twice a month). An **emergency killswitch**
(`scripts/killswitch.py --halt | --flatten`) stops the engine and cancels orders, or liquidates
the whole book to cash — see [docs/CLI.md](docs/CLI.md).

---

## Repository layout

```
.
├── docs/            # SPEC, ARCHITECTURE, DECISIONS, BUILD_ORDER, CLAUDE
├── engine/          # ingest, factors, optimize, covered_calls, execute,
│                    #   risk, reconcile, monitor, alpaca_client, config, db
├── scripts/         # backfill, init_db, run_eod, run_dashboard, backtest, killswitch
├── dashboard/       # FastAPI backend + single-page HTML dashboard
├── deploy/          # GCP VM systemd units, update/backup/watchdog, Tailscale funnel
├── tests/           # unit (per module) + integration (synthetic pipeline)
├── config/          # settings.yaml — all tuneable parameters
└── requirements.txt
```

---

## Setup

Requires Python 3.12+.

```bash
python3 -m venv .venv
./.venv/bin/pip install -r requirements.txt
```

**Credentials** are read only from the environment — never hardcoded, never
committed. Copy the template and fill in your keys:

```bash
cp .env.example .env.paper   # then edit .env.paper
```

| Variable | Purpose |
|---|---|
| `ALPACA_API_KEY` / `ALPACA_SECRET_KEY` | Alpaca paper credentials |
| `ALPACA_BASE_URL` | `https://paper-api.alpaca.markets` |
| `ALPACA_DATA_URL` | `https://data.alpaca.markets` |
| `DATABASE_URL` | `postgresql://localhost:5432/sharpe_engine` |
| `SMTP_PASSWORD` | Email alert credentials |

All tuneable parameters (universe filters, factor weights, covered-call deltas,
rebalance thresholds, ingest cadence) live in
[config/settings.yaml](config/settings.yaml).

---

## Usage

```bash
# Create the PostgreSQL tables (database must exist first)
./.venv/bin/python scripts/init_db.py --env paper

# One-time historical backfill (Parquet store)
./.venv/bin/python scripts/backfill.py --env paper

# Daily incremental ingest for a given date (defaults to today)
./.venv/bin/python -m engine.ingest --env paper --date 2026-06-17

# Run the test suite
./.venv/bin/python -m pytest tests/unit -q
```

### Data-feed note

The default Alpaca **`iex`** feed (free tier) provides consolidated history from
roughly **mid-2020 onward** and reports IEX-exchange volume only. Full history
back to 2016 and the consolidated tape require Alpaca's paid **SIP** feed — a
documented upgrade path, not a current dependency.

---

## Documentation

| Doc | What it covers |
|---|---|
| [docs/SPEC.md](docs/SPEC.md) | What the system does |
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | How it is built |
| [docs/DECISIONS.md](docs/DECISIONS.md) | Why key choices were made (source of truth) |
| [docs/BUILD_ORDER.md](docs/BUILD_ORDER.md) | Phase-by-phase build plan and gates |
| [docs/CLI.md](docs/CLI.md) | All command-line entry points and their flags |
| [docs/CLAUDE.md](docs/CLAUDE.md) | Primary anchor / working agreement |

---

## License

Proprietary. All rights reserved.
