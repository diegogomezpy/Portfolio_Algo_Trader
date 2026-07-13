# sharpe-engine

A systematic **factor-equity portfolio** with a **covered-call income overlay**, paper-traded on
Alpaca — plus an on-the-fly **strategy studio** for composing and running new systematic strategies
from the browser, no code required. The goal is uncorrelated, risk-adjusted USD returns with
**capital preservation as the primary mandate**.

This is not an alpha-seeking ML system. It is a disciplined implementation of well-documented factor
premia (quality, value, low-beta, low-volatility) combined with a volatility-risk-premium harvest via
index call spreads.

> ⚠️ **Paper trading only. Not investment advice.** Everything here runs against an Alpaca *paper*
> account. Nothing in this repository is a recommendation to buy or sell any security. Numbers shown
> on the dashboard are simulated fills on paper capital.

📊 **Live dashboard (public, no login):** **https://sharpe-engine.tailc7136e.ts.net**
The whole fund is openly viewable. *Executing* anything — trades, adding an account, saving or running
a strategy — requires an operator token (`SEPI_EXEC_TOKEN`); see [Security model](#security-model).

---

## What's interesting here

- **A no-code strategy studio.** Compose a systematic strategy in the browser — pick factor/fundamental
  signals and weight them, *or* write a free-form **formula** over the full data vocabulary
  (`0.5*quality + 0.3*z(raw_ep) - 0.2*raw_beta`), set every construction/universe/overlay/cadence
  parameter, **preview** the exact book it would trade, then **save**, **run**, or hand it to an
  autonomous scheduler — each strategy isolated on its own brokerage account. See
  [docs/PLATFORM.md](docs/PLATFORM.md).
- **A real income overlay.** The low-beta book is overwritten with a defined-risk **SPY call spread**
  sized to the book's market beta — harvesting index option premium the single-name options are too
  illiquid to sell.
- **An institutional-grade live dashboard.** NAV to the cent, holdings heatmap, factor tilt, drawdown /
  VaR / rolling-vol risk analytics, per-fill slippage vs arrival mid, regulatory-fee attribution, and a
  live execution visualizer — all self-updating from Postgres + the Alpaca feed.
- **Research and live share one code path.** The backtest, daily ingest, and live rebalance reuse the
  same factor, optimizer, and execution logic, so what is validated is what runs.

---

## Strategy at a glance (the primary book)

| Aspect | Choice |
|---|---|
| Universe | US equities, ADV > $1M, price > $5, SEC-EDGAR (US-GAAP) filers only |
| Factors | Quality, Value, **Low-Beta**, Low-Vol — equal weight (0.25 each) |
| Construction | Constrained optimizer — long-only, ≤ 5% / name, ≥ 4% floor, ≤ 30% / sector |
| Leverage | 2× gross on the paper book (risk-gate-capped) to make the overlay testable |
| Income overlay | SPY call **spread** (β-overwrite), short ≈ 0.30Δ / long ≈ 0.05Δ wing, 30–45 DTE |
| Rebalancing | **Monthly** (first trading day, 13:00 ET); drift is telemetry, not a trigger |
| Covariance | Fama-French 5-factor model |
| Broker | Alpaca (paper) |

The full rationale for every choice lives in [docs/DECISIONS.md](docs/DECISIONS.md) — the source of
truth for *why* the system is built the way it is. (Momentum and a raft of single-metric traits are
available in the strategy studio even though the primary book runs the four factors above.)

---

## The strategy studio

Beyond the fixed primary book, the engine is a small **platform** for building systematic strategies
declaratively. Full reference: **[docs/PLATFORM.md](docs/PLATFORM.md)**. In short:

- **Vocabulary** — nine z-scored **signals** (`quality`, `value`, `low_beta`, `low_vol`, `roe`,
  `gross_margin`, `earnings_yield`, `book_yield`, `momentum`) and nine raw **fields** (`raw_pe`,
  `raw_beta`, `raw_vol`, …). Bare names are z-scored (higher = better); `raw_*` names are native units.
- **Formula engine** — a *sandboxed* (not `eval`) arithmetic mini-language over that vocabulary with
  `z() rank() winsor() clip() where() log() …`; attribute access, imports, subscripts, lambdas and
  comprehensions are rejected at parse time.
- **Full parameter surface** — construction method + caps, leverage, universe filters, factor windows,
  overlay, rebalance cadence and execution mode are all per-strategy, threaded through a settings
  overlay so a strategy's own caps/leverage govern *its* optimizer and risk gate.
- **Isolated execution** — each strategy trades its **own** Alpaca account (encrypted creds), can never
  touch the primary book, and can be **previewed** (dry-run, no orders), **run on demand**, or driven
  by an **autonomous per-account scheduler** (monthly / weekly / daily; opt-in, defaults off).

---

## How it works

A daily, idempotent pipeline:

```
ingest → factors / signals → optimize → overlay → risk gate → execute
                                                       ↓
                             PostgreSQL (operational state) + live dashboard
```

- **Data layer.** Raw daily OHLCV (Alpaca, `adjustment='all'`) and point-in-time fundamentals (SEC
  EDGAR, yfinance fallback) are written to a **Parquet** store. **PostgreSQL** holds operational state
  (orders, fills, snapshots, option lifecycle, per-account strategy specs, audit). Alpaca is the source
  of truth and is reconciled at every startup.
- **Defensive by construction.** Structured JSON logging, retries with exponential backoff,
  market-calendar awareness, a pre-trade risk gate, and a monthly rebalance that **catches up** if a run
  is missed (never twice a month). An emergency killswitch (`scripts/killswitch.py --halt | --flatten`)
  stops the engine or liquidates to cash.

See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for the full design.

---

## Security model

The dashboard is **publicly viewable** — the live book, performance, holdings, factor scores and
strategy configuration are all world-readable (it's a paper book on display). Everything that *changes
state or touches broker credentials* is gated inside the app by a single operator token,
`SEPI_EXEC_TOKEN` (sent as the `X-Exec-Token` header; compared in constant time; fails closed if unset):

| Public (view) | Token-gated (operate) |
|---|---|
| NAV, holdings, performance, risk, factors, config, the builder palette + preview inputs | Place / cancel trades, set leverage override, add / remove accounts, **save / run a strategy**, and the account roster + per-sleeve balances |

Removing the view password never weakened execution gating — the two are independent. Broker secrets
are Fernet-encrypted at rest and **never** returned by any endpoint (only a masked key fingerprint).
See [deploy/DEPLOY.md](deploy/DEPLOY.md#share-it-publicly-tailscale-funnel).

---

## Repository layout

```
.
├── docs/            # SPEC, ARCHITECTURE, PLATFORM, DECISIONS, BUILD_ORDER, CLI, CLAUDE, adr/
├── engine/          # ingest, factors, signals, formula, config_strategy, account_runner,
│                    #   specstore, scheduler, optimize, covered_calls, execute, risk,
│                    #   reconcile, monitor, credstore, alpaca_client, config, db
├── scripts/         # backfill, init_db, run_eod, run_dashboard, run_config_strategy, backtest, killswitch
├── dashboard/       # FastAPI backend + single-page HTML dashboard (Overview / Portfolio / Performance / Execute / Builder)
├── deploy/          # GCP VM systemd units, update/backup/watchdog, Tailscale Funnel + Caddy proxy
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

**Credentials** are read only from the environment — never hardcoded, never committed. Copy the
template and fill in your keys:

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
| `SEPI_EXEC_TOKEN` | Operator token that gates every execution / account action (dashboard) |

All tuneable parameters (universe filters, factor weights, overlay deltas, rebalance cadence, ingest
cadence) live in [config/settings.yaml](config/settings.yaml).

---

## Usage

```bash
# Create the PostgreSQL tables (database must exist first)
./.venv/bin/python scripts/init_db.py --env paper

# One-time historical backfill (Parquet store)
./.venv/bin/python scripts/backfill.py --env paper

# Daily incremental ingest for a given date (defaults to today)
./.venv/bin/python -m engine.ingest --env paper --date 2026-06-17

# Dry-run a config strategy on a non-primary account (no orders)
./.venv/bin/python scripts/run_config_strategy.py --account trend \
    --signals quality=0.5,value=0.5 --construction optimizer --dry-run

# Run the test suite
./.venv/bin/python -m pytest tests/unit -q
```

### Data-feed note

The default Alpaca **`iex`** feed (free tier) provides consolidated history from roughly **mid-2020
onward** and reports IEX-exchange volume only. Full history back to 2016 and the consolidated tape
require Alpaca's paid **SIP** feed — a documented upgrade path, not a current dependency.

---

## Documentation

| Doc | What it covers |
|---|---|
| [docs/SPEC.md](docs/SPEC.md) | What the system does |
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | How it is built |
| [docs/PLATFORM.md](docs/PLATFORM.md) | The on-the-fly strategy studio (vocabulary, formula, params, scheduler, security) |
| [docs/DECISIONS.md](docs/DECISIONS.md) | Why key choices were made (source of truth) |
| [docs/BUILD_ORDER.md](docs/BUILD_ORDER.md) | Phase-by-phase build plan and gates |
| [docs/CLI.md](docs/CLI.md) | All command-line entry points and their flags |
| [docs/adr/](docs/adr/) | Architecture decision records (ADR-001: the multi-strategy platform) |

---

## License

All rights reserved. Source-available for reference; not licensed for reuse or redistribution.
