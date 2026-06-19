# CLAUDE.md — sharpe-engine

Primary anchor for Claude Code. Read this first, then docs/ in order.

---

## What this project is

A systematic factor equity portfolio with a covered call income overlay,
run as a proprietary book for CADIEM Casa de Bolsa. The goal is to generate
uncorrelated, risk-adjusted USD returns that diversify CADIEM's core
brokerage business. Capital preservation is the primary mandate.

This is not an alpha-seeking ML system. It is a systematic implementation
of well-documented factor premia (quality, value, momentum, low-volatility)
combined with a volatility risk premium harvest via covered calls.

---

## Repo layout

Modules marked ⏳ are planned for a later phase and not built yet (see
BUILD_ORDER.md); everything else exists today.

```
sharpe-engine/
├── README.md                  ← project overview + setup
├── .env.paper                 ← paper trading credentials (never commit)
├── .env.live                  ← live trading credentials (never commit)
├── .env.example               ← credential template (committed)
├── .gitignore                 ← excludes .env.*, data/, logs/
├── docs/
│   ├── CLAUDE.md              ← you are here (primary anchor)
│   ├── SPEC.md                ← what the system does
│   ├── ARCHITECTURE.md        ← how it is built
│   ├── DECISIONS.md           ← why key choices were made
│   ├── BUILD_ORDER.md         ← phase-by-phase build plan
│   └── CLI.md                 ← all command-line entry points
├── data/
│   └── raw/
│       ├── equities/          ← daily OHLCV Parquet per date
│       └── fundamentals/      ← quarterly fundamental snapshots
├── engine/
│   ├── config.py              ← env + settings.yaml loader, Alpaca client factory
│   ├── logger.py              ← shared structured JSON logger
│   ├── db.py                  ← PostgreSQL schema + connection factory
│   ├── alpaca_client.py       ← thin Alpaca REST wrapper
│   ├── ingest.py              ← Alpaca + yfinance data pulls
│   ├── reconcile.py           ← position reconciliation vs Alpaca (stub)
│   ├── factors.py             ← ⏳ factor score computation
│   ├── optimize.py            ← ⏳ cvxpy max-Sharpe optimizer
│   ├── covered_calls.py       ← ⏳ covered call selection + roll logic
│   ├── execute.py             ← ⏳ order routing + Alpaca execution
│   ├── risk.py                ← ⏳ pre-trade risk gate
│   └── monitor.py             ← ⏳ drift monitor + NAV tracking
├── dashboard/                 ← ⏳ Phase 5
│   ├── app.py                 ← FastAPI backend
│   └── static/index.html      ← single-page dashboard
├── scripts/
│   ├── init_db.py             ← create the PostgreSQL schema
│   ├── backfill.py            ← one-time historical data pull
│   ├── backtest.py            ← ⏳ walk-forward backtest
│   ├── run_eod.py             ← ⏳ scheduler entry point (terminal 1)
│   └── run_dashboard.py       ← ⏳ dashboard entry point (terminal 2)
├── tests/
│   ├── unit/                  ← one file per engine module
│   └── integration/           ← full pipeline tests on synthetic data
├── config/
│   └── settings.yaml          ← all tuneable parameters
└── requirements.txt
```

---

## Settled decisions

