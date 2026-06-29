# Handoff: SFI Fund — Algorithmic Covered-Call Dashboard

## Overview
A real-time monitoring dashboard for a systematic, factor-tilted equity fund that overlays a covered-call options strategy. It shows live NAV/P&L, holdings, factor exposures, the covered-call book, performance vs. benchmarks, risk, execution quality, and a long-horizon backtest. The product is an operator/PM-facing "engine console," not a retail brokerage app — dense, data-forward, monospace numerics, dark room aesthetic.

## About the Design Files
The files in this bundle are **design references created in HTML** — prototypes that demonstrate the intended look, layout, and behavior. They are **not production code to copy directly**. Your task is to **recreate these designs in the target codebase's existing environment** (React, Vue, Svelte, etc.) using its established component library, data layer, and conventions. If no codebase exists yet, pick the most appropriate stack (a React + TypeScript SPA with a charting lib such as Recharts/visx/lightweight-charts is a natural fit) and implement there.

All numbers in the prototype are simulated/illustrative (random-walk price ticks, hardcoded tables). In production, wire these views to real fund data, a market-data feed, and the strategy engine's outputs.

## Fidelity
**High-fidelity (hifi).** Final colors, typography, spacing, density, and interaction patterns are intentional and should be matched closely. Charts in the prototype are hand-rolled SVG/canvas; in the real app, reproduce the same visual style (line weights, colors, gridlines, fills) using the codebase's charting library rather than porting the SVG path math.

## Layout System
- **Max content width:** 1680px, centered, `padding: 0 22px`.
- **App background:** `#070d18` with a faint 30px dot/line grid overlay and a soft radial teal glow at top center (`radial-gradient(120% 80% at 50% -10%, rgba(70,184,173,.06), transparent 55%)`).
- **Sticky top chrome** (`z-index:30`, `background: rgba(11,19,34,.9)`, `backdrop-filter: blur(12px)`, bottom border `#1b2740`) stacks three rows:
  1. **Header row:** logo lockup (3×3 dot glyph + vertical "SFI" mono mark + divider + "Fund" wordmark) · primary tab nav · right cluster (PAPER badge, "streaming · live feed" pulse, clock, refresh button).
  2. **Status ribbon:** horizontal scroll of label/value chips (Engine, Market, Next rebalance, Risk gate, Drift L1, Alerts 24h), each with a colored status dot.
  3. **Ticker tape:** infinite marquee (`@keyframes sfMarq`, 54s linear, pauses on hover) of symbol · price · sparkline · change% · CALL tag.
- **Body:** single column of cards, `display:flex; flex-direction:column; gap:14px`. Cards: `background:#0e1830; border:1px solid #1b2740; border-radius:7px`.

## Screens / Views
Primary navigation is **four tabs**: Overview · Portfolio · Performance · Backtest. Some tabs have sub-tabs (pill buttons: active `bg:#16223a, border:#1b2740, fg:#eaf2fb`; inactive transparent, `fg:#65758c`).

### 1. Overview
- **NAV hero card:** "NET ASSET VALUE" eyebrow; NAV in `Space Grotesk 500 46px` tabular-nums; day P&L pill colored by sign (`+$… · +x.xx% today`); inline NAV sparkline. Right side: window-change stats. On live tick, the NAV and P&L cells briefly flash a tinted background (green up / red down).
- **Period selector:** `1M / 3M / YTD / ITD` pills driving a KPI strip.
- **KPI strip:** Return (net), Sharpe (annualized), Max drawdown (peak-to-trough), Volatility (annualized) — value colored by sign where relevant.
- **System health tiles:** Engine (Live), Market (Open), Last rebalance, Next rebalance, Drift vs target, Alerts (24h), Factor data, Last order — each a dot + value + sub-caption.
- Plus distribution chart, risk-contribution chart, and a recent-activity blotter.

### 2. Portfolio  — sub-tabs: Holdings · Activity
- **Holdings:** positions table — symbol chip (sector-colored), name, sector, last price, day%, sparkline, market value, weight%, a weight bar showing **current weight vs target tick**, and delta-to-target (signed, colored). Side panels: sector allocation bar + list (over-cap weights flagged red, `cap = 30%`), concentration stats (top-5, effective N, largest, #sectors), factor-tilt diverging bar chart (Value/Quality/Momentum/Size/Low-vol style tilts), and the **covered-call book** (symbol, contracts, strike, expiry, DTE — colored warn ≤7d, delta, premium; subtitle "N open · NN sh covered").
- **Activity:** order blotter (symbol, side buy=green/sell=red, qty, status filled/partial, fill price) and an alerts log (timestamp, type, message, severity color, delivery ✓ sent / dry-run).

### 3. Performance — sub-tabs: Returns · Risk · Execution
- **Returns:** benchmark comparison rows (Strategy vs SPY, XYLD, JEPI) and a monthly returns **calendar heatmap** (`Jan…Dec` columns + yearly total).
- **Risk:** max drawdown, current drawdown vs high-water mark, volatility (annualized + per-day), and 95% VaR (% and ≈$ at today's NAV).
- **Execution:** slippage table per filled limit order (symbol, side, qty, intended vs filled price, bps colored — positive=paid up=red, cost in $), with average-bps summary. Note: "+bps = paid up vs mid".

### 4. Backtest
- **Metric tiles:** CAGR (net, 10-yr), Sharpe, Sortino (downside-adj), Max drawdown, Volatility, Monthly hit rate.
- **Equity curve:** Strategy (accent teal) vs SPY (`#8893a3`) over 10 years, with legend.

## Interactions & Behavior
- **Live simulation loop:** on an interval the prototype ticks prices (random walk), recomputes NAV/P&L/ticker, and flash-highlights changed NAV/P&L cells. In production, replace with a real feed subscription; keep the flash-on-change affordance.
- **Tab + sub-tab switching:** pure client-side view state; no route change in the prototype (use real routing in the app).
- **Refresh button:** re-pulls/re-simulates data.
- **Ticker marquee:** CSS animation, `animation-play-state: paused` on hover.
- **Status dot pulse:** `@keyframes sfpulse` ring pulse on the "streaming" indicator.
- **Clock:** simulated ET clock, `HH:MM:SS ET`, monospace.

## State Management
- `view`: `'overview' | 'portfolio' | 'performance' | 'backtest'`.
- `subP`: `'holdings' | 'activity'` (Portfolio).
- `subPerf`: `'returns' | 'risk' | 'execution'` (Performance).
- `period`: `'1M' | '3M' | 'YTD' | 'ITD'` (Overview KPI window).
- Live tick state: current prices, NAV, day P&L, simulated clock, last-changed direction (for flash highlight).
- Data fetching (production): fund NAV/positions, market quotes (streaming), factor scores, options book, orders/alerts, benchmark series, backtest series.

## Design Tokens

### Color
- Backgrounds: app `#070d18`, base `#0b1322`, panel/card `#0e1830`, grid fill `#16223a`.
- Borders: line `#1b2740`, soft `#131e34`.
- Text: primary `#eaf2fb`, dim `#cdd9e8`, muted `#65758c`, faint `#34425e`.
- Accent (teal): `#46b8ad`.
- Semantic: positive `#5fb088`, negative `#cf6f66`, warn `#d8a84b`.
- Sector palette: Information Technology `#6f93d6`, Communication Services `#9b87d4`, Health Care `#cf8a86`, Financials `#4ea6a0`, Consumer Discretionary `#d8a84b`, Consumer Staples `#7fae8f`, Energy `#c2a36b`, Industrials `#8893a3`, Materials `#6fb0c4`. (Chips use the sector color at 18% alpha for background, full color for text.)

### Typography
- **Space Grotesk** (400/500/600/700) — UI text, labels, headings. NAV hero 500 46px; section labels 600 10px uppercase `letter-spacing:.16em`; tabs 600 13px.
- **IBM Plex Mono** (400/500/600) — all numbers, prices, clock, status values. Use `font-variant-numeric: tabular-nums` on numeric cells.
- Eyebrow/label pattern: `600 10px Space Grotesk; letter-spacing:.12–.16em; text-transform:uppercase; color:#65758c`.

### Shape / spacing
- Card radius `7px`; pill/badge radius `4–7px`; small control radius `6px`.
- Card-stack gap `14px`; content padding `18–22px`; grid overlay cell `30px`.

### Motion
- Marquee `54s linear infinite`; status pulse `sfpulse 2.4s infinite`; cell flash highlight on value change (~tinted bg, short fade).

## Assets
No external image assets. The logo is an inline SVG (3×3 grid of `r≈3.4` circles; center column teal `#46b8ad`, others faint `#34425e`) next to a vertical mono "SFI" mark and a "Fund" wordmark. Sparklines and all charts are generated from data — reproduce with the codebase's charting library. Fonts load from Google Fonts (Space Grotesk, IBM Plex Mono).

## Files
- `SFI Dashboard - Handoff.html` — self-contained, openable reference of the full dashboard (all four tabs + sub-tabs). Open in a browser to explore every state.
- `SFI Design Language - Handoff.html` — the design-language reference (color, type, components, tokens) for the SFI system.
- `SFI Dashboard.dc.html` + `support.js` — the source prototype (Design Component format). Read for exact markup, inline styles, and the data/sim logic; requires `support.js` in the same folder to run.

Start from the two `- Handoff.html` files for visual reference; consult the `.dc.html` source for precise values.
