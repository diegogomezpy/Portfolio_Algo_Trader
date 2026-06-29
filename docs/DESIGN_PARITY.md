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
- [ ] **Period selector `1M / 3M / YTD / ITD`** driving the KPI strip — MISSING. _Needs: frontend +
  windowed metrics (return/Sharpe/MaxDD/vol over the chosen window) in the data layer._
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