| Decision | Choice |
|---|---|
| Broker | Alpaca (paper → live) |
| Universe | Full Alpaca equity universe, ADV > $1M, price > $5 |
| Strategy | Factor equity portfolio + covered call overlay |
| Factors | Quality, value, momentum, low-vol — equal weight to start |
| Factor weights | Equal weight initially, adjust after backtesting |
| Objective | Max Sharpe, capital preservation mandate |
| Constraints | Long-only, sector cap, min position size, no direct leverage |
| Rebalancing | Monthly primary, L1 drift threshold secondary |
| Covered calls | Delta = 0.25, 30-45 DTE, roll at 21 DTE |
| Covered call rebalance | Close all calls before monthly rebalance, rewrite fresh after new weights set |
| Earnings policy | Close calls before earnings date, rewrite after announcement |
| Contract sizing | Mini options (10 shares) where available, standard (100 shares) otherwise |
| Factor freshness | Price factors (momentum, vol) recomputed daily; fundamentals from last quarterly pull |
| Assignment policy | Conditional re-entry if stock still scores above threshold |
| Regime filter | None — quality and low-vol factors handle defensiveness naturally |
| Crypto | Small allocation, size TBD after equities validated |
| Leverage | TBD — paper trade unlevered first, 1.5x margin after validation |
| Covariance | Fama-French 5 factor model via pandas-datareader |
| Price data | Alpaca Data API, adjustment='all' |
| Fundamental data | SEC EDGAR for deep point-in-time history (DECISIONS D22); yfinance current-quarter fallback |
| Data stores | Parquet (raw prices + fundamentals), PostgreSQL (operational) |
| Scheduler | APScheduler, America/New_York timezone via pytz |
| Dashboard | FastAPI + plain HTML, two separate processes |
| Alerts | Email via SMTP |
| Deployment | Local machine, cloud VM later |
| Environment | .env.paper / .env.live, --env flag, 5s countdown on live |
| Testing | Unit + integration + backtest as system test |
| Shutdown | SIGTERM handler: finish stage, cancel orders, exit 0 |
| Logging | Daily rotation, 30-day retention, structured JSON |
| Backfill | Target ~2016; currently from 2020-07-27 on the free IEX feed (DECISIONS D21). One-time script, separate from daily ingest |
| Survivorship bias | ADV + price filter, point-in-time |
| Settlement | T+1 equities, immediate crypto |
| Corporate actions | adjustment='all' on all Alpaca price pulls |
| Reconciliation | Every pipeline startup, Alpaca is source of truth |
| Holiday detection | Alpaca market calendar API |
| API failure | 3 retries exponential backoff, skip day and alert |
| Order rejection | Permanent errors skip + alert, accumulate in pending_adjustments |
| Raw data retention | 2-year rolling window for prices, fundamentals forever |
| Staleness (weekends) | Friday FF5 data used for Saturday/Sunday crypto runs |

---

## Open / TBD

- Crypto allocation size (address after equities phase validated)
- L1 drift threshold for secondary rebalance (calibrate in backtest)
- Sector cap level (start at 30%, adjust in backtest)
- Factor weight adjustment (equal weight now, refine after backtest)
- Fundamental data upgrade — resolved to **SEC EDGAR** for deep point-in-time
  history (DECISIONS D22); build pending. yfinance remains the current-quarter fallback.
- Go-live gate criteria (user-defined before real capital deployed)
- DB backup strategy (required before go-live)
- Secret rotation policy (required before go-live)
- Leverage implementation (1.5x margin, after live validation)

---

## Key constraints for Claude Code

- Never hardcode API keys — always read from environment variables
- All parameters in config/settings.yaml, never scattered in source
- Every module independently testable — no hidden state between modules
- Execution engine must be idempotent — safe to re-run mid-rebalance
- Backtest and live share the same factor, optimizer, and execution logic
- Log everything: every factor score, every order, every fill, every drift
- Never commit .env files, data directories, or log files

---

## Environment variables

```
ALPACA_API_KEY
ALPACA_SECRET_KEY
ALPACA_BASE_URL          # paper: https://paper-api.alpaca.markets
ALPACA_DATA_URL          # https://data.alpaca.markets
DATABASE_URL             # postgresql://user:pass@localhost:5432/sharpe_engine
SMTP_PASSWORD            # email alert credentials
# No Bloomberg API or FMP needed — yfinance handles fundamentals
```

---

## Where to start

See docs/BUILD_ORDER.md. Phase 0 is data pipeline + backfill.
Do not build the execution engine until the factor model is
validated in backtest.