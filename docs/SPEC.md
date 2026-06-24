# SPEC.md — sharpe-engine

What the system does, end to end. No architecture, no tech choices.

---

## Purpose

sharpe-engine maintains a systematic factor equity portfolio with a covered
call income overlay on behalf of the firm's proprietary book.
It scores stocks on four well-documented factors, constructs a max-Sharpe
portfolio subject to hard constraints, writes covered calls against held
positions to harvest the volatility risk premium, and rebalances monthly
or when the portfolio drifts significantly from target.

The mandate is capital preservation and generation of uncorrelated
USD-denominated returns that diversify the firm's core brokerage business.

---

## Universe

All US equities and ETFs listed on Alpaca, filtered on each rebalance date:
- Price > $5 (exclude penny stocks)
- 20-day average daily volume > $1M (ensure executable liquidity)
- Not halted or delisted as of the rebalance date

Filtering uses only data available on that date — no look-ahead into which
symbols survive to today. Stocks that delist or go to zero naturally
disappear from the universe when their price and volume data stops.

A small crypto allocation will be added after the equity strategy is
validated. Size TBD.

---

## Factor scoring

Every stock in the universe receives a composite factor score on each
rebalance date. The score is a simple equal-weighted average of four
normalized sub-scores:

**Quality** — measures profitability and balance sheet health:
- Return on equity (ROE)
- Gross profit margin
- Source: yfinance quarterly financials
- Update frequency: quarterly, on earnings release

**Value** — measures cheapness relative to fundamentals:
- Price-to-earnings ratio (P/E) — inverted so lower P/E = higher score
- Price-to-book ratio (P/B) — inverted
- Source: yfinance quarterly financials
- Update frequency: quarterly, on earnings release

**Momentum** — measures price trend:
- 12-month return minus most recent 1-month return (12-1 momentum)
  to avoid short-term reversal contaminating the signal
- Source: Alpaca daily price data
- Update frequency: daily

**Low volatility** — measures return stability:
- 252-day realized volatility — inverted so lower vol = higher score
- Source: Alpaca daily price data
- Update frequency: daily

Each sub-score is cross-sectionally z-scored within the universe on each
rebalance date before averaging. The composite score is then used to rank
all stocks — higher composite score = more attractive.

Fundamental factors (quality, value) are stale between earnings releases.
This is accepted — these factors are low-turnover by nature and do not
require daily updates to be effective.

---

## Portfolio construction

Given the ranked factor scores, the optimizer selects the final portfolio:

1. Take the top N stocks by composite factor score (N determined by
   the minimum position size constraint given current NAV)
2. Run a max-Sharpe optimization using the Fama-French 5 factor model
   covariance matrix
3. Apply hard constraints:
   - Long-only (w ≥ 0)
   - Weights sum to base equity allocation target
   - No single position > configurable single-name cap
   - No single GICS sector > configurable sector cap
   - No position below $4,000 minimum notional size

On optimizer infeasibility: relax sector cap by +5% per retry, then
minimum position size by $250 per retry, maximum 3 retries. If still
infeasible after 3 retries, hold previous weights and alert.

---

## Covered call overlay

After each rebalance, the covered call module writes calls against
equity positions that meet the minimum holding period (configurable,
default 10 days — avoid writing calls on positions about to be exited).

**Contract sizing:** Standard contracts cover 100 shares. Mini options
contracts (10 shares) are used where available on Alpaca. Positions below
the minimum notional for even one mini contract are excluded from the
covered call overlay for that cycle.

**Strike selection:** delta = 0.25 targeting. The nearest listed contract
with delta closest to 0.25 and DTE between 30 and 45 is selected.
This targets a consistent ~25% probability of assignment regardless of
each stock's individual volatility.

**Earnings policy:** the system checks earnings announcement dates for all
held positions. Any call on a stock with an earnings announcement within
the call's remaining DTE window is closed before the announcement date
and rewritten after the announcement. This is systematic with no
discretionary override.

