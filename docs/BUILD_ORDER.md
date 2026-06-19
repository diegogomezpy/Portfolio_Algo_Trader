# BUILD_ORDER.md — sharpe-engine

Phase-by-phase build plan. Do not proceed to the next phase until the
gate criteria are met. Claude Code should always know which phase is
active and never build ahead of it.

**Active phase: 1** (factor scoring). Status legend:
✅ done · 🔄 in progress · ⬜ not started.

---

## Overview

| Phase | Scope | Gate | Status |
|---|---|---|---|
| 0 | Scaffold + data pipeline + backfill | Clean data in Parquet and PostgreSQL | ✅ |
| 1 | Factor scoring | Factor scores look reasonable on historical data | ✅ |
| 2 | Optimizer + equity-only backtest | Backtest Sharpe > 1.0 after transaction costs | ⬜ |
| 2b | Full strategy backtest including covered calls | Combined strategy Sharpe validated | ⬜ |
| 3 | Execution engine + risk gate | Orders submit and fill correctly in paper account | ⬜ |
| 4 | Covered call overlay | Calls written, rolled, and assigned correctly in paper | ⬜ |
| 5 | Dashboard + alerting | Live dashboard shows portfolio state, alerts deliver | ⬜ |
| 6 | Crypto allocation | Crypto positions added to paper portfolio | ⬜ |
| 7 | Go-live | User-defined gate criteria met | ⬜ |
| 8 | Leverage (TBD) | Live track record validated, CADIEM approval | ⬜ |

---

## Phase 0 — Scaffold + data pipeline + backfill

**Goal:** Working data pipeline with clean historical data ready for
factor computation.

**Status (2026-06-18):** ✅ Complete. Equities backfilled (mid-2020→present;
see gate note), fundamentals backfilled (scoped to the liquid universe;
~2,083 names with statement-derived fundamentals). Postgres schema created.
Unit tests 22/22.

**Build:**

1. ✅ Repo structure — create all directories per CLAUDE.md layout
2. ✅ `.gitignore` — exclude .env.*, data/, logs/, *.pyc, __pycache__
3. ✅ `.env.paper` and `.env.live` — credential file templates (do not
   populate with real keys in the repo)
4. ✅ `config/settings.yaml` — stub all parameter groups with starting
   values from ARCHITECTURE.md; nothing hardcoded in source files
5. ✅ `requirements.txt`:
   ```
   alpaca-py, yfinance, pandas, pyarrow, psycopg2-binary, sqlalchemy,
   apscheduler, cvxpy, pandas-datareader, requests,
   fastapi, uvicorn, pytz, pytest, python-dotenv, numpy, scipy
   ```
6. ✅ PostgreSQL setup — create `sharpe_engine` database and all tables
   defined in ARCHITECTURE.md via a `scripts/init_db.py` migration script
7. ✅ `engine/logger.py` — shared structured JSON logger; daily rotation,
   30-day retention; imported by every module
8. ✅ `scripts/backfill.py` — one-time historical data pull (target ~2016;
   currently mid-2020 on the free IEX feed — see gate note and DECISIONS D21):
   - Alpaca historical OHLCV for full equity universe (adjustment='all')
   - yfinance historical quarterly fundamentals (scoped to the liquid
     universe via `--fundamentals-universe liquid`)
   - Derives historical P/E and P/B from price history + quarterly EPS
     and book value; writes to `data/raw/fundamentals/YYYY-QN.parquet`
   - Rate-limit aware and **resumable**: sleeps, retry/backoff, per-year
     (equities) and per-50-symbol checkpoint (fundamentals); idempotent
   - This script runs once before anything else; not part of daily pipeline
9. ✅ `engine/ingest.py` — daily incremental pull:
   - Today's OHLCV from Alpaca
   - yfinance fundamentals only if current quarter not already cached
   - Market calendar check via Alpaca API
   - 3 retries with exponential backoff (30s, 60s, 120s) on any failure
   - Same output schema as backfill — downstream modules see no difference
10. ✅ `engine/reconcile.py` — position reconciliation stub (can be minimal
    at this stage since no positions exist yet, but module must exist)

**Gate criteria:**
- ✅ `python scripts/backfill.py` completes without errors
- ⚠️ `data/raw/equities/` contains Parquet files — **mid-2020 → present**,
  not 2016: the free Alpaca IEX feed only serves history back to
  ~2020-07-27 (DECISIONS D21). Full 2016+ history needs the paid SIP feed.
- ✅ `data/raw/fundamentals/` contains quarterly fundamental snapshots —
  scoped backfill to the liquid universe complete (~2,083 names with
  statement-derived fundamentals; the rest are ETFs/ADRs/funds/recent
  listings yfinance cannot derive quarterly fundamentals for)
- ✅ `python -m engine.ingest` runs correctly for today's date
- ✅ Backfill and daily ingest produce identical Parquet schema
- ✅ Re-running either for the same date overwrites cleanly
- ✅ `pytest tests/unit/test_ingest.py` passes (mock Alpaca + yfinance)
- ✅ No credentials hardcoded anywhere; all from environment

---

## Phase 1 — Factor scoring

**Status (2026-06-19):** ✅ Complete — gate passed. See "Gate result" below.

**Goal:** Factor scores computed for the full universe, producing
sensible output on historical data.

**Build:**

1. `engine/factors.py` — full factor score computation:
   - Universe filter (price > min_price, ADV > min_adv_usd)
   - Quality sub-score: double-z of z_score(ROE) + z_score(gross_margin)
   - Value sub-score: double-z of z_score(E/P) + z_score(B/P) — earnings/book
     YIELD, not -PE/-PB; avoids the negative-PE inversion trap (~24% of names)
   - Momentum sub-score: 12-1 month return, cross-sectional z-score
   - Low-vol sub-score: z_score(-realized_vol_252d)
   - Composite: equal-weighted average of four z-scored sub-scores; each metric
     percentile-winsorized (winsor_pct) before z-scoring; missing fields
     neutral-filled per-field so partial fundamentals still contribute
   - Staleness flag for stocks missing any fundamental field (diagnostic only —
     does not zero the score)
   - Write daily factor scores to PostgreSQL `factor_scores` table

2. `scripts/backtest_factors.py` — quick sanity check before optimizer:
   - For each month from 2018 to present (⚠️ bounded to mid-2020→present
     on the free IEX feed until SIP is enabled — DECISIONS D21):
     - Compute factor scores on universe
     - Take top quintile by composite score, equally weight
     - Compute next-month return
   - Output: quintile spread (top vs bottom), hit rate, monthly IC

**Gate criteria:**
- ✅ Factor scores computed for full universe without errors
- ✅ Top quintile of composite score shows positive spread over bottom
  quintile in backtest (doesn't need to be large, just directionally correct)
- ✅ No forward-looking bias: momentum and vol use only data available
  at computation date; fundamentals use the most recent quarterly report
  available at that date (not current restated values) — point-in-time via
  EDGAR filed date (report_lag_days=0); 3 no-look-ahead unit tests
- ✅ `pytest tests/unit/test_factors.py` passes

**Gate result (2026-06-19, `scripts/backtest_factors.py` 2021-08 → 2026-05,
58 monthly rebalances, ~1,759 names/mo):**
- Top–bottom quintile spread **+0.58%/mo (+6.9%/yr)**; monotonic ladder
  Q1 +0.15% → Q5 +0.73%
- Long-short Sharpe **0.50**; hit rate 59%
- Mean monthly rank IC **+0.037, t-stat +2.12** (statistically significant)
- Fundamental coverage (avg/mo over scored universe): Value 68%, Quality 71%,
  all-four-fields 33% — Value/Quality finally backed by real EDGAR history
  (back to 2009), not just momentum+low-vol as in the pre-EDGAR run
- **Deferred to Phase 2:** raise gross_margin point-in-time coverage (~45%
  avg/mo). Banks/financials legitimately lack a gross margin, but the rest is
  likely a `CostOfRevenue`/`CostOfGoodsSold` XBRL tag-fallback gap in
  `engine/edgar.py`. Also (carried from EDGAR build) quarter-ize cash-flow
  fields (OCF/capex/FCF/dividends) via YTD differencing — currently NaN, not a
  factor input.

---

## Phase 2 — Optimizer + full backtest

**Goal:** Complete walk-forward backtest of the full factor portfolio
(without covered calls) producing clean performance metrics.

**Build** (incremental order; implementation choices fixed in DECISIONS D23):

0. `engine/sectors.py` — sector map for the sector cap (no sector field exists
   in the store yet). Read each filer's SIC from the SEC submissions endpoint
   (`data.sec.gov/submissions/CIK{…}.json`), map SIC ranges → ~11 sector
   buckets, cache `data/ref/sectors.parquet`; no-CIK names → "Unknown".

1. `engine/covariance.py` — FF5 factor-model covariance (D23a). Download the
   daily 5-factor file from the Ken French library directly (`requests` + zip,
   cached — `pandas_datareader` is dead under pandas 3.0), OLS each asset's
   excess returns on the 5 factors over `covariance.estimation_window_days` →
   Σ = B·cov(F)·Bᵀ + diag(resid var). Test: recovers known betas on synthetic
   returns; Σ is PSD.

2. `engine/optimize.py` — convex max-Sharpe optimizer (D23b, no MIQP solver
   available):
   - μ from `rank(composite)/N × target_return_scale`. NB (D24): name count is
     set by the **max-single-name cap** (≈ budget/max_name), not by λ/scale —
     calibrated to 5% → ~19-20 names; λ/scale tilt is largely inert at that band
   - Pre-select top-K (≈50) by composite, solve `max μᵀw − λ·wᵀΣw` s.t.
     `sum(w)=base_equity_allocation`, `0≤w≤max_single_name_pct`,
     `sum(w[sector])≤max_sector_pct` (CLARABEL). **λ=0 by default (D25):** the
     composite already prices risk, so the mean-variance term double-counts low-vol
     and cost ~10%/yr — it's pure alpha-weighting LP, Σ kept for risk reporting
   - Min-position cleanup: drop the *single smallest* sub-floor name and re-solve,
     repeat (one-at-a-time, or it collapses to the concentrated corner — D24)
   - Infeasibility ladder: sector cap +5/+10/+15%, min position −$250/−$500,
     max 3 retries, then hold previous weights + alert
   - Cash normalized to invested capital; test in isolation on synthetic μ/Σ

3. `scripts/backtest.py` — walk-forward monthly backtest:
   - For each month, mid-2020 → present (⚠️ free-IEX floor, DECISIONS D21):
     factors.py → covariance.py → optimize.py
   - Transaction costs: tiered fixed bps by ADV (D23d) — retires the `spread`
     placeholder; T+1 settlement on equity sells
   - Track weights, returns, turnover, factor attribution
   - Output: annualized return, Sharpe, max drawdown, turnover, Calmar,
     factor-contribution breakdown

4. `notebooks/backtest_analysis.ipynb`:
   - Cumulative returns vs SPY benchmark
   - Rolling 12-month Sharpe
   - Factor score contribution over time
   - Turnover analysis

**Gate criteria:**
- Backtest Sharpe > 1.0 annualized after transaction costs
- Max drawdown within acceptable range (TBD — present to CADIEM)
- Turnover < 30% monthly (low-turnover factor strategy should be stable)
- No suspiciously perfect results — Sharpe > 3.0 suggests data leakage
- Factor contribution shows all four factors contributing positively
  over the backtest period (if one is consistently negative, investigate)
- `pytest tests/unit/test_optimize.py` passes
- `pytest tests/integration/test_backtest_pipeline.py` passes

**Gate status (2026-06-19) — ❌ not yet passed.** Harness built + tested; λ=0
calibration applied (D25). Walk-forward 2021-09 → 2026-05 (57 months), net of
tiered-bps costs: **+14.0%/yr (gross +15.3%), Sharpe 0.75, vol 20.3%, maxDD
−23.4%, Calmar 0.60, turnover 33%** — beats SPY (+11.1%). Sleeves: quality +7.9%,
value +15.3%, momentum +3.0%, **low-vol −1.5%**. Open items before the gate passes:
  1. **Turnover 33% > 30%** — add a hold/replace buffer (hysteresis) around the
     top-N cutoff to cut churn (and cost).
  2. **Net Sharpe 0.75 < 1.0** — the 1.0 hurdle likely belongs to the *combined*
     strategy; the covered-call overlay (Phase 2b) reshapes return/vol. Revisit the
     equity-only threshold vs. deferring the Sharpe gate to 2b.
  3. **Low-vol sleeve negative** this period — a factor-level question (keep at
     equal weight? it still diversifies), not a blocker for the others.


---

## Phase 2b — Full strategy backtest including covered calls

**Goal:** Backtest the complete strategy — factor portfolio + covered call
overlay — to validate the combined Sharpe before building live execution.

**Why a separate phase:** The covered call overlay meaningfully changes
the return distribution (caps upside, adds premium income, reduces vol).
Backtesting the equity portfolio alone in Phase 2 validates the factor
signal. Phase 2b validates the combined strategy that will actually run
in production.

**Build:**

1. `scripts/backtest_covered_calls.py` — extend the Phase 2 backtest to
   simulate the covered call overlay:
   - At each monthly rebalance: simulate writing delta-0.25 calls
     on all positions at 30-45 DTE
   - Use historical options data from Alpaca or approximated from
     Black-Scholes with historical IV for each stock
   - Simulate earnings close/rewrite events using historical earnings dates
   - Track premium income, assignment events, and combined P&L

2. Combined performance metrics:
   - Equity component return vs covered call premium income breakdown
   - Assignment rate (how often calls get exercised)
   - Combined Sharpe vs equity-only Sharpe
   - Combined max drawdown vs equity-only max drawdown

**Gate criteria:**
- Combined strategy Sharpe ≥ equity-only Sharpe from Phase 2
  (covered calls should not hurt on a risk-adjusted basis)
- Assignment rate < 30% (if calls are being exercised too often,
  delta target may need adjustment)
- Premium income > 0 in all market environments (validates vol premium)
- Combined max drawdown ≤ equity-only max drawdown
  (covered calls should reduce drawdown via premium cushion)

---

## Phase 3 — Execution engine + risk gate

**Goal:** System submits real orders to Alpaca paper account and manages
positions correctly.

**Build:**

1. `engine/risk.py` — pre-trade risk gate:
   - All checks defined in ARCHITECTURE.md
   - Test with synthetic order batches; unit tests must pass before
     connecting to live execution

2. `engine/execute.py` — order routing and submission:
   - Order sequencing (sells → buys)
   - Order type selection (market vs limit)
   - Minimum trade filter + pending_adjustments accumulation
   - Fill tracking loop writing to PostgreSQL
   - Idempotency check before submission

3. `engine/reconcile.py` — complete implementation:
   - Fetch Alpaca live positions
   - Compare to PostgreSQL snapshots
   - Update DB to match Alpaca
   - Block pipeline if Alpaca unreachable

4. `engine/monitor.py` — continuous monitoring:
   - Live NAV computation
   - L1 drift vs last target weights
   - Snapshot writes to PostgreSQL
   - DTE check on any held options (placeholder for Phase 4)

5. `scripts/run_eod.py` — APScheduler entry point:
   - All jobs wired in correct sequence
   - SIGTERM handler installed at startup
   - --env flag with .env.paper / .env.live loading
   - 5-second countdown on --env live

**Gate criteria:**
- First paper rebalance executes: equity orders submitted, fills
  confirmed in Alpaca paper dashboard
- Re-running run_eod.py for same date does not submit duplicate orders
- Risk gate correctly blocks a synthetic bad order batch in unit tests
- `pending_adjustments` table accumulates sub-threshold deltas correctly
- Monitor detects and logs drift between rebalances
- SIGTERM test: send SIGTERM mid-run, confirm clean shutdown and no
  orphaned orders
- Paper/live safeguard: missing --env flag refuses to start; --env live
  shows 5-second countdown
- `pytest tests/unit/` all pass
- `pytest tests/integration/test_execution.py` passes

---

## Phase 4 — Covered call overlay

**Goal:** System writes covered calls against held equity positions,
rolls them correctly, and handles assignment.

**Build:**

1. `engine/covered_calls.py` — full covered call module:
   - Monthly rebalance: close ALL existing calls first, rewrite after
     equity trades settle
   - Contract sizing: prefer mini contracts (10 shares), fall back to
     standard (100 shares); skip if position too small for one mini
   - Strike selection: delta = 0.25, 30-45 DTE
   - Earnings check: close calls before earnings, rewrite after
     (earnings dates from Alpaca corporate actions or yfinance)
   - Roll logic: DTE ≤ 21 → close + rewrite at delta 0.25, 30-45 DTE
   - Force-close: DTE = 0 → close at market open before all other orders
   - Assignment: conditional re-entry if composite score > threshold
   - All events written to `options_lifecycle` table

2. Wire `covered_calls.py` into the rebalance pipeline between
   `optimize.py` and `risk.py`

3. Wire daily DTE check into `monitor.py` and daily scheduler

4. Update `risk.py` to include covered call checks (no naked calls,
   valid expiry, covered position exists)

5. Update `execute.py` to handle options order types

**Gate criteria:**
- All existing calls correctly closed before first monthly rebalance
- New calls written fresh after equity trades settle
- Mini contracts used where available (verify in Alpaca paper dashboard)
- Earnings close confirmed: manually inject an upcoming earnings date
  and confirm call closes before it
- Roll executes correctly when DTE falls below 21
- Force-close fires correctly on expiry date
- Assignment: manually exercise a call, confirm conditional re-entry logic
- Premium income tracked separately in options_lifecycle table
- `pytest tests/unit/test_covered_calls.py` passes

---

## Phase 5 — Dashboard + alerting

**Goal:** Live local dashboard showing full portfolio state, working
email alerts on all trigger events.

**Build:**

1. `engine/alerts.py` — SMTP email sender:
   - Called by any module that needs to send an alert
   - Reads credentials from environment
   - All alert types from SPEC.md implemented

2. Wire alerts into all trigger points throughout the codebase

3. `dashboard/app.py` — FastAPI with endpoints:
   - `GET /api/state` — current portfolio snapshot
   - `GET /api/orders` — recent orders and fill status
   - `GET /api/factors` — latest factor scores for held positions
   - `GET /api/calls` — current covered call positions and P&L
   - `GET /api/alerts` — recent alert log

4. `dashboard/static/index.html` — single HTML file:
   - Portfolio positions table (ticker, weight, target, factor score)
   - NAV and daily P&L (equity component + options premium separately)
   - Covered calls table (ticker, strike, expiry, delta, premium)
   - Recent orders and fills
   - Alert feed
   - Polls `/api/state` every 30 seconds

5. `scripts/run_dashboard.py` — FastAPI startup script

**Gate criteria:**
- Dashboard loads at localhost:8000 and shows live paper portfolio
- Dashboard updates without page refresh
- All six alert types fire and deliver email in dry-run test
- Covered call P&L tracked separately from equity P&L

---

## Phase 6 — Crypto allocation

**Goal:** Small crypto allocation added to paper portfolio.

**Build:**

1. Determine crypto allocation size based on equity portfolio vol
   profile from Phase 2 backtest (target: 3-7% of NAV)

2. Extend `engine/ingest.py` to pull crypto OHLCV from Alpaca

3. Extend `engine/factors.py` to score crypto:
   - Momentum (7d, 30d return)
   - Volatility (30d realized vol)
   - Volume ratio (today vs 30d avg)
   - Simple equal-weighted composite

4. Extend `engine/optimize.py` to include crypto sub-portfolio
   with fixed capital budget (the allocation decided in step 1)

5. Extend `engine/execute.py` for crypto order types (market orders,
   immediate settlement)

6. Update dashboard to show crypto positions separately

**Gate criteria:**
- Crypto positions appear in paper portfolio after first rebalance
- Combined equity + crypto portfolio Sharpe does not degrade vs
  equity-only (adding crypto should help via diversification)
- Crypto allocation stays within configured budget

---

## Phase 7 — Go-live

**Goal:** Deploy real CADIEM capital to the live Alpaca account.

**Prerequisites (all must be met):**
- Phases 0-6 gate criteria met
- Paper trading has run for a meaningful period (TBD by CADIEM)
- Go-live gate criteria defined and met (TBD by CADIEM)
- Database backup configured and tested
- All alert types confirmed working on live paper events
- Manual review of at least 5 complete rebalance cycles
- CADIEM sign-off received

**Build:**
1. Switch `ALPACA_BASE_URL` to live endpoint in `.env.live`
2. Set `nav` in settings.yaml to actual funded account balance
3. Confirm risk gate limits are appropriate for real capital
4. Run first live rebalance interactively (call run_eod.py manually,
   monitor each stage before proceeding)
5. After first live rebalance confirms correctly, enable automated scheduler

**No code changes required to go live** — only `ALPACA_BASE_URL`
changes between paper and live. This is by design.

---

## Phase 8 — Leverage (TBD)

**Goal:** Add 1.5x margin leverage after live strategy is validated.

**Prerequisites:**
- Minimum 6 months live track record
- Sharpe in live trading consistent with backtest
- CADIEM approval for levered operation
- Margin account enabled on Alpaca live account

**Build:**
1. Change optimizer weight sum constraint from 1.0 to 1.5
2. Add margin utilization check to risk gate
3. Add margin cost (interest rate × borrowed amount) to P&L attribution
4. Update dashboard to show gross vs net exposure

---

## What not to build ahead of schedule

- Do not build the execution engine before Phase 2 backtest gate is met
- Do not add covered calls before Phase 3 execution engine is stable
- Do not add crypto before equities + covered calls are running cleanly
- Do not go live before paper trading period meets CADIEM's criteria
- Do not add leverage before a live track record is established
- Do not add ML models — if signal quality needs improving, add
  Bloomberg data and earnings revision signals first