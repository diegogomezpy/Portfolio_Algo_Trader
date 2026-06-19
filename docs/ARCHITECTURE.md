# ARCHITECTURE.md — sharpe-engine

How the system is built. Tech stack, module boundaries, data flow.

---

## Tech stack

| Layer | Choice |
|---|---|
| Language | Python 3.11+ |
| Optimizer | cvxpy |
| Covariance | Fama-French 5 factor model (pandas-datareader) |
| Scheduler | APScheduler (America/New_York timezone via pytz) |
| Data store — prices/fundamentals | Parquet files on disk |
| Data store — operational state | PostgreSQL |
| Price data | Alpaca Data API |
| Fundamental data | yfinance — current + historical quarterly financials |
| Broker | Alpaca Trading API |
| Dashboard | FastAPI + plain HTML/JS |
| Alerts | Email (SMTP) |
| Deployment | Local machine → cloud VM |

---

## System overview

```
┌─────────────────────────────────────────────────────┐
│              Scheduler (APScheduler / ET)           │
│     Monthly rebalance + daily drift monitor         │
└──────────────────────┬──────────────────────────────┘
                       │
            ┌──────────▼──────────┐
            │    ingest.py        │
            │ Alpaca + yfinance   │
            └──────────┬──────────┘
                       │
            ┌──────────▼──────────┐
            │    factors.py       │
            │ Quality/Value/Mom/  │
            │ LowVol factor scores│
            └────┬────────────────┘
                 │
            ┌────▼────────────────┐
            │    optimize.py      │
            │ cvxpy max-Sharpe    │
            │ FF5 covariance      │
            └────┬────────────────┘
                 │
            ┌────▼────────────────┐
            │  covered_calls.py   │
            │ Delta-0.25 strike   │
            │ selection + rolls   │
            └────┬────────────────┘
                 │
            ┌────▼────────────────┐
            │    risk.py          │
            │ Pre-trade gate      │
            └────┬────────────────┘
                 │
            ┌────▼────────────────┐
            │    execute.py       │
            │ Alpaca order router │
            └────┬────────────────┘
                 │
            ┌────▼────────────────┐
            │    PostgreSQL       │
            │ Orders/fills/state  │
            └────┬────────────────┘
                 │
            ┌────▼────────────────┐
            │    dashboard/       │
            │ FastAPI + HTML      │
            │ + email alerts      │
            └─────────────────────┘
```

---

## Module breakdown

### `engine/ingest.py`

Pulls all external data and writes to the Parquet store and PostgreSQL.
Runs at the start of every rebalance cycle and daily for drift monitoring.

**Alpaca Data API:**
- Daily OHLCV bars for full equity universe (adjustment='all')
- Options chain snapshots for held positions (for covered call management)
- Market calendar check (confirms trading day before proceeding)
- Feed: free `iex` tier — history back to ~2020-07-27, IEX-exchange volume
  only. Paid `sip` feed is the upgrade for 2016+ history and the
  consolidated tape (DECISIONS D21). `end` is an exclusive bar-timestamp
  boundary, so pulls go through `end+1` to include the as-of day.

**yfinance (fundamentals — backtest and live):**
- No API key required — free, installs via pip
- Current fundamentals: `ticker.info` keys: trailingPE, priceToBook,
  returnOnEquity, grossMargins
- Historical quarterly fundamentals derived from:
  `ticker.quarterly_financials` (income statement)
  `ticker.quarterly_balance_sheet`
  Historical P/E computed as: price_at_quarter_end / trailing_eps
  Historical P/B computed as: price_at_quarter_end / book_value_per_share
- Data written to `data/raw/fundamentals/YYYY-QN.parquet` keyed by
  ticker and quarter-end date
- Pulled quarterly; cached in Parquet and not re-fetched until next
  earnings cycle
- Rate-limit aware: add sleep between ticker pulls for large batches
- Known limitation: not point-in-time (current restated figures).
  Backtest quality/value signals may be slightly optimistic. Discount
  backtest Sharpe by ~5-10% mentally and document in any CADIEM presentation.
- Upgrade path: swap yfinance for FMP paid tier or Bloomberg API when
  available — only engine/ingest.py needs to change

**Idempotent:** re-running for the same date overwrites, does not duplicate.
**Missing assets:** log the skip, continue. No pipeline halt.
**API failure:** 3 retries with exponential backoff (30s, 60s, 120s),
then skip the day and alert.

---

### `engine/factors.py`

Computes the composite factor score for every stock in the universe.

**Input:** raw Parquet price data + yfinance fundamentals + universe filter

**Update frequency:** momentum and vol recomputed daily from prices.
Fundamentals used from last quarterly pull — not re-fetched between earnings.
**Output:** ranked DataFrame (ticker, composite_score, sub-scores)

**Universe filter (applied first):**
```python
universe = prices[
    (prices['close'] > settings.universe.min_price) &
    (prices['adv_20d'] > settings.universe.min_adv_usd)
]
```

**Sub-score computation:**

Quality score:
```
quality = z_score(ROE) + z_score(gross_margin)
quality_score = z_score(quality)
```

Value score (inverted so lower multiple = higher score):
```
value = z_score(-PE_ratio) + z_score(-PB_ratio)
value_score = z_score(value)
```

Momentum score (12-1 month, avoids short-term reversal):
```
momentum = return(t-252, t-21)
momentum_score = z_score(momentum)
```

Low-vol score (inverted so lower vol = higher score):
```
lowvol_score = z_score(-realized_vol_252d)
```

Composite score (equal weight to start):
```
composite = (quality_score + value_score + momentum_score + lowvol_score) / 4
```

All z-scores are cross-sectional — computed within the universe on each
rebalance date, not time-series z-scores.

Stocks with missing fundamentals (fundamental data unavailable) receive NaN for
quality and value sub-scores. Their composite is computed from available
sub-scores only, with a staleness flag set. The optimizer can optionally
exclude flagged stocks — configurable.

---

### `engine/optimize.py`

Constructs the target portfolio weights using cvxpy.

**Input:** factor scores, FF5 covariance matrix, current NAV,
constraint parameters from settings.yaml
**Output:** target weight vector over selected universe

**Covariance estimation:**
```python
import pandas_datareader.data as web

factors = web.DataReader('F-F_Research_Data_5_Factors_2x3_daily',
                         'famafrench', start=start_date)[0]

# OLS: regress each asset's returns on FF5 factors
# Σ = B @ F @ B.T + D  (factor + idiosyncratic)
```

**Optimization problem:**
```
maximize    μᵀw - λ · wᵀΣw
subject to  sum(w) = base_allocation
            w >= 0
            w_i >= w_min or w_i = 0
            sum(w[sector_k]) <= sector_cap  ∀k
```

Where μ is derived from composite factor scores:
```python
rank_score = rank(composite_score) / N   # 0 to 1
mu = rank_score * target_return_scale    # scaled to annualized return space
```
target_return_scale is a configurable parameter (e.g. 0.15 for 15% max
expected return). The scale relative to Σ determines portfolio concentration.
λ and target_return_scale are calibrated together in Phase 2 to produce
the desired 20-30 name portfolio. TBD — do not hardcode before calibration.

**Infeasibility handling:**
- Retry 1: sector cap + 5%
- Retry 2: sector cap + 10%, min position - $250
- Retry 3: sector cap + 15%, min position - $500
- After 3 retries: hold previous weights, log, alert

**Cash handling:** weights are normalized to invested capital
(NAV - cash). Cash from rounding and pending adjustments is
deployed at the next rebalance.

---

### `engine/covered_calls.py`

Manages the covered call overlay on held equity positions.

**Runs after optimize.py, before execute.py, on rebalance days.**
**Also runs daily to check for roll triggers, expiry, and earnings.**

**Monthly rebalance sequence:**
1. Close ALL existing covered calls (before optimizer runs)
2. After equity trades settle: rewrite calls fresh on all eligible positions

**Call writing (on rebalance, after equity trades settle):**
For each equity position:
1. Check contract sizing: compute max contracts writable
   - Try mini options first (10 shares per contract) if available on Alpaca
   - Fall back to standard (100 shares per contract)
   - If position too small for even one mini contract: skip, log warning
2. Check earnings calendar: if earnings date falls within 30-45 DTE window,
   skip writing call — will write after earnings announcement
3. Pull current options chain from Alpaca
4. Filter to contracts with 30 ≤ DTE ≤ 45
5. Select contract with delta closest to 0.25
6. Add sell order for maximum writable contracts

**Earnings monitoring (daily check):**
For each held call position:
- Check if underlying has earnings announcement within remaining DTE
- If yes and not already flagged: close call before announcement date
- After announcement: rewrite call at delta 0.25, 30-45 DTE

**Roll logic (daily check):**
For each held call position:
- If DTE ≤ 21: close existing call, write new call at delta 0.25, 30-45 DTE
- If DTE = 0: force-close at market open (runs before any other orders)

**Assignment handling:**
When Alpaca reports a call was exercised:
1. Position is gone — compute current composite factor score for that ticker
2. If score > settings.covered_calls.reentry_threshold: re-enter at market
3. If score ≤ threshold: flag cash as available for next monthly rebalance
4. Log the event to PostgreSQL `options_lifecycle` table

**Output:** list of options orders to add to the execution batch.

---

### `engine/risk.py`

Pre-trade gate. Called after covered_calls.py, before execute.py.
Returns (approved: bool, reason: str).

Checks:
- No single position > single_name_cap after proposed trades
- No sector > sector_cap after proposed trades
- Total notional ≤ NAV (no unintended leverage)
- All equity symbols in current approved universe
- All covered calls are against held equity positions (no naked calls)
- Covered call strike/expiry is valid (not already expired)

If any check fails: return False, log at ERROR, send alert.
No orders are submitted for that cycle if gate returns False.

---

### `engine/execute.py`

Translates approved order list into Alpaca API calls.

**Order sequencing:**
1. Force-close expiring options (DTE = 0) first
2. Equity sells (descending by size)
3. Equity buys (descending by size)
4. New covered call writes
5. Covered call rolls (close + open)

**Order types:**
- Equities: market order if ADV > $50M and spread < 0.1%, else limit at mid
- Options: limit order at mid price
- All equity prices use adjustment='all'

**Minimum trade filter:**
Trades below min_trade_usd are skipped and added to `pending_adjustments`
table in PostgreSQL. They accumulate and roll into the next rebalance.

**Fill tracking:**
Every order written to PostgreSQL `orders` table on submission.
Fill status polled from Alpaca and updated to `filled`, `partial`,
or `cancelled`. Partial fills trigger alert. Unfilled orders at
session end are cancelled; residual delta rolls to pending_adjustments.

**Idempotent:** checks `orders` table for existing submissions for
the current rebalance cycle before submitting. Safe to re-run.

**Order rejection:**
HTTP 4xx from Alpaca = permanent rejection. Skip the order, accumulate
delta in pending_adjustments, alert. No retry on rejection.

---

### `engine/reconcile.py`

Runs at pipeline startup before any other job (4:10 PM ET on rebalance
days, or at scheduler start on non-rebalance days for monitoring).

1. Fetch live positions from Alpaca Trading API
2. Fetch last known positions from PostgreSQL `snapshots` table
3. For each position: compare Alpaca qty vs DB qty
4. If divergence > threshold: update PostgreSQL to match Alpaca, alert
5. Alpaca is always source of truth
6. If Alpaca unreachable: block pipeline entirely, alert

---

### `engine/monitor.py`

Runs continuously every 60 seconds between rebalances.

- Computes live NAV from Alpaca positions + cash
- Computes L1 drift between current weights and last target weights
- If drift > threshold: log warning, flag for out-of-cycle rebalance
- Checks DTE of all held calls: if any ≤ 21, flags for roll
- Writes portfolio snapshot to PostgreSQL `snapshots` table
- Feeds data to dashboard via PostgreSQL

---

## Data stores

### Parquet store (`data/`)

```
data/raw/
├── equities/YYYY-MM-DD.parquet      ← OHLCV, one file per trading day
└── fundamentals/YYYY-QN.parquet     ← fundamentals, one per quarter
```

Each equities Parquet: one row per ticker, columns: open, high, low,
close, volume, adv_20d, spread. Written daily by ingest.py.

Each fundamentals Parquet: one row per ticker, columns: pe_ratio,
pb_ratio, roe, gross_margin, report_date, source. Written quarterly.

### PostgreSQL operational database

| Table | Contents |
|---|---|
| `orders` | Every submitted order: ticker, side, qty, type, status, fill_price, timestamp |
| `fills` | Fill confirmations from Alpaca |
| `snapshots` | Portfolio snapshots: weights, NAV, drift, timestamp |
| `rebalance_log` | Each rebalance: trigger reason, target weights, risk gate result, timestamp |
| `pending_adjustments` | Accumulated sub-threshold deltas rolling to next cycle |
| `alerts` | Alert history: type, message, timestamp, delivered |
| `options_lifecycle` | Covered call events: write, roll, assignment, force-close |
| `factor_scores` | Daily composite and sub-scores per ticker (for dashboard + audit) |

---

## Scheduler design

Single APScheduler process (`scripts/run_eod.py`).
All times in America/New_York via pytz.

**Monthly rebalance jobs (first trading day of month):**
- 4:10 PM — `reconcile_job`
- 4:12 PM — `holiday_check_job` (queries Alpaca calendar; cancels all
  downstream jobs if not a trading day)
- 4:15 PM — `ingest_job` (retries 3x with backoff on failure)
- 4:20 PM — `factors_job` (price factors recomputed fresh)
- 4:25 PM — `optimize_job`
- 4:28 PM — `covered_calls_close_job` (close all existing calls)
- 4:30 PM — `risk_and_execute_equity_job` (equity trades)
- 4:40 PM — `covered_calls_write_job` (rewrite calls after equity trades)
- 4:45 PM — `risk_and_execute_options_job` (submit covered call writes)

**Daily jobs (every trading day):**
- 4:10 PM — `reconcile_job`
- 4:12 PM — `holiday_check_job`
- 4:15 PM — `ingest_job` (prices only, not fundamentals)
- 4:20 PM — `factors_job` (price factors: momentum + vol refreshed from today's prices)
- 4:25 PM — `drift_check_job` (check L1 drift using fresh price factors + cached fundamentals)
- 4:30 PM — `options_check_job` (check DTE, earnings dates, trigger rolls/force-closes)

**Continuous:**
- Every 60s — `monitor_job`

Jobs are chained with dependencies. If any upstream job fails or skips,
all downstream jobs cancel for that day. Alert sent on any cancellation.

---

## Configuration (`config/settings.yaml`)

```yaml
universe:
  min_price: 5.0
  min_adv_usd: 1_000_000

ingest:
  backfill_start: "2020-07-27"   # free IEX feed history floor (DECISIONS D21)
  adv_window: 20                 # trading days for average dollar volume
  lookback_days: 40              # calendar days pulled daily to compute adv_20d
  symbol_chunk_size: 200         # symbols per multi-symbol Alpaca bars request
  request_sleep_s: 0.2           # rate-limit pause between batched requests
  retry_backoff_s: [30, 60, 120] # exponential backoff on API failure

portfolio:
  nav: 100_000
  min_position_usd: 4_000      # sized for contract eligibility
  max_single_name_pct: 0.10
  max_sector_pct: 0.30
  base_equity_allocation: 0.95   # 95% equity, 5% cash buffer (no regime filter)
  infeasibility_max_retries: 3
  risk_aversion_lambda: 1.0

factors:
  weights:
    quality: 0.25
    value: 0.25
    momentum: 0.25
    low_vol: 0.25
  momentum_long_window: 252      # 12 months
  momentum_short_window: 21      # 1 month (excluded from momentum)
  vol_window: 252
  exclude_missing_fundamentals: false

rebalancing:
  primary: monthly               # first trading day of month
  drift_threshold_l1: 0.08      # TBD — calibrate in backtest

covered_calls:
  target_delta: 0.25
  min_dte_entry: 30
  max_dte_entry: 45
  roll_dte_trigger: 21
  min_holding_days: 10           # don't write calls on positions about to be exited (Phase 4)
  reentry_threshold: 0.0         # reenter if composite score > 0 after assignment
  prefer_mini_contracts: true    # use 10-share mini contracts where available
  close_before_earnings: true    # always close calls before earnings announcement

execution:
  min_trade_usd: 500
  large_cap_adv_threshold: 50_000_000
  spread_threshold: 0.001
  price_adjustment: all

covariance:
  estimation_window_days: 60
  factor_source: pandas_datareader

data_retention:
  raw_equities_days: 730         # 2-year rolling
  fundamentals: forever

alerts:
  smtp_host: TBD
  smtp_port: 587
  staleness_threshold_pct: 0.10
```

---

## Startup

**Terminal 1 — scheduler:**
```bash
python scripts/run_eod.py --env paper
```
Loads `.env.paper`. Starts APScheduler. `--env live` shows 5-second
countdown warning before proceeding.

**Terminal 2 — dashboard:**
```bash
python scripts/run_dashboard.py
```
Starts FastAPI on localhost:8000. Read-only. Can be started and
stopped independently of the scheduler.

---

## Shutdown

SIGTERM handler installed at scheduler startup.
On SIGTERM: finish current stage → cancel open orders → log shutdown → exit 0.
On SIGKILL: idempotency + reconciliation at next startup recovers state.

---

## Key design principles

- **No ML models** — factor scores are explicit, auditable computations.
  Every score can be traced back to a data point and a formula.
- **Idempotency everywhere** — every stage can be re-run safely.
- **Fail loudly** — unexpected errors raise, log, and alert.
- **Alpaca is source of truth** — PostgreSQL is reconciled to Alpaca
  at every startup, never the reverse.
- **Config over code** — every threshold and parameter in settings.yaml.
- **Paper first** — only ALPACA_BASE_URL changes to go live.