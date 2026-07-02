# SFI Design Language

**SFI — Systematic Equity Premium Income.** This is the design language for everything the project shows
a human: the live dashboard, the generated backtest report, emails, and any future surface.
It supersedes the earlier "dark-institutional / terminal-grade" look (near-monochrome ink, blue
accent, IBM Plex Sans).

There are three artefacts; keep them in sync:

| Artefact | Role |
| --- | --- |
| **This file** (`docs/DESIGN_LANGUAGE.md`) | the written spec — rules, tokens, rationale |
| `design_lang/*.html` | the **visual handoff** — open in a browser to see the language applied (Design Language reference + a full Dashboard mock). Self-contained (fonts inlined). |
| `dashboard/static/theme.css` | the **implementation** — the single source of truth in code. Both the live tab and the generated backtest tab `<link>` it; chart SVGs read these CSS variables via `getComputedStyle`. |

> The handoff uses `--sf-*` prefixed token names; the implementation maps those same values onto
> `theme.css`'s unprefixed tokens (`--bg`, `--accent`, …). When a value here and in `theme.css`
> disagree, **`theme.css` wins** — update this doc to match.

---

## 1 · Identity

- **Name:** SFI · Systematic Equity Premium Income.
- **Mark:** a 3×3 dot grid with the **middle column in teal**, the rest in faint slate. Rendered
  as the header logo (`dashboard/static/index.html`) and the favicon (`dashboard/app.py`).
- **Voice:** a research terminal, not a storefront. Dense, quiet, numeric. Every pixel either
  carries data or gets out of the way.

## 2 · Principles

1. **Causal, not decorative.** Motion and colour explain a change in data or state. If it doesn't
   carry meaning, it doesn't move and it isn't coloured.
2. **Fast & quiet.** UI feedback lands in ≤200 ms. Transitions are short, subtle, and never block
   reading the numbers.
3. **Enter soft, exit quick.** Content decelerates in (ease-out), accelerates out (ease-in).
   Travel is small — 8–10 px, never a full slide across the screen.
4. **Colour = direction.** Teal is brand & focus. **Green/red are reserved strictly for signed
   P&L** (signed numbers and the bars/flashes that represent them) — never for chrome, never for
   an equity curve, never for a sector.
5. **Flat.** No gradients, no texture, minimal chrome, hairline borders. Shadow appears only on
   genuinely floating elements (toast, sticky header).

## 3 · Colour tokens

Defined in `dashboard/static/theme.css` `:root`. Hex values are canonical here only as
documentation; edit them in `theme.css`.

### Surfaces — navy ink
| Token | Value | Use |
| --- | --- | --- |
| `--bg` | `#0b1322` | app canvas (flat) |
| `--bg-2` | `#0a1120` | sticky header, code & footer strips |
| `--panel` | `#0e1830` | panels |
| `--panel-2` | `#16223a` | nested rows, hover, inputs |
| `--panel-3` | `#1f2c49` | active / elevated |
| `--line` | `#1b2740` | borders / hairlines |
| `--line-soft` | `#131e34` | inner rows / faint hairlines |
| `--grid` | `#16223a` | chart gridlines |

### Text
| Token | Value | Use |
| --- | --- | --- |
| `--fg` | `#eaf2fb` | primary |
| `--fg-dim` | `#cdd9e8` | secondary |
| `--muted` | `#65758c` | labels / tertiary (only at ≥11 px) |
| `--faint` | `#34425e` | axes / quaternary |

### Accent & semantic — one accent; green/red signed-only
| Token | Value | Use |
| --- | --- | --- |
| `--accent` | `#46b8ad` | **teal** — brand, focus, the primary data series |
| `--green` | `#5fb088` | **signed-positive only** |
| `--red` | `#cf6f66` | **signed-negative only** |
| `--amber` | `#d8a84b` | warn / stale only |
| `--purple` | `#9b87d4` | benchmark series only |
| `--spy` | `#8893a3` | neutral benchmark series |

Each has a matching `--*-dim` (low-alpha fill) for pill/banner backgrounds.

### Sectors — low-saturation, signed-neutral
`--sec-tech #6f93d6` · `--sec-comm #9b87d4` · `--sec-health #cf8a86` · `--sec-fin #4ea6a0` ·
`--sec-disc #d8a84b` · `--sec-staples #7fae8f` · `--sec-energy #c2a36b` · `--sec-ind #8893a3` ·
`--sec-mat #6fb0c4`. These tint ticker chips and exposure rings; they never compete with the
teal accent or the green/red P&L colours.

## 4 · Typography

- `--font` = **Space Grotesk** — all display, UI, and labels.
- `--mono` = **IBM Plex Mono** — **every number**, tabular (`font-variant-numeric: tabular-nums`).
  This is the terminal signature: numbers never reflow or jitter.
- Loaded from Google Fonts in both `index.html` and the generated backtest head (an iframe does
  not inherit the parent's fonts, so the backtest page links them itself).
- Micro-labels are UPPERCASE, tracked ~.1em. Panel body is Sentence case. System strings
  (status, timestamps, tickers) are lowercase/mono; tickers are uppercase in a sector-tinted chip.

## 5 · Shape & space

- **Radius:** `--r-sm 3px` (chip) · `--r 6px` (control) · `--r-lg 7px` (panel).
- **Spacing:** 8 px base · 13–16 px panel padding · 14 px gap.
- **Borders:** 1 px hairline `--line`; inner rows `--line-soft`.
- **Elevation:** flat (`--shadow: none`). Floating only → `--shadow-float: 0 8px 22px rgba(0,0,0,.5)`.

## 6 · Motion

Tokens in `theme.css`:

- Easings: `--ease` standard `cubic-bezier(.4,0,.2,1)` · `--ease-decel` enter `cubic-bezier(0,0,.2,1)` ·
  `--ease-accel` exit `cubic-bezier(.4,0,1,1)` · `--ease-emph` `cubic-bezier(.2,.8,.2,1)`.
- Durations: `--dur-instant 80ms` (hover/press/focus) · `--dur-fast 140ms` (value flash, tooltip,
  chip) · `--dur-base 200ms` (tab slide, row enter, toast) · `--dur-slow 320ms` (panel stagger,
  view change). Count-ups and chart draw-ins run 700–1100 ms.

Catalogue: live value count-up + sign-coloured flash; chart draw-in (`stroke-dashoffset 1→0`);
live heartbeat pulse (only while the feed is live); tab underline slide; row hover; skeleton
shimmer (until first data frame); toast (in 200 ms ease-out / out 160 ms ease-in); tooltip fade +
4 px rise; panel-enter stagger (80 ms). **All wrapped in `@media (prefers-reduced-motion: reduce)`** —
count-ups snap, loops stop, charts render already-drawn.

## 7 · Iconography

24×24 grid, 2 px keyline padding · 1.8 px stroke, round cap & join · **outline only, never filled** ·
`currentColor` (inherits text) · 14–16 px inline, 20 px standalone.

## 8 · Data visualization

- Gridlines: hairline `--grid`; **no axis lines, no ticks**.
- Axis labels: IBM Plex Mono 11 px, `--muted`, tabular.
- Lines: 1.6–2.2 px; **≤4 series**; the **primary series is teal** (`--accent`).
- The **equity / NAV curve is teal**, regardless of up/down — it is a series, not a signed number.
- Benchmarks: neutral slate / blue / purple (`--spy`, `--sec-tech`, `--purple`) — **never green/red**.
- Area fill: single primary only, ~14–18%→0%.
- Semantic series: drawdown red, volatility amber, equity teal.
- Signed bars (attribution sleeves, VRP IV−RV, calendar return deltas): green positive, red negative.
- Insufficient data → a dashed placeholder ("appears after 2 points"), never a broken axis.

## 9 · Numbers, formatting & content

| Type | Rule | Example |
| --- | --- | --- |
| Currency · large | no decimals; abbreviate in dense cells | `$4,182,940` · `$4.18M` · `$148.2k` |
| Currency · price | two decimals | `$520.40` |
| Signed delta | always `+`/`−`; colour by sign | `+$28,640` · `−0.2%` |
| Percent | 1 dp default · 0 dp coarse · 2 dp daily | `0.69%` · `31%` · `0.58%` |
| Basis points | slippage; `+` = adverse | `+5.8 bps` |
| Multiples / ratios | `×` suffix; ratios 2 dp | `1.70×` · `1.34` |
| Missing / null | en-dash — never `0` or `NaN` | `–` |
| Dates & time | `Mmm D` display · ISO in logs · TZ on clocks | `Jun 17` · `2026-06-23` · `15:48 ET` |
| Relative age | humanized, compact | `44s` · `2m ago` · `in 6 days` |

## 10 · Responsive & layout

Container max **1680 px**, gutters **22 px**. Breakpoints: ≥1440 (6-col KPI, 2-col panels) →
1200–1440 (3-col KPI, panels stack) → 1024–1200 (tables gain horizontal scroll) → <1024 (single
column, 2-col KPI, ribbon scrolls) → <720 (stacked cards, secondary columns hidden). Dense tables
keep a min-width and scroll rather than reflow; header row stays sticky. Charts are fluid. Top
chrome and the status ribbon stay pinned.

## 11 · States & resilience

- **First-run:** every panel has a pre-trade empty state, not just charts.
- **Freshness ladder:** live `<3 min` (green pulse) · stale `3–10 min` (amber dot + "as of HH:MM",
  data still shown) · down `>10 min` (red banner, last-known greyed, retry surfaced).
- **No jitter:** numeric columns are fixed-width tabular; ticks don't reflow.
- **Independent load:** each panel skeletons on its own; chrome paints first.
- **Partial:** gate metrics (Sharpe, annualized) until ≥10 days, marked `*`.

## 12 · Accessibility & disclosure

- **Triple-encode sign:** arrow + `+/−` + colour. Never colour alone (colour-blind safe).
- AA contrast on text; `--muted` only at ≥11 px.
- Reduced motion respected (§6). `aria-live="polite"` on value & alert updates. Full keyboard tab
  order with a visible 3 px accent focus ring. Touch targets ≥44 px, pointer ≥24 px.
- **Disclosure strip** pinned to every view: environment (`PAPER`/`LIVE`), the
  representative-results caveat, and an explicit "as-of" timestamp. Status is always also stated in
  text — never motion- or colour-only.
