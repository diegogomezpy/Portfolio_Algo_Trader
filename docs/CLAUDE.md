# CLAUDE.md — sharpe-engine

Primary anchor for Claude Code. Read this first, then docs/ in order.

---

## What this project is

A systematic factor equity portfolio with a covered call income overlay,
run as a proprietary book for the firm. The goal is to generate
uncorrelated, risk-adjusted USD returns that diversify the firm's core
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
│   ├── reconcile.py           ← position reconciliation vs Alpaca (Phase 3 ✅)
│   ├── factors.py             ← factor score computation (Phase 1 ✅)
│   ├── edgar.py               ← SEC EDGAR point-in-time fundamentals (D22)
│   ├── sectors.py             ← SIC→sector map for the sector cap (Phase 2 ✅, D23c)
│   ├── covariance.py          ← FF5 factor-model covariance (Phase 2 ✅, D23a)
│   ├── optimize.py            ← alpha-driven weighting + constraints (Phase 2 ✅, D24/D25)
│   ├── options.py             ← Black-Scholes for the covered-call overlay (Phase 2b ✅)
│   ├── broker.py              ← Alpaca write client: equity + option orders (Phase 3/4 ✅)
│   ├── covered_calls.py       ← covered-call overlay: strike/write/close/earnings (Phase 4 ✅)
│   ├── execute.py             ← order routing + execution (Phase 3 ✅)
│   ├── risk.py                ← pre-trade risk gate (Phase 3 ✅)
│   ├── monitor.py             ← NAV tracking + drift telemetry (Phase 3 ✅)
│   └── alerts.py              ← DB-recorded, dry-run-capable email alerts (Phase 5 ✅)
├── dashboard/                 ← live dashboard (Phase 5 ✅)
│   ├── app.py                 ← FastAPI backend (Postgres reads + self-updating monitor/live-orders layer)
│   ├── data.py                ← dashboard read queries
│   └── static/{index.html,theme.css} ← dark-institutional single-page dashboard (polls /api/*)
├── scripts/
│   ├── init_db.py             ← create the PostgreSQL schema
│   ├── backfill.py            ← one-time historical data pull
│   ├── backtest.py            ← walk-forward equity backtest (Phase 2 ✅)
│   ├── backtest_covered_calls.py ← covered-call overlay + real-premium/VRP backtest (Phase 2b ✅, D33)
│   ├── build_dashboard.py     ← regenerate the Backtest-tab analytics (Phase 5 ✅)
│   ├── run_eod.py             ← rebalance driver: --once / --serve (Phase 3 ✅)
│   └── run_dashboard.py       ← dashboard entry point, terminal 2 (Phase 5 ✅)
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
| Constraints | Long-only, sector cap, min position size, leverage capped at `max_leverage` (risk gate) |
| Rebalancing | Monthly only — first trading day; equities + covered calls together (DECISIONS D31). Drift is telemetry, not a trigger |
| Covered calls | Delta = 0.30 (D29), 30-45 DTE, rewritten monthly — no mid-cycle roll (D31) |
| Covered call rebalance | Close all calls before monthly rebalance, rewrite fresh after new weights set |
| Earnings policy | Close calls before earnings date, rewrite after announcement |
| Contract sizing | Standard 100-share contracts only; single-name 10-share minis were delisted ~2014 (DECISIONS D32) — a position must hold ≥100 shares to be covered |
| Factor freshness | Price factors (momentum, vol) recomputed daily; fundamentals from last quarterly pull |
| Assignment policy | Conditional re-entry if stock still scores above threshold |
| Regime filter | None — quality and low-vol factors handle defensiveness naturally |
| Crypto | Small allocation, size TBD after equities validated |
| Leverage | 2:1 on the paper book now (DECISIONS D32, `target_leverage=2.0`, risk-gate capped) to make the covered-call overlay testable; revisit before live |
| Covariance | Fama-French 5 factor model; FF5 daily downloaded directly from the Ken French data library (DECISIONS D23a — pandas-datareader removed, it import-crashes under pandas 3.0) |
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
- (resolved D31) No secondary/drift-triggered rebalance — single monthly cadence
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

See docs/BUILD_ORDER.md. Phases 0-1 and **2 + 2b** are ✅ gate-passed (factor model +
covered-call overlay validated in backtest — D27). **Phase 3 (execution engine + risk
gate) is code-complete (3.1–3.7, equity-only) and tested**; its live-paper gate criteria
(orders filling in the Alpaca dashboard, no duplicate orders on re-run, SIGTERM shutdown)
are pending a run against the paper account. **Phase 4 (covered-call overlay) is
code-complete (4.0–4.5: leverage, strike selection, broker option orders, write/close,
earnings-close + expiry + rewrite + assignment re-entry, 4.0–4.6) and tested.**
**Phase 5 (alerting + live dashboard) is code-complete and tested** — alerts.py records and
(now) live-emails the 6 SPEC alert types via Gmail SMTP (`send_enabled: true`, App Password
in `SMTP_PASSWORD`); the dark-institutional dashboard (app.py + static/) serves live state
from Postgres, self-updates via an in-process Alpaca→Postgres monitor + live-orders read, and
its Backtest tab carries the real-premium/variance-risk-premium analytics (D33). The one
remaining gate is the **live-paper verification of the full strategy**. **Phase 6 (crypto)
and Phase 7 (go-live) follow.**