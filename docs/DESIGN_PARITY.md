# Dashboard design parity — gap tracker

Tracks what the live dashboard (`dashboard/static/index.html` + `theme.css`, backed by
`dashboard/app.py` / `dashboard/data.py`) needs to reach parity with the **updated** SFI handoff
in `design_lang/` (`README.md` spec + the two `- Handoff.html` references + `SFI Dashboard.dc.html`
/ `support.js` source).

Status legend: **MISSING** (not built) · **PARTIAL** (built but incomplete / differs) ·
**OK** (matches the spec). "Needs" flags whether the work is frontend-only or also touches the
data layer.

The dashboard is already in the SFI dark style with the 4-tab + sub-tab shell, KPI strip, NAV
sparkline, nested sector donut, factor tilt, covered-call book, orders/alerts, track-record,
drawdown/vol charts, slippage + fees, and a system-health ribbon. The items below are the delta
the newer spec introduces.

---

## A · Top chrome & foundations

- [ ] **Ticker tape marquee** — MISSING. Spec row 3: infinite marquee (54s linear, pause on hover)
  of `symbol · price · sparkline · change% · CALL tag`. _Needs: frontend + per-symbol quote/day-change
  (positions already give symbols/prices; mini sparkline from intraday or recent history)._
- [ ] **Live ET clock** `HH:MM:SS ET` (mono) in the header right cluster — MISSING (currently shows a
  "last update" age). _Needs: frontend; can drive off `/api/health` market clock + local tick._
- [ ] **"streaming · live feed" pulse** label with `sfpulse` ring — PARTIAL (a live dot exists, no
  labelled streaming pulse). _Needs: frontend._
- [ ] **Brand lockup wordmark** — PARTIAL. Spec: 3×3 dot glyph + vertical "SFI" mono mark + divider +
  "Fund" wordmark. Current: glyph + `sharpe-engine` + tagline. Decide internal vs. external naming,
  then restyle. _Needs: frontend._
- [ ] **App background layer** — PARTIAL. Spec: `#070d18` base + faint 30px dot/line grid overlay +
  soft radial teal glow at top centre. Current `--bg #0b1322`, no grid/glow. _Needs: frontend (CSS)._
- [x] **Status ribbon** (Engine · Market · Next rebalance · Risk gate · Drift L1 · Alerts 24h) — OK.
  Consider making it always-visible/scrollable rather than behind the Details toggle.

## B · Overview

- [ ] **NAV hero card** — PARTIAL. Spec: eyebrow + NAV in `Space Grotesk 500 46px` tabular, day-P&L
  pill, inline sparkline, window-change stats on the right. Current: 20px NAV KPI card + a separate
  NAV panel below. Restructure into one hero. _Needs: frontend._
- [ ] **Flash-on-tick highlight** — MISSING. Brief tinted bg (green up / red down) on NAV & P&L cells
  when the value changes. _Needs: frontend (compare last value, animate)._
- [x] **Performance window picker (`All / 1M / 3M / YTD / custom date`)** — DONE. Re-bases the track
  record + risk + benchmarks to a chosen start (per-browser via localStorage); `api_track_record` /
  `api_risk` take a `start` param. Supersedes the period-selector item; an Overview KPI-strip variant
  over the window is a follow-up.
- [ ] **Overview KPI strip = Return · Sharpe · MaxDD · Volatility** — PARTIAL/DIFFERENT. Current
  overview KPIs are NAV/Day P&L/Leverage/Cash/Premium/Positions. Spec's overview strip is the four
  performance metrics. Decide whether to add the perf strip here. _Needs: frontend + windowed metrics._
- [ ] **Distribution chart** (return distribution / histogram) — MISSING. _Needs: frontend + data._
- [ ] **Risk-contribution chart** — MISSING. _Needs: frontend + risk-decomposition data._
- [ ] **Recent-activity blotter on Overview** — MISSING here (exists under Portfolio → Activity). Add
  a compact blotter to the Overview. _Needs: frontend (reuse `/api/orders`)._
- [ ] **System-health tiles on Overview** — PARTIAL. Detailed tiles exist but sit behind the Details
  toggle; the spec shows them on the Overview. _Needs: frontend._

## C · Portfolio → Holdings

- [ ] **Holdings table extra columns** — PARTIAL. Have: symbol chip (sector-coloured), weight-vs-target
  bar, weight%. Missing per the spec: **last price, day%, per-row sparkline, market value,
  delta-to-target (signed/coloured)**. _Needs: frontend + data (per-symbol day-change + short price
  history per holding)._
- [ ] **Sector allocation bar + list with over-cap red flag (cap 30%)** — PARTIAL/DIFFERENT. Current
  uses a nested sector/ticker donut. Either add the bar+list with explicit over-cap flagging, or
  confirm the donut + concentration panel covers it. _Needs: frontend._
- [x] **Concentration stats** (top-5, effective N, largest, # sectors) — OK / verify the exact set
  matches (HHI / effective names already present).
- [x] **Factor-tilt diverging bars** — OK.
- [x] **Covered-call book** (contracts, strike, expiry, DTE warn ≤7d, delta, premium) — OK.

## D · Performance

- [ ] **Returns: monthly-returns calendar heatmap** (`Jan…Dec` columns + yearly total) — MISSING.
  _Needs: frontend + monthly return series from the NAV history (data)._
- [ ] **Risk: 95% VaR** (% and ≈$ at today's NAV) — MISSING. `api_risk` has drawdown/vol but no VaR.
  _Needs: data (`api_risk`) + a frontend tile._
- [x] **Risk: max drawdown · current drawdown vs HWM · vol (annualised + per-day)** — OK
  (`api_risk` already returns these).
- [x] **Execution: per-fill slippage table + avg bps + "+bps = paid up vs mid"** — OK (matches spec).
- [ ] **Benchmark set reconciliation** — PARTIAL. Spec names SPY / XYLD / JEPI; the dashboard's
  `BENCH_VAR` lists XYLD/JEPI/QYLD/DIVO; the engine (`engine/benchmarks.py`) uses SPY / BXMD / BXRD.
  Pick one canonical set and align spec + engine + dashboard. _Needs: decision + small data/frontend._

## E · Backtest

- [ ] **Backtest metric tiles** — VERIFY. Spec: CAGR (net, 10-yr) · Sharpe · **Sortino** · MaxDD ·
  Volatility · **Monthly hit rate**, plus a Strategy-vs-SPY equity curve with legend. The tab is an
  iframe to the generated backtest dashboard (`scripts/build_dashboard.py`); confirm Sortino, monthly
  hit rate, and a CAGR tile exist, add if missing. _Needs: check `build_dashboard.py`; maybe data._

## F · Motion & interactions

- [ ] Flash-on-change (see B) · ticker marquee + pause-on-hover (see A) · `sfpulse` streaming dot
  (see A) · live ET clock (see A). Standard SFI motion tokens (`--ease*`, durations) are already in
  `theme.css`.

---

## Suggested order (small wins first)

1. **Frontend-only, cheap:** app-bg grid+glow, brand wordmark, live ET clock, streaming pulse,
   flash-on-tick, NAV hero restructure, surface health tiles + a blotter on Overview.
2. **Frontend + light data:** period selector + windowed KPIs, ticker marquee, sector over-cap
   flagging, holdings day%/delta-to-target columns.
3. **Needs data work:** 95% VaR (`api_risk`), monthly-returns calendar heatmap, per-holding sparkline
   + day-change history, distribution & risk-contribution charts, backtest Sortino / hit-rate / CAGR.
4. **Decision then align:** canonical benchmark set across spec ↔ engine ↔ dashboard.

> Note: the handoff's data is simulated; everything here must be wired to real fund/engine/feed data,
> which the live dashboard already does for the existing panels.

---

## Requested beyond the handoff

- [x] **No Alpaca↔dashboard discrepancies** — the dashboard must only ever show what Alpaca
  actually holds/filled. Three fixes:
  - **Covered-call book** (`api_calls`) is now sourced from the **live snapshot's short-call
    positions** (Alpaca truth via the 60s monitor), enriched with strike/delta/premium from the
    lifecycle log — not the write log. An unfilled/cancelled write carries no position, so it can
    never appear; a closed call disappears on the next snapshot.
  - **Premium ledger** — `options_lifecycle` is written **only on a real fill, at the real fill
    price** (`engine/covered_calls.py:_execute_option_leg`), so `premium_collected` can't count
    money that was never collected. (One-time cleanup: the 3 cancelled-write rows from 2026-06-29
    were deleted so premium reads its true value.)
  - **Orders blotter** — `engine.reconcile.reconcile_orders` syncs the `orders` table to Alpaca's
    current order statuses each cycle/day (stale `pending_cancel` → `canceled`, late fills,
    inserts option orders), so the activity blotter always matches Alpaca.
- [x] **Covered-call writes/closes chase to the touch** — option orders now re-peg to the bid
  (write) / ask (close) until they fill or the session nears the close, mirroring the equity
  chaser (`engine/covered_calls.py:OptionChase`). Stops the "passive limit at the mid never
  fills" problem the equities already had fixed.
- [x] **Performance start-date / window picker** — done (see Overview/Performance above).
- [x] **Sub-second headline metrics (live market-data feed)** — the Overview NAV/KPIs refresh every
  **1 s** from `/api/state`, which is now overlaid with **live streamed trade prices**
  (`engine.price_feed.LivePriceFeed` → an always-on `StockDataStream` in the dashboard process →
  `data.apply_live_prices` marks NAV/positions to the live cache). Previously the 1 s poll only saw
  the 60 s monitor snapshot, so headline data was up to a minute stale. The in-memory cache means no
  per-request Alpaca call. Coverage is the held book minus names that haven't printed on the free
  **IEX** feed (they keep the snapshot value); full coverage would need the paid SIP feed. Heavier
  Postgres panels (history, factors, health) stay on the 30 s poll.
