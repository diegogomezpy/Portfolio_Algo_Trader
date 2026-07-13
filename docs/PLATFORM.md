# PLATFORM.md — the on-the-fly strategy studio

The engine is not just the fixed low-beta book: it is a small **platform** for building systematic
strategies as *data* (ADR-001 / D37 north star). This doc is the reference for that layer — the
composition vocabulary, the sandboxed formula language, the full parameter surface, isolated
per-account execution, persistence, the autonomous scheduler, and the security model.

The primary book (`low_beta_overwrite`) is untouched by any of this — every builder path is isolated
to non-primary accounts and gated by a parity guard on the live strategy.

---

## 1. Composition vocabulary — `engine/signals.py`

Two disjoint namespaces so a bare name and a `raw_*` name never collide: a **bare name is a z-scored
signal** (higher = more attractive); a **`raw_*` name is the untouched native-unit value**.

### Signals (`register_builtins()`, palette from `signal_specs()`)

Each returns a per-symbol z-scored `Series` (winsorized at `factors.winsor_pct`), NaN preserved where
inputs are missing (`compose` neutral-fills to 0). The first four wrap the *live* `engine.factors` math
and are byte-identical to the corresponding factor sub-score (parity-tested).

| `name` | category | needs | computes | higher = |
|---|---|---|---|---|
| `quality` | Fundamental · composite | fundamentals | double-z of z(ROE)+z(gross_margin) | more profitable |
| `value` | Fundamental · composite | fundamentals | double-z of z(E/P)+z(B/P) | cheaper |
| `low_beta` | Price · defensive | prices | z(−β) vs SPY over `beta_window` | lower market sensitivity |
| `low_vol` | Price · defensive | prices | z(−realized_vol) over `vol_window` | calmer |
| `roe` | Fundamental | fundamentals | z(ROE) | more profitable |
| `gross_margin` | Fundamental | fundamentals | z(gross margin) | wider margins |
| `earnings_yield` | Fundamental | fundamentals | z(1/PE), guarded pe≠0 | cheaper on earnings |
| `book_yield` | Fundamental | fundamentals | z(1/PB), guarded pb≠0 | cheaper on book |
| `momentum` | Price | prices | z(12-1 momentum), `mom_lookback`/`mom_skip` | stronger trend |

### Raw fields (`FIELD_META`, computed by `raw_fields()`, palette from `field_specs()`)

Native-unit series, the formula engine's variables. Only fields whose source panel is present return.

`raw_pe`, `raw_pb`, `raw_ep` (1/PE), `raw_bp` (1/PB), `raw_roe`, `raw_gross_margin` *(fundamentals)*;
`raw_beta`, `raw_vol`, `raw_ret` (12-1 return) *(prices)*.

Data sources: `fundamentals` = point-in-time EDGAR panel (`pe_ratio`, `pb_ratio`, `roe`,
`gross_margin`); `prices` = the (date × symbol) close panel. `compose(scores, weights)` blends the
selected signals into the composite exactly as `factors.compute_factor_scores` does.

---

## 2. Formula grammar — `engine/formula.py`

A formula is a plain arithmetic expression over the vocabulary. **It is not `eval`.** The string is
parsed with `ast.parse(mode="eval")` and walked by `_walk_validate` under a strict node allow-list;
`_eval` only ever sees the supplied namespace.

**Operators:** binary `+ - * / ** %`, unary `-x / +x`, and a **single** comparison `< > <= >= == !=`
(whose purpose is to feed `where`).

**Functions:**

| Signature | Meaning |
|---|---|
| `z(x, p=0.01)` | winsorized cross-sectional z-score (the same `zscore` the factors use) |
| `rank(x)` | cross-sectional percentile rank in [0, 1] |
| `winsor(x, p=0.01)` | clip x to its [p, 1−p] quantiles (no standardize) |
| `clip(x, lo, hi)` | clip x to [lo, hi] |
| `where(cond, a, b)` | elementwise: a where cond true, else b |
| `log`, `log1p`, `sqrt`, `abs`, `sign` | elementwise math |
| `minimum(a, b)`, `maximum(a, b)` | elementwise min / max |

**Rejected at validation** (raise `FormulaError`): attribute access (`x.__class__`, `__import__`),
subscripts (`x[0]`), lambdas, comprehensions, boolean `and`/`or`, keyword args, chained comparisons,
non-numeric literals (strings, and `bool` explicitly), unknown names, unknown functions, and a formula
that evaluates to a scalar rather than a per-symbol series. `÷0` → NaN (neutralized, not ±inf).

`validate(expr, allowed_names)` (pre-run error surfacing) and `referenced_names(expr)` back the API;
`function_specs()` serves the palette to the editor. Example:
`where(raw_beta < 1, z(raw_ep) + 0.5*quality, 0)`.

---

## 3. StrategySpec + `effective_settings` — `engine/config_strategy.py`

A `StrategySpec` is a strategy as data. Score is defined by **`signals`** (name→weight blend) *or*
**`formula`** (which takes precedence when both are set).

**Construction knobs:** `construction` (`topn` | `optimizer`), `max_names`, `max_weight`, `leverage`
(applied at sizing, not baked into weights), `min_score`, `overlay` (`OverlaySpec`: mode/market/params).

**Extended knobs — `None` inherits the global YAML:** `min_position_pct`, `max_sector_pct`,
`risk_aversion_lambda`, `preselect_top_k`, `target_return_scale`, `beta_window`, `vol_window`,
`winsor_pct`, `mom_lookback`, `mom_skip`, `min_price`, `min_adv_usd`, `require_edgar`.

**Operational:** `rebalance_frequency` (`monthly` | `weekly` | `daily`), `mode` (`normal` | `express`).

### `effective_settings(base, spec)` — the leaf mapping

Deep-copies the global settings and overlays the spec's knobs onto the **same leaves the optimizer
AND the pre-trade risk gate read** — so a builder-chosen 8% cap or higher leverage on a sleeve isn't
vetoed by the global 5%/2× defaults:

- `portfolio.max_single_name_pct ← max_weight`, `min_position_pct`, `max_sector_pct`, `risk_aversion_lambda`
- `portfolio.target_leverage ← leverage`, **plus** `max_leverage = max(existing, leverage)` (lifts the
  hard cap so the risk gate never vetoes the sleeve's own leverage choice)
- `optimizer.preselect_top_k`, `optimizer.target_return_scale`
- `factors.beta_window / vol_window / winsor_pct / mom_lookback / mom_skip`
- `universe.min_price / min_adv_usd / require_edgar_fundamentals`

The docstring is explicit: **the primary book never goes through here.**

**Construction:** `construct_weights` = top-`max_names` by score, normalized to 1.0, clipped to
`max_weight` — and if the cap binds (too few names to reach 100%), it **holds cash** rather than
renormalizing past the cap. `optimizer` construction routes to `optimize.optimize_portfolio` and
**degrades to top-N** on any missing config. `spec_to_dict` / `spec_from_dict` round-trip the spec.

---

## 4. Isolated per-account execution — `engine/account_runner.py`

`run_strategy_on_account(account, strategy, …, dry_run, mode)` runs any `Strategy` on **one
non-primary account**, mirroring `run_cycle`'s sizing → risk-gate → plan → execute → snapshot, with
three deliberate safety differences:

- **Isolation** — builds *that account's own* `AlpacaClient` + `Broker` from `credstore` creds; orders
  can only route to that Alpaca account.
- **No reconcile** — reconcile writes shared position state; skipped for a single-account sleeve.
- **Refuses primary** — `account == PRIMARY_ACCOUNT` raises; the live book stays on `run_eod`.

If the strategy carries a `.spec`, the run uses `effective_settings` (§3). `nav = equity × leverage`.
`dry_run` computes + risk-gates the plan and returns the sized orders **without submitting** (the
builder's preview). `mode` selects patient `submit_and_track` vs fast `submit_express`. Snapshots and
`cycle_key` are account-tagged (`acct:<slug>:<date>`). Equity-only for now (per-account overlay
execution is a later piece).

---

## 5. Persistence — `engine/specstore.py` + `strategy_specs`

One active spec per account. The `strategy_specs` table (`engine/db.py`) is created on demand
(`create(checkfirst=True)` — no manual VM migration). Columns: `account` (unique, credstore slug),
`name`, `spec` (JSON `spec_to_dict`), `rebalance_frequency`, `mode`, `auto_enabled`
(`server_default=false`), `last_run` (scheduler idempotency lease), timestamps.

`save_spec` upserts and **preserves existing `auto_enabled`/`last_run` when not overridden**;
`get_spec` / `list_specs` return non-secret dicts; `set_auto_enabled`; `mark_run`; `delete_spec`. No
secrets here — credentials stay Fernet-encrypted in `engine.credstore`.

---

## 6. Autonomous scheduler — `engine/scheduler.py`

`run_scheduled(...)` is called each pass by the dashboard monitor loop, after the NAV snapshots and
awaited so a run never overlaps the next pass. It is a **separate mechanism** from the primary book's
`run_eod` APScheduler.

**Cadence** (`is_due`): `daily` if not already run today; `weekly` if the ISO (year, week) differs from
the last run; `monthly` if the (year, month) differs. `last_run >= as_of` → not due.

**Layered safety guards:**
- **Refuses primary** — only iterates credstore sleeves; `account_runner` also refuses primary.
- **Opt-in, defaults OFF** — a row is skipped unless `auto_enabled` is truthy. Nothing trades
  automatically until the operator flips it on in the builder.
- **Market + timing gate** — skips before `execution.rebalance_hour_et` (13 ET) and if the market
  clock can't be confirmed open (mirrors the primary book's patient mid-session timing).
- **Per-account idempotency lease** — `mark_run(acct, as_of)` is stamped only *after* a run returns; a
  transient failure leaves `last_run` unset so the next pass retries (and alerts).

---

## 7. Security model

The dashboard is **publicly viewable**; everything that changes state or reads broker credentials is
gated by `SEPI_EXEC_TOKEN`. `_exec_gate(header)` returns `None` only when the env var is set **and** the
`X-Exec-Token` header matches via `hmac.compare_digest` — unset ⇒ the endpoint is disabled (fails
closed), so a fresh deploy can't accidentally expose trading. The token lives only in the git-ignored
env file, never in source/YAML.

**Token-gated:** `POST /api/strategy/{preview,save,run}`, `POST /api/accounts/{add,remove}`,
`POST /api/exec/*`, **and the account-management reads** `GET /api/accounts` +
`GET /api/accounts/{slug}/state` (they expose partial key fingerprints + per-sleeve balances and make
real-key Alpaca calls). **Public:** the fund read surface + the builder palette/preview inputs
(`GET /api/signals`, `/api/formula/vocab`, `/api/strategy/spec`, `/api/strategy/run_status`). Broker
secrets are never returned by any endpoint — only a masked fingerprint.

---

## 8. API surface — builder endpoints (`dashboard/app.py`)

| Method + route | Purpose | Gated |
|---|---|---|
| `GET /api/signals` | signal palette (`signal_specs()`) | — |
| `GET /api/formula/vocab` | `{signals, fields, functions}` for the formula editor | — |
| `POST /api/strategy/preview` | dry-run a spec → the sized order plan it *would* trade; no orders | ✅ |
| `GET /api/strategy/spec?account=` | the account's saved spec (non-secret) or `{}` | — |
| `POST /api/strategy/save` | validate + persist a spec (+ cadence/mode/auto_enabled) | ✅ |
| `POST /api/strategy/run` | manual "Run now" — trade a spec live on its (non-primary) account | ✅ |
| `GET /api/strategy/run_status?account=` | poll a background run | — |
| `GET /api/accounts` | account roster (masked fingerprints) | ✅ |
| `POST /api/accounts/add` · `POST /api/accounts/remove` | manage encrypted account creds | ✅ |
| `GET /api/accounts/{slug}/state` | per-sleeve NAV / #positions / #open-orders | ✅ |

`_spec_from_body` validates the full parameter surface (signals coerced to `{str:float}`; a non-empty
formula is checked with `formula.validate`), then `spec_from_dict`. `/api/strategy/run` uses the posted
spec if present, else the account's saved spec; it refuses if a run is already in progress or (non-express)
the market is closed.

---

**Related:** [ADR-001](adr/ADR-001-multi-strategy-platform.md) (the decision), D37 in
[DECISIONS.md](DECISIONS.md), and the builder UI in the dashboard's **Builder** tab.
