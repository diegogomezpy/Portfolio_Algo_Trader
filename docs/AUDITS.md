# AUDITS.md — repo audit log

One page per audit pass: what was found, what changed, what's still open. Findings live in
the commit messages too, but this is the place to see them together.

---

## 2026-07-02/03 — Full repo audit (correctness + hygiene)

Commits `01d35d2..815a192`. 355 → 425 tests.

**Bugs fixed (ranked by strategy impact):**
- **B1** `AlpacaClient.account_activities` was defined twice; the second def shadowed the
  first, so every daily OPASN assignment check raised `TypeError` into a broad `except` —
  **assignment detection had never worked**. Merged; guarded by an AST no-duplicate-methods
  test over every class in the codebase.
- **B2** `options_daily_check` ignored `overlay_mode`: within 5 days of any holding's
  earnings it would have written **per-name covered calls on top of the SPY spread**. Now
  dispatched by `covered_calls.overlay_mode()`; index mode manages only the market spread.
- **B3** No close-side for the SPY spread: `close_calls` bought back short legs only,
  stranding the long wing. `close_index_overwrite()` closes short-first-then-wing (order is
  load-bearing — Alpaca 40310000), wired into `run_cycle` + the daily expiry check.
- **B4** Assignment of the (stock-less) SPY short leg left **short SPY stock** unhandled —
  now flattened at market immediately (`short_flatten` lifecycle event).
- **B5** Missing account equity silently fell back to the YAML `nav: 100_000` → a $1M book
  sized at $100k. Now a hard error; `daily_job` alerts + retries.
- **B6** `nav_history` raw-row limit ≈ 17h of 60s snapshots — the NAV windows >1D were
  starved. Past days now daily-sampled server-side.
- **B7** Reconcile's correction snapshots wrote `weights={}` (60s dashboard zero-flicker).

**Parameter layer:** 7 dead settings deleted (incl. `close_before_earnings`, which claimed
to gate a behavior that ran unconditionally — now actually wired); `min_position_pct`
replaces `min_position_usd/nav` (the old pair silently became a 4%-of-anything floor);
`iv_window`, `min_bid_frac`, `rewrite_after_earnings_days`, `ff5_max_stale_days` promoted
from hardcodes; `paper=True` ×3 now derives from `ALPACA_BASE_URL` (`config.is_paper_env`).
Guarded by `test_settings_coverage` (every YAML leaf key must be referenced in code).

**Perf/ops:** `lookback=10**9` full-store panel loads capped (the e2-small OOM cause);
`engine/retention.py` implements the `data_retention` block (snapshot thinning + parquet
pruning); dashboard's monitor no longer double-writes snapshots alongside sharpe-eod's.

**Dead code:** ~600 lines removed (legacy touch-chase executor path, 8 unused AlpacaClient
surfaces, 5 dead dashboard render fns, `_touch_price`). `engine/symbols.py` unified 3
OCC regexes + 4 `len<=5` heuristics; `safe_covariance` moved into `engine.covariance`.

---

## 2026-07-03 — Layout & web-client performance pass

Commits `05da21c..` (this pass). Measured before/after with the live preview.

- **W1** Governed polling: hidden tabs poll nothing; closed market slows state 1s→15s and
  execution 2.5s→60s. One master tick replaces five free-running intervals.
- **W3** `/api/execution`'s Alpaca `get_orders` behind a shared TTL cache (2s in-session /
  60s off-hours) — was one upstream REST call per poll per tab, 24/7.
- **W4** Compression end-to-end (Caddy `encode zstd gzip` + FastAPI GZip): index page
  186KB → 53KB on the wire; **W7** `uvicorn[standard]` (uvloop/httptools).
- **W2** `memoPaint` change-detection: the treemap SVG and other heavy panels rebuild only
  when their inputs change (previously every second, even ~17h of identical after-hours data).
- **W5** `index.html` (185KB monolith) split → 9.4KB shell + cacheable
  `app.js`/`dashboard.css`/`fonts.css` (`?v=N` busting); **W6** fonts self-hosted
  (132KB woff2) — zero third-party requests.
- **Layout:** shared backtest math → `engine/backtest_lib.py` (scripts are now pure CLIs);
  `presentation/` → `tools/deck/`; `deploy/update.sh` gained `--dashboard-only` and an
  open-orders guard before any sharpe-eod restart (its restart cancels all orders).

**Incident (2026-07-03, self-inflicted during this pass):** the first retention wiring
defaulted ON inside `daily_job`; the integration suite exercises `daily_job` in the repo
working dir, so a test run pruned ~1,000 historical equity parquet files (2020-07 →
2024-07) from the **local** store. VM store was untouched; local restored from the VM the
same hour. Fixes: retention is now **opt-in per call site** (`daily_job(retention_fn=…)`,
passed only by `serve`), `prune_equity_parquet` refuses to delete >10% of the store in one
pass (steady state is ~1 file/day), and a regression test locks both properties.

**Still open (deliberate):** backtest still models per-name calls (SPY-spread validation
pending); consolidated price-panel parquet; EDGAR quarterly refetch scoped to the liquid
universe; delta-aware overwrite sizing option; IV-regime coverage modulation.