**Roll policy:** when a held call's DTE falls below 21, it is closed and
a new call is written at the same delta target with 30-45 DTE.

**Expiry:** any call reaching DTE = 0 is force-closed at market open
before any other orders run that day.

**Assignment:** when a stock position is called away:
- Compute the stock's current composite factor score
- If score is still above the entry threshold: re-enter the position
  at market after the call is settled
- If score has fallen below threshold: let the cash sit and deploy
  it at the next monthly rebalance via the optimizer

**Premium income:** collected upfront when the call is written. Tracked
in PostgreSQL for P&L attribution (overlay P&L shown separately from
equity P&L in dashboard).

---

## Rebalancing

**Primary trigger — monthly:**
On the first trading day of each calendar month:
1. Close all existing covered calls
2. Run full rebalance pipeline: ingest → factor scoring → optimizer
   → risk gate → equity execution
3. Rewrite covered calls fresh against new positions

**Secondary trigger — L1 drift:**
Daily monitoring computes the L1 distance between current weights and
last target weights. If drift exceeds the configured threshold (TBD,
calibrate in backtest), trigger an out-of-cycle rebalance. This catches
large market dislocations without waiting for the monthly cycle.

The daily monitoring job runs continuously. The monthly rebalance job
runs on the first trading day of each month. Both use the same pipeline.

---

## Execution

Orders are submitted to Alpaca after passing the pre-trade risk gate.

- Sells always execute before buys (cash-first sequencing)
- Orders below minimum trade size are skipped and accumulated into
  the next rebalance cycle via pending_adjustments
- Equity orders: limit orders for mid-cap and below, market orders
  for large liquid names; all use adjustment='all' pricing
- Covered call orders: limit orders at mid price
- All equity price data uses adjustment='all' (split + dividend adjusted)
- Settlement: T+1 for equities, immediate for crypto

---

## Risk gate

Pre-trade check before any order batch is submitted. Blocks the batch if:
- Any position would exceed the single-name cap
- Any sector would exceed the sector cap
- Gross notional exceeds NAV (no unintended leverage)
- Any symbol is not in the approved tradeable universe
- Any covered call would result in an uncovered position

If blocked: log reason, send alert, do not submit any orders for that cycle.

---

## Data sources

| Data | Source | Frequency |
|---|---|---|
| Equity OHLCV | Alpaca Data API | Daily |
| Options chain | Alpaca Data API | Daily (for covered calls) |
| P/E, P/B, ROE, gross margin | yfinance (current) + derived historical from quarterly financials | Quarterly |
| FF5 factor returns | pandas-datareader (Ken French) | Daily (3-5 day lag) |
| Fundamentals enrichment | Bloomberg Terminal (optional) | Quarterly |

Missing data on any asset for a given cycle: skip that asset, rebalance
the rest. No full pipeline halt for individual asset data gaps.

---

## Monitoring and alerting

**Dashboard (always-on, read-only):**
- Current positions and weights vs targets
- NAV and daily P&L (total, equity component, covered call premium)
- Factor scores for current holdings
- Recent orders and fill status
- Drift level vs threshold
- Risk gate status

**Email alerts on:**
- Monthly rebalance triggered and completed
- L1 drift threshold rebalance triggered
- Risk gate block (with reason)
- Any order fill failure or partial fill
- Assignment of a covered call position
- Data staleness warning
- System error or crash

---

## Paper trading and go-live

The system runs on Alpaca paper trading until go-live criteria are met.
Go-live criteria are TBD and will be defined by the firm before real
capital is deployed.

---

## What this system does NOT do

- No intraday trading
- No short selling
- No direct leverage (margin borrowing deferred to post-live validation)
- No high-frequency execution
- No standalone options positions (calls are only written against held equity)
- No earnings prediction or ML return forecasting
- No tax-loss harvesting