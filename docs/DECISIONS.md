# DECISIONS.md — sharpe-engine

Why key decisions were made. Read before changing anything structural.

---

## D1 — Factor investing over ML return prediction

**Decision:** The equity scoring model uses explicit, formula-based factor
scores (quality, value, momentum, low-vol) rather than a machine learning
model predicting forward returns.

**Why:** Cross-sectional return prediction from price features is the most
crowded, most efficiently arbitraged signal space in finance. A LightGBM
model trained on standard momentum and vol features in 2026 competes with
Renaissance, Two Sigma, and thousands of quant funds who have been trading
these signals for decades. The half-life of ML price signals has compressed
dramatically as more capital chases them.

Factor premia are different — they are persistent risk premia with 50 years
of academic evidence across dozens of markets and time periods. They work
because they are compensation for systematic risk, not because they identify
mispricings. This makes them more durable and more defensible to management.

**Why this fits CADIEM's mandate:** The goal is capital preservation and
uncorrelated returns, not alpha generation. A clean factor portfolio with
an options income overlay is honest about what it is, easy to explain to
an investment committee, and has a credible academic track record. A black-
box ML model is harder to defend when it goes through a bad stretch.

---

## D2 — Equal factor weights to start, adjust after backtesting

**Decision:** Quality, value, momentum, and low-vol are weighted equally
(25% each) in the composite score. Weights will be adjusted after seeing
backtest results.

**Why equal weights:** Equal weighting is the most defensible starting
point when you don't have a strong prior on which factors will dominate
in the backtest period. Research-based weights (e.g. quality 35%, low-vol
30%) embed assumptions about future factor premia that may not hold.
Equal weighting also avoids overfitting the weight selection to historical
data before the backtest is run.

**What was considered:** Research-based weights from academic Sharpe
rankings (Novy-Marx quality, Frazzini-Pedersen low-vol, Jegadeesh-Titman
momentum, Fama-French value). These would tilt toward quality and low-vol
given the capital preservation mandate. Revisit after backtest if equal
weighting produces a clearly suboptimal factor mix.

---

## D3 — Covered call overlay as primary income source

**Decision:** The system writes covered calls (delta = 0.25, 30-45 DTE)
against held equity positions to harvest the volatility risk premium.

**Why covered calls over cash-secured puts:** Puts require holding large
cash reserves as collateral — dead weight in a prop book. Covered calls
generate income on capital that is already deployed in the factor portfolio.
The capped upside is acceptable given the capital preservation mandate —
CADIEM doesn't need 50% years, they need consistent income.

**Why not the wheel strategy (puts + calls):** Additional operational
complexity. The put side creates cash drag. For a first version, covered
calls alone are cleaner and more controllable. Puts can be added later.

**Academic basis:** The volatility risk premium — implied vol persistently
overstating realized vol — is one of the most robust anomalies in options
markets. The CBOE BuyWrite Index (BXM) has historically delivered similar
returns to the S&P 500 with ~30% lower volatility. Premium is typically
1-3% annualized on a risk-adjusted basis above buy-and-hold. It is a risk
premium (compensation for bearing vol risk), not pure alpha, and it
underperforms in strong bull markets.

---

## D4 — Delta = 0.25 targeting for strike selection

**Decision:** Covered calls are written at the strike with delta closest
to 0.25, regardless of the stock or its factor classification.

**Why delta targeting over fixed OTM percentage:** A fixed 7% OTM strike
means very different things for a 15% vol stock vs a 40% vol stock. Delta
targeting gives a consistent probability of assignment (~25%) across all
names and adapts naturally to each stock's volatility. In high-IV
environments the call is further OTM in dollar terms; in low-IV
environments it is closer, maximizing premium relative to assignment risk.

**Why 0.25:** Balances premium income against upside capture. A delta of
0.25 means roughly 25% probability of assignment — low enough to let most
winners run, high enough to collect meaningful premium. Industry standard
for systematic covered call programs.

**What was considered:** Factor-specific delta targeting (0.15 for
momentum, 0.30 for quality/low-vol, 0.20 for value). This is the natural
upgrade once the base strategy is validated — momentum names should have
wider strikes to let winners run. Uniform delta is the right starting point.

---

## D5 — No regime filter; quality and low-vol factors handle defensiveness

**Decision:** No regime filter. The system maintains a stable base equity
allocation (95%) at all times. Defensive rotation in risk-off environments
is handled naturally by the factor model — quality and low-vol stocks
outperform in downturns, so the optimizer naturally tilts defensive when
these factors score highly.

**Why drop the regime filter:** The factor model already adapts defensively.
High-quality, low-vol stocks by definition hold up better in market stress.
Adding a regime filter on top creates double-counting — the portfolio would
reduce equity allocation AND rotate defensively at the same time, making
the risk-off response too aggressive. Simplicity is also a virtue: the
fewer moving parts, the easier the system is to audit and explain to CADIEM.

**What was considered:** Composite z-score regime filter using VIX, yield
curve, and HY credit spread. Rejected because: (1) factor model subsumes
this defensiveness naturally, (2) macro timing is notoriously unreliable
and most regime filters destroy rather than add value over full cycles,
(3) removes FRED as a dependency, simplifying the data pipeline.

---

## D6 — Monthly rebalance primary, L1 drift threshold secondary

**Decision:** The primary rebalance trigger is the first trading day of
each calendar month. A secondary drift trigger fires an out-of-cycle
rebalance if L1 weight drift exceeds a threshold.

**Why monthly over daily:** Factor scores change slowly — fundamentals
update quarterly, momentum is stable over weeks. Daily rebalancing
generates unnecessary turnover and transaction costs without improving
the portfolio meaningfully. Monthly rebalancing is standard for factor
portfolios and consistent with the low-turnover nature of the strategy.

**Why keep a drift trigger:** Large market dislocations (e.g. a 15%
market crash) can rapidly move the portfolio far from its factor targets
as some sectors fall more than others. A drift trigger catches these
events without waiting up to a month for the next scheduled rebalance.

**Threshold:** TBD — calibrate during backtesting. Starting suggestion
8% L1 drift. Factor portfolios are less sensitive to drift than ML
signal portfolios because the signals are slow-moving.

---

## D7 — yfinance for fundamentals, FMP/Bloomberg as upgrade path

**Decision:** yfinance provides fundamental data (P/E, P/B, ROE, gross
margins) for both backtesting and live trading. No API key required.
Upgrade to FMP paid tier or Bloomberg API when available.

**Why yfinance over FMP free tier:**
No API key, no rate limit concerns for the backfill, no signup required.
For getting the system built and validated quickly, yfinance removes all
data source friction. FMP and Bloomberg are better long-term solutions
but the switching cost is low — only engine/ingest.py needs to change.

**Why yfinance over Bloomberg manual export:**
The Bloomberg export requires manual batching of 2,000-3,000 tickers
across multiple Excel sessions — a multi-hour process before a single
line of code can be written. yfinance provides the same fields
programmatically and lets Phase 0 start immediately.

**Known limitation — look-ahead bias:**
yfinance serves current restated fundamentals rather than as-reported
point-in-time data. For backtesting quality and value factors in 2019,
the P/E and ROE figures may reflect subsequent restatements. This makes
the backtest quality and value signals slightly optimistic. Mentally
discount the backtest Sharpe by ~5-10% and document this limitation
clearly in any CADIEM presentation.

**Known limitation — reliability:**
yfinance is an unofficial Yahoo Finance scraper and can break when Yahoo
changes their backend. For quarterly fundamental updates in a live system,
brief outages are acceptable — if yfinance is down for a few days, the
cached quarterly fundamentals from the last pull remain valid.

**Upgrade path:**
When Bloomberg API access becomes available or FMP paid tier is justified
by live system performance, swap yfinance for the new source in
engine/ingest.py only. All downstream modules are agnostic to the
fundamental data source.

---

## D8 — Fama-French 5 factor model for covariance

**Decision:** Portfolio covariance estimated via Fama-French 5 factor
model (Σ = BFBᵀ + D). Factor returns from Ken French library via
pandas-datareader. Accept 3-5 day publication lag.

**Why factor model:** The full Alpaca universe (8,000+ symbols) makes
sample covariance singular — more parameters than observations. Factor
models scale to any universe size by decomposing covariance into a
small number of common factors plus idiosyncratic noise.

**Why FF5 specifically:** The five factors (market, size, value,
profitability, investment) align directly with the factor scores being
computed — using the same factor structure for both scoring and
covariance estimation is consistent and interpretable.

**⚠️ Phase 2 prerequisite (2026-06-18):** `pandas_datareader` currently fails
at import in the venv (`TypeError: deprecate_kwarg()` — incompatible with the
installed pandas). It is the only thing that fetches the Ken French FF5 series,
so Phase 2 cannot estimate covariance until this is resolved. Two paths: pin a
compatible `pandas`/`pandas-datareader` pair, or drop the dependency and fetch
the FF5 CSV directly from the Ken French data library. Decide at the start of
Phase 2 — does not affect Phase 0 or Phase 1.

---

## D9 — Conditional re-entry after assignment

**Decision:** When a covered call is exercised, re-enter the position
only if the stock's current composite factor score is above the
configured re-entry threshold.

**Why conditional over automatic:** Automatic re-entry can mean buying
back a stock that just ran 8% above your strike — paying a high price
to re-enter something you just sold cheaper. The factor score is the
objective measure of whether the position is still worth holding.
If momentum has peaked and the stock's composite score has fallen,
letting the cash deploy naturally at the next monthly rebalance is
the right call.

---

## D10 — No ML, no model versioning, no training pipeline

**Decision:** There are no machine learning models in this system.
Factor scores are computed from explicit formulas. No training,
no serialized model files, no IC validation, no signal decay analysis.

**Why this simplifies the system significantly:** Removes the entire
ML infrastructure: no training scripts, no model versioning, no
rollback logic, no held-out test sets, no backfill_meta, no
meta-model cold start problem. The system is simpler, more auditable,
and easier to explain to CADIEM management.

**What replaces model quality monitoring:** Factor score stability
monitoring in the dashboard (are factor scores behaving reasonably?),
backtest performance attribution by factor (which factors contributed
in which periods?), and live P&L attribution (equity vs covered call
premium component).

---

## D11 — Leverage deferred to post-live validation

**Decision:** The system launches unlevered (weights sum to ≤ 1.0).
1.5x margin leverage via Alpaca is a planned Phase 8 addition after
the live strategy has a validated track record.

**Why defer:** Paper trading validates signal quality and execution.
Live unlevered validates real-world performance. Adding leverage to
an unvalidated strategy amplifies both returns and errors. Presenting
an unlevered system to CADIEM first also builds trust — it's easier
to add leverage to a proven strategy than to explain leverage losses
on an unproven one.

**Implementation when added:** Alpaca margin account, optimizer
weight sum constraint changed from 1.0 to 1.5, margin utilization
check added to risk gate. No other changes required.

---

## D12 — Crypto as small allocation, size TBD

**Decision:** A small crypto allocation is included for diversification
but the exact size is deferred until the equity strategy is validated.

**Why include crypto:** Low correlation to equities provides genuine
portfolio diversification. At small allocations the tail risk is
bounded and the diversification benefit is real.

**Why defer sizing:** Crypto's volatility profile means even a 5%
allocation can dominate portfolio drawdown in a severe crypto crash
(-80% on BTC = -4% on total NAV at 5% allocation). The right size
depends on the equity portfolio's vol profile, which becomes clear
after backtesting. A reasonable starting range is 3-7%.

---

## D13 — Alpaca as source of truth, reconcile at every startup

**Decision:** PostgreSQL is reconciled against Alpaca live positions
at every pipeline startup. Alpaca is always source of truth.
Pipeline is blocked if Alpaca is unreachable.

---

## D14 — Monthly rebalance, daily monitoring, 2-year raw data retention

**Decision:** Raw Parquet equity files kept for 2 years (rolling).
Fundamental Parquet files kept forever. Disk usage monitored.

**Why 2 years for raw prices:** The factor computation needs
252 trading days (1 year) of price history for momentum and vol.
2 years provides a generous buffer. Raw prices can be re-pulled
from Alpaca's historical API if needed — they are not the primary
asset. Fundamental data is kept forever as it is harder to
reconstruct and changes meaningfully over time.

---

## Open decisions

| Decision | What's needed to resolve it |
|---|---|
| L1 drift threshold | Backtest calibration — start at 8% |
| Sector cap | Backtest — start at 30% |
| Factor weight adjustment | Run backtest, check factor contribution |
| Crypto allocation size | After equity backtest — likely 3-7% |
| FF5 covariance source | Phase 2 — `pandas_datareader` import is broken (see D8); pin compatible versions or fetch Ken French CSV directly |
| Go-live gate criteria | User-defined before real capital deployed |
| DB backup | Set up before go-live |
| Secret rotation | Set up before go-live |
| Leverage details | After live track record established |
| Covered call re-entry threshold | Calibrate in backtest |

---

## D15 — Close all covered calls at monthly rebalance, rewrite fresh

**Decision:** At each monthly rebalance, all existing covered calls are
closed before equity trades execute. After new equity positions are set,
calls are rewritten fresh against the new portfolio.

**Why close all rather than selectively:** Selectively keeping calls on
positions that are being held requires tracking which calls are still
appropriate for the new position sizes and weights. Closing everything
and rewriting fresh is simpler, always correct, and ensures calls are
always aligned to current positions. The additional transaction cost is
minimal at monthly frequency.

---

## D16 — Close covered calls before earnings, rewrite after

**Decision:** Any covered call on a stock with an earnings announcement
within the call's remaining DTE window is closed before the announcement
and rewritten after. Systematic, no discretion.

**Why:** Earnings announcements cause gap moves that make covered calls
unpredictable. A large upside gap means the call is exercised far below
the new price — the upside is captured by the counterparty, not the
portfolio. A large downside gap means the call provided almost no
protection and you still hold a losing position. Closing before earnings
removes this binary risk cleanly.

**How earnings dates are sourced:** Alpaca provides earnings calendar
data via their news/corporate actions API. Alternatively, yfinance
provides earnings dates (`ticker.calendar` / `ticker.earnings_dates`).

---

## D17 — Mini options contracts where available, standard otherwise

**Decision:** The covered call module prefers mini options contracts
(10 shares per contract) where available on Alpaca. Standard contracts
(100 shares) are used where mini contracts are not listed.

**Why:** At $100k NAV with 20-30 names, average position size is $3,300-
$5,000. On a $200 stock that is 16-25 shares. One standard contract
requires 100 shares ($20,000 notional) — most positions can't support
even one contract. Mini contracts require only 10 shares ($2,000
notional), making the covered call overlay viable across the full
portfolio rather than only on the largest positions.

**Minimum position for mini contract:** $2,000 notional at minimum
(10 shares × $200 stock). The $4,000 minimum position size ensures
most positions can support at least 2 mini contracts.

---

## D18 — Minimum position size $4,000

**Decision:** Increased from $2,000 to $4,000 to ensure covered call
eligibility. A $4,000 position supports at least 2 mini contracts on
stocks up to $200/share, giving meaningful options overlay coverage
across the portfolio.

**Tradeoff:** Fewer names in the portfolio at $100k NAV. At $4,000
minimum, maximum holdings drops from 50 to 25. This is acceptable and
consistent with the target of 20-30 names.

---

## D19 — Price factors recomputed daily, fundamentals from last quarterly pull

**Decision:** On non-rebalance days, momentum and realized vol are
recomputed fresh from that day's prices. Quality and value scores use
the most recent quarterly fundamental pull and are not updated intraday
or daily.

**Why:** Momentum and vol are pure price derivatives — trivial to
recompute daily and meaningfully updated by each day's price move.
Fundamentals (P/E, ROE, gross margin) are reported quarterly and don't
change between earnings releases. Pulling fundamentals daily when they
haven't changed wastes API calls and adds complexity.

**For drift-triggered rebalances:** The optimizer uses fresh momentum/vol
scores from today's prices combined with the most recent quarterly
fundamental scores. This is the most current information available and
is appropriate — a mid-month drift rebalance is responding to price
moves, so fresh price-derived factors are what matter most.

---

## D20 — μ scaling TBD, calibrate in Phase 2

**Decision:** The mapping from composite factor scores to optimizer μ
(expected returns) is not hardcoded. Rank-normalized scores will be
scaled to annualized return space (rank/N × target_return_scale) and
target_return_scale + λ calibrated together during Phase 2 to produce
the desired 20-30 name portfolio with reasonable weight distribution.

**Why defer:** The right scaling depends on the covariance matrix
magnitude, which is empirical. Setting it upfront risks either a
highly concentrated portfolio (μ too large relative to Σ) or a near
equal-weight portfolio (μ too small). Phase 2 calibration with real
historical data is the correct approach.

---

## D21 — Free IEX feed sets the backfill history floor (~mid-2020); SIP is the upgrade path

**Decision:** The one-time backfill starts at **2020-07-27**
(`settings.ingest.backfill_start`), not 2016. The free Alpaca **IEX** feed
only serves daily history back to roughly that date and reports
IEX-exchange volume only (a fraction of the consolidated tape). We proceed
with ~6 years of free history for now; the paid **SIP** feed (~$99/mo) is
the upgrade path for full 2016+ history and consolidated volume — and only
`settings.ingest.backfill_start` plus the feed setting change, no code.

**Why proceed on IEX:** ~6 years spans a full cycle (2020 COVID crash, 2022
bear, 2023–25 recovery) — enough to validate factor signals in Phases 1–2.
Paying for SIP before the strategy is validated is premature.

**Consequence for backtests:** the equity backtest window is bounded to
mid-2020→present until SIP is enabled. Partial IEX volume also means
`adv_20d` understates true liquidity, so the universe filter is
conservative (fewer names pass) — acceptable, and re-derived once on SIP.

**Fundamentals scope (related):** the fundamentals backfill is scoped to
the **liquid universe** (`--fundamentals-universe liquid`, ~2,700 names
passing ADV>$1M / price>$5) rather than the full ~13k tradable universe.
The illiquid/ETF tail is never traded, so pulling its fundamentals is ~11h
of yfinance calls for nothing (~2h scoped instead). Survivorship caveat:
the scope is *today's* liquid set; revisit for point-in-time coverage if a
Phase 2 backtest needs it.

---

## D22 — SEC EDGAR for deep point-in-time fundamental history (resolves D7's upgrade path)

**Decision:** Source historical fundamentals from the **SEC EDGAR**
`companyfacts` XBRL API (`data.sec.gov`), not yfinance, for the backfill.
yfinance stays as a convenient *current-quarter* fallback for daily ingest,
but the historical store is rebuilt from EDGAR. This resolves the D7
"FMP/Bloomberg as upgrade path" question in favor of EDGAR — **free** and
genuinely **point-in-time** (each XBRL fact carries its `filed` date), with
history back to ~2009.

**Why this surfaced now (Phase 1):** the factor sanity backtest
(`scripts/backtest_factors.py`) revealed that yfinance quarterly statements
only return ~8 quarters — our fundamentals store spanned just **2024-Q3 →
2026-Q2**. So **40 of 59 backtest months had zero fundamentals**, and the
"4-factor" composite was silently momentum + low-vol over 2021–2024. Value
and Quality cannot be validated on a ~2-year window; a credible Phase 2
Sharpe gate is impossible without deeper history.

**Why EDGAR over FMP (paid) or yfinance (as-is):** FMP (~$22-50/mo) is fast
to integrate but a recurring cost on a not-yet-validated strategy; yfinance
as-is leaves Value/Quality untestable. EDGAR is free, authoritative, and
point-in-time via filing dates — the right long-term foundation for a real
book. Cost is engineering, not dollars.

**Consequence / scope:** a data-foundation build (ticker→CIK mapping via
`company_tickers.json`; pull `companyfacts`; map US-GAAP tags →
revenue / gross profit / net income / stockholders' equity → derive
ROE, gross_margin, and — with the price panel — point-in-time P/E and P/B;
write the existing `data/raw/fundamentals/YYYY-QN.parquet` schema so
`engine/factors.py` is unchanged). Honors SEC fair-access limits
(declared User-Agent, ≤10 req/s). Still bounded *below* by the D21 price
floor (~mid-2020) for P/E and P/B that need prices, but ROE and gross_margin
go back as far as EDGAR — and the 2020→present 4-factor backtest becomes
credible. Validated **2026-06-18**, Phase 1.

**Scope confirmed with Diego (2026-06-18):**
- **Point-in-time key = the real SEC `filed` date** (not quarter-end + heuristic
  lag). `report_date` stores the filed date and `report_lag_days` → 0. yfinance
  ADR-fallback rows store period-end + 45d as their availability proxy, so
  `report_date` uniformly means "date the data was public".
- **EDGAR is primary; yfinance is demoted to a thin fallback for foreign ADRs**
  only (20-F / IFRS filers absent from EDGAR's us-gaap). End-state stack:
  **Alpaca** = market data + options + execution · **EDGAR** = all fundamentals +
  (Phase 4) earnings dates · **Ken French** = FF5 factor returns.
- **Pull a richer tag superset** in the same `companyfacts` call (assets, debt,
  operating/free cash flow, shares, dividends, plus the raw NI/revenue/equity
  used for the 4 metrics) so Phase 2+ can build stronger quality/value signals
  without re-pulling. `factors.py` reads only its four fields; extra columns ride
  along in the parquet.
- **Switch both backfill *and* the daily refresh to EDGAR** in this build
  (yfinance's daily role ends now); the daily path refreshes only when a new
  filing appears rather than re-fetching every day.
- **Earnings dates** move to EDGAR (8-K item 2.02 / 10-Q filed date) in Phase 4.

---

## D23 — Phase 2 optimizer/backtest implementation choices

Four implementation decisions made at the start of Phase 2 (2026-06-19,
confirmed with Diego). Each adapts the ARCHITECTURE plan to what actually
holds today; none changes the strategy, only how it is computed.

**(a) Covariance — keep FF5 factor model, but fetch Ken French data directly.**
ARCHITECTURE specifies FF5 covariance via `pandas_datareader`, but pdr
import-crashes under pandas 3.0 (`deprecate_kwarg` signature change). Rather
than pin pandas backward or swap to a plain sample covariance, `engine/
covariance.py` downloads the daily 5-factor file from the Dartmouth/Ken French
data library directly (`requests` + zip, cached to disk), then builds
Σ = B·cov(F)·Bᵀ + diag(resid var) by OLS-regressing each asset's excess
returns on the five factors over `covariance.estimation_window_days`. This
preserves the documented, well-conditioned factor-model Σ (PSD for any N) and
drops the broken dependency. *Rejected:* Ledoit-Wolf sample covariance —
self-contained but departs from the FF5 design and is worse-conditioned on a
60-day window.

**(b) Min-position constraint — pre-select top-K + convex QP + cleanup, not MIQP.**
The `w ≥ min_position or w = 0` rule is semi-continuous (non-convex), and the
installed cvxpy solvers (CLARABEL/OSQP/SCS/HIGHS) include no MIQP backend. So
`engine/optimize.py` (1) pre-selects the top-K names by composite score
(K ≈ 50), (2) solves the convex QP `max μᵀw − λ·wᵀΣw` s.t. budget =
base_equity_allocation, 0 ≤ w ≤ max_single_name_pct, sector caps, (3) zeroes
any name below `min_position_usd/NAV`, renormalizes, and re-solves on the
survivors, then (4) applies the D-existing infeasibility-relaxation ladder.
The 95% budget / 4% min ratio caps the book at ~23 names organically, hitting
the 20–30 target. *Rejected:* true MIQP via a new solver (e.g. pyscipopt) —
faithful to the spec but adds a dependency and is slower in the walk-forward
loop; revisit only if the cleanup heuristic misbehaves.

**(c) Sector data for the 30% sector cap — EDGAR SIC → sector buckets.**
No sector field exists in the store. `engine/sectors.py` reads each filer's
SIC code from the SEC submissions endpoint (`data.sec.gov/submissions/
CIK{…}.json` — already within our EDGAR access), maps SIC ranges to ~11
sector buckets, and caches `data/ref/sectors.parquet`. Self-contained, stable,
point-in-time-safe; names without a CIK (ETFs/ADRs) bucket to "Unknown".
*Rejected:* yfinance `.info['sector']` (cleaner GICS labels but flaky,
rate-limited, not point-in-time) and deferring the cap (risks a concentrated
first backtest).

**(d) Transaction costs — tiered fixed bps by ADV.**
The stored `spread` column is a high-low *placeholder*, not bid/ask. The
backtest charges a half-spread in basis points tiered by liquidity
(large-cap / mid / small via `execution.large_cap_adv_threshold` and a new
tiered-bps setting), using the ADV already computed. Transparent and easy to
stress. *Rejected:* Corwin-Schultz high-low spread estimator — noisier, can go
negative, needs clamping; the `spread` placeholder is retired either way.

---

## D24 — Portfolio concentration: 5% max single name → ~19-20 names

**Decision:** lower ``portfolio.max_single_name_pct`` from 0.10 to **0.05**,
producing a diversified ~19-20 name, near-equal-weight book. Confirmed with
Diego (2026-06-19) during Phase 2 optimizer calibration.

**Why this surfaced — the count lever is the max-name cap, not λ/scale.**
ARCHITECTURE assumed "calibrate ``target_return_scale`` and
``risk_aversion_lambda`` until the optimizer produces 20-30 names." Empirically
(live, 2026-05-01 cross-section) that is false: across a 2-D sweep of
scale ∈ [0.05, 0.30] and λ ∈ [0.5, 16] the name count barely moved — the book
sat at ~11 names because both the return-tilt optimum *and* the min-variance
optimum concentrate, and the **10% max-single-name cap** is what actually sets
the floor on name count (≈ ``budget / max_name``). Name count is therefore a
constraint choice, not a calibration outcome. (Also fixed a related defect: the
min-position cleanup must drop the *single smallest* sub-floor name and re-solve,
not the whole sub-floor batch at once, or it collapses to the concentrated corner
or to nothing.)

**Why 5% / diversified.** At 10% the optimizer pins the top 5 names at the cap —
~50% of the book in five names — which is hard to square with a
capital-preservation mandate. 5% over the existing 4% min-position floor yields
~19-20 names, matching the documented 20-30 target and capping single-name risk.
*Rejected:* keep 10% (~11 concentrated names, max conviction) and the ~6.7%
middle (~15 names).

**Consequence — effectively equal-weight at the cap.** The 4% floor is
load-bearing (it is the covered-call mini-contract threshold, so positions can't
go below it), so the [4%, 5%] band is tight: the optimizer funds the top ~19 by
composite at 5% each, subject to the 30% sector cap (which does bind, e.g.
Financials ~25%). λ, ``target_return_scale``, and the FF5 covariance tilt are
largely inert at this concentration — the factor *selection* and sector caps do
the work. This is a deliberate, robust construction (equal-weight factor books
travel well out-of-sample). The covariance machinery (D23a) still serves the
backtest's risk attribution and would tilt the book if the band is ever widened.

---

## D25 — Alpha-driven weighting: risk-aversion λ = 0 (supersedes the mean-variance framing)

**Decision:** set ``portfolio.risk_aversion_lambda`` to **0**, turning the
optimizer objective from mean-variance (``max μᵀw − λ·wᵀΣw``) into pure
constrained alpha-weighting (``max μᵀw`` s.t. the budget / max-name / sector /
min-position constraints). The FF5 covariance (D23a) is retained for **risk
reporting and the thin-history filter**, not as a return penalty. Confirmed with
Diego (2026-06-19) from the Phase 2 walk-forward backtest.

**Why — the risk term double-counts low-vol and destroys return.** The composite
score *already* prices risk: low-volatility is one of its four equal-weighted
sub-scores. Adding a mean-variance penalty λ·wᵀΣw on top tilts the book toward
low-vol names a *second* time. In the 2021-2026 walk-forward (led by higher-vol
value/momentum names) this cost ~10%/yr. Decomposition (same scores, same
universe, gross annualized return):

| optimizer config | return |
|---|---|
| λ=1, sector cap 30% (original) | +2.3% |
| λ≈0, sector cap 30% | +12.8% |
| λ=1, no sector cap | +2.4% |
| λ≈0, no sector cap | +12.2% |
| naive top-19 equal-weight by composite | +13.7% |

The risk term — not the sector caps — was the cause; removing it recovered the
signal. Full backtest at λ=0: net **+14.0%/yr** (gross +15.3%), Sharpe **0.75**
(was 0.15), vol 20.3%, beating SPY (+11.1%). The risk term shed a little vol but
killed far more return, so λ=0 is better on Sharpe too.

**Why this is sound, not reckless.** λ=0 would normally concentrate in the single
highest-μ name — but the **box constraints already enforce diversification** (5%
max, 4% min, 30% sector), so the LP solution is "top ~19 by composite, equal-weight
at the cap, sector-capped." The alpha model selects; the constraints diversify; the
risk model monitors. This is a standard institutional split (alpha vs. risk model)
and matches D24's equal-weight reality. *Rejected:* drop low-vol from the composite
and keep λ>0 (let the risk term be the defensive layer) — cleaner in theory but
re-opens the whole signal, and low-vol was −1.5% this period anyway.

**Open follow-ups (not yet passing the gate):** turnover ~33%/mo (> 30% cap) needs
a hold/replace buffer (hysteresis); net Sharpe 0.75 < 1.0 — the 1.0 hurdle likely
belongs to the *combined* strategy once the covered-call overlay (Phase 2b) reshapes
the return/vol profile. Both tracked in BUILD_ORDER Phase 2.

---

## D26 — Turnover is signal: hysteresis built but disabled; turnover gate reframed

**Decision:** keep the turnover-reduction hysteresis (incumbent score bonus) in the
code as a **defaulted-off knob** (``rebalancing.incumbent_bonus = 0``) and **reframe
the BUILD_ORDER "turnover < 30%/mo" gate** rather than force the strategy under it.
Confirmed with Diego (2026-06-19).

**Mechanism built (D26a).** ``engine.optimize._apply_incumbent_bonus`` adds a
composite-score (z-unit) premium to currently-held names before ranking, so the
optimizer keeps an incumbent unless a challenger beats it by more than the bonus
(soft rank buffer; sector/min/box constraints still enforced). Chosen over an
index-style two-threshold add/drop rule for clean integration with the λ=0 LP.

**Why disabled — the turnover is alpha, not waste.** The ~33%/mo turnover is almost
entirely *membership churn* (~6 of ~19 names swap monthly; only ~2.5% is weight
drift), and that churn is the book staying on fresh signal (the composite re-ranks
monthly, momentum especially). A calibration sweep over the bonus:

| incumbent_bonus | turnover | net return | Sharpe |
|---|---|---|---|
| 0.00 (off) | 33% | +14.0% | 0.75 |
| 0.10 | 25% | +10.6% | 0.59 |
| 0.20 | 20% | +10.7% | 0.60 |
| 0.30 | 18% | +5.5% | 0.36 |

Every unit of turnover removed costs more return than it saves: the realized
transaction cost of 33% turnover is only **~1%/yr** (already inside the net +14%),
while suppressing it to satisfy the gate gives up **3-4%/yr of alpha** and drops
Sharpe below SPY. Paying 3-4 to save 1 is a bad trade.

**Gate reframe.** The "< 30%/mo" criterion was a generic "factor strategies should be
stable" guideline; it does not fit a momentum-inclusive book whose churn is productive
and whose cost is modest and already netted. The turnover gate becomes: *turnover cost
is modest and accounted for in net returns* — not a hard 30% cap. The knob remains for
future use (e.g. if costs rise on a larger book or a different cost regime).