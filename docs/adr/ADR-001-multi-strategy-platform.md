# ADR-001: From single-strategy engine to a multi-strategy platform

**Status:** Accepted
**Date:** 2026-07-08 (proposed and accepted)
**Deciders:** Diego (owner)
**Supersedes / relates to:** extends the module boundaries in [ARCHITECTURE.md](../ARCHITECTURE.md);
sits above the execution decisions D35 (launch hardening) and the 2026-07-06 execution X-series.
Recorded as `D37` in [DECISIONS.md](../DECISIONS.md).

---

## Context

Today the system runs exactly one strategy — a low-beta factor equity book with a SPY
call-spread overwrite, 2× levered, paper-traded on a single Alpaca account. That strategy is
**not a first-class object**; it is *implicit*, fused into the orchestrator:

- `scripts/run_eod.py::compute_targets` hardwires the `factors → covariance → optimize` path.
- `run_cycle(overlay=True)` calls the SPY covered-call functions in `engine/covered_calls.py`
  directly, as a special post-step.
- `config/settings.yaml` is one flat namespace mixing *platform* params (`execution`) with
  *strategy* params (`factors`, `covered_calls`, `portfolio` weights).
- `engine/config.py::get_alpaca_client()` returns the one account's client.
- `engine/risk.py` mixes a platform check (`_check_leverage_cap`) with a strategy check
  (`check_covered_call_coverage`).
- The dashboard assumes a single book throughout (`/api/state`, positions, the overlay card).

The goal is to make this a **general systematic investment platform**: run *N* heterogeneous
strategies (factor equity, ETF trend, cash-secured puts, future crypto sleeves…) on shared,
hardened infrastructure — execution, reconciliation, monitoring, risk, backtest, dashboard.

**Forces at play**

- *Strong foundations, wrong granularity.* The pure-planner / injected-I/O split, the
  backtest↔live parity principle, the `settings.yaml` discipline, and the recently hardened
  execution engine are exactly the properties a platform needs. What is missing is a strategy
  *abstraction boundary*.
- *Owner constraints.* Paper trading until Phase 7. Small increments, sketch-before-build.
  **Credential boundary:** no credentials in source or YAML; the assistant never handles raw key
  values; the owner manages secrets himself.
- *Existing infra to reuse.* GCP project `covered-call-factor-portfolio`, Secret Manager already
  holds three secrets, the VM has a least-privilege service account, and the dashboard is exposed
  to the public internet via Tailscale Funnel + basic auth.
- *Don't boil the ocean.* The risk is rewriting working internals in the name of generality. The
  discipline is to extract boundaries **around** proven code and prove them with a second strategy.

This ADR records three linked decisions: **(1)** a pluggable Strategy interface (the keystone),
**(2)** an account-per-strategy capital model, and **(3)** per-strategy broker credentials in
GCP Secret Manager, swappable from the dashboard.

---

## Decision

### D-a — Strategy becomes a first-class plugin (the keystone)

Introduce a `Strategy` protocol and a registry. Each strategy produces a **`TargetBook`** and
nothing else; all I/O (reconcile, risk gate, execute, monitor) is shared platform machinery that
runs *around* the strategy.

```python
class Strategy(Protocol):
    name: str
    def generate(self, ctx: StrategyContext, as_of: date) -> TargetBook: ...

@dataclass
class TargetBook:
    equity_weights: pd.Series           # fractions of the strategy's deployable base
    overlay: OverlayPlan | None = None  # optional derivatives leg(s) (e.g. the SPY spread)
    metadata: dict = field(default_factory=dict)   # e.g. beta_p, signal snapshot, sizing notes
```

`StrategyContext` carries the read surfaces a strategy needs (price panel, fundamentals, universe,
current positions, its deployable capital base) — never a broker write handle. `run_cycle` becomes
strategy-agnostic: load the strategy named in config, call `generate()`, run the shared pipeline.
The current strategy is refactored — **not rewritten** — into `strategies/low_beta_overwrite.py`,
the first registered plugin, which proves the interface rather than being special.

### D-b — Account-per-strategy capital model

Each strategy runs in **its own Alpaca account** with its own key pair and its own capital.
Attribution is therefore *native*: the account **is** the sleeve, Alpaca is the per-strategy source
of truth, and there is no virtual position ledger to maintain. `config.get_alpaca_client()` becomes
`get_client(strategy)`. A strategy is defined by `{strategy_impl, account/credential ref, capital,
leverage, schedule, enabled}`.

### D-c — Per-strategy credentials in GCP Secret Manager, dashboard-swappable

One secret per strategy account (`sepi-strategy-<slug>`), payload a JSON credential set
`{api_key, api_secret, base_url}`. **Swapping a key = publishing a new secret version.** The engine
reads a strategy's secret at run time and builds its client in-process — never written to Postgres,
disk, or the nightly `pg_dump`. The dashboard writes via the Secret Manager API (`add_secret_version`),
**gated by the same `SEPI_EXEC_TOKEN` as the Execute console** (a caller who can move the book can
already do the most dangerous thing; credential swaps sit at that same trust level), write-only (UI
shows a masked fingerprint), payload never logged. GCP Cloud Audit Logs record every swap for free.
**The assistant never handles key values**; they flow browser → server → Secret Manager only.
Granting the VM service account `secretmanager.secretVersionAdder` is the owner's `gcloud` step.

---

## Options Considered

### Fork 1 — Strategy abstraction

**Option A — Pluggable `Strategy` interface + registry (chosen).**

| Dimension | Assessment |
|---|---|
| Complexity | Medium (mostly refactor of existing well-factored code) |
| Backtest parity | Free — backtest and live both call `generate()` |
| Extensibility | High — new strategy = new plugin, infra untouched |
| Risk | Low-medium — proven internals wrapped, not rewritten |

**Pros:** one seam unlocks multi-strategy, sleeves, and automatic backtestability; forces the
platform/strategy split that's already half-done. **Cons:** requires disciplined interface design;
the overlay must be re-expressed as strategy output rather than an orchestrator post-step.

**Option B — Keep the monolith, copy-paste per strategy.** Fork `run_eod` per strategy.
*Pros:* zero abstraction work now. *Cons:* execution/risk/reconcile logic duplicated and drifts;
backtest parity breaks; unmaintainable past two strategies. Rejected.

**Option C — Config-only strategy DSL.** Express strategies purely as YAML (signals + weights +
constraints) with a generic engine. *Pros:* no code per strategy. *Cons:* premature — a DSL that
can express the SPY-overlay sizing and future crypto sleeves is a large speculative build; better to
grow it *out of* several concrete plugins than design it up front. Deferred (may emerge later).

### Fork 2 — Capital model

**Option A — Account-per-strategy (chosen).**

| Dimension | Assessment |
|---|---|
| Attribution | Native — account = sleeve, no virtual ledger |
| Isolation | Strong — one strategy can't consume another's margin or capital |
| Key management | Natural fit — one account = one key pair = per-strategy secret |
| Capital efficiency | Lower — no cross-margining; each account levers only its own slice |
| Operational surface | Higher — N reconciliations, N feeds, N funding decisions |

**Pros:** removes the single hardest problem of the alternative (the fill-attribution ledger and its
`Σ(sleeve) ≡ Alpaca` invariant); clean per-strategy risk and P&L straight from Alpaca; fail-isolation;
aligns 1:1 with per-strategy keys. **Accepted trade-offs:** no shared margin (a capital-efficiency
cost at 2× we accept for isolation); moving capital between strategies is an actual account transfer,
not a config change; N× operational surface. Multiple paper accounts under one login are available
(confirmed by owner, 2026-07-08).

**Option B — One account + attribution ledger.** *Pros:* shared margin, one reconciliation, capital
reallocation is a number change. *Cons:* the engine becomes the sole source of truth for the
per-sleeve split; every fill must be sleeve-tagged; a new failure class (attribution breaks) and a
split risk gate. Materially more engine complexity for capital efficiency the paper book doesn't need
yet. Rejected for now — revisit only if capital efficiency becomes the binding constraint.

**Option C — Broker sub-accounts (Alpaca Broker API).** *Pros:* isolation + a single integration.
*Cons:* the Broker API is a different, heavier product than the Trading API in use; over-scoped for a
proprietary book. Rejected.

### Fork 3 — Credential storage

**Option A — GCP Secret Manager (chosen).**

| Dimension | Assessment |
|---|---|
| At-rest exposure | None in app scope — not in Postgres or `pg_dump` |
| Rotation UX | New secret version = one action; native versioning |
| Audit | Cloud Audit Logs record every change for free |
| Fit | Reuses the Secret Manager setup already in `deploy/` |
| Coupling | Ties credential mgmt to GCP |

**Pros:** keeps the "no creds in source/YAML" rule intact; keys never touch the DB or backups;
GCP handles encryption, versioning, IAM, and audit. **Cons:** GCP-coupled; the dashboard needs an
IAM grant (owner action).

**Option B — Encrypted in Postgres.** A `credentials` table, values encrypted by a KEK held only in
the VM env. *Pros:* no GCP coupling. *Cons:* we own KEK management and must exclude the table from
`pg_dump`; a mistake puts live keys in a GCS backup. Rejected — reinvents Secret Manager, worse.

**Option C — Per-strategy `.env.<slug>` files.** Dashboard writes env files the engine loads.
*Pros:* closest to today, simplest. *Cons:* plaintext at rest on the box, no rotation history/audit,
weakest for eventual live keys. Rejected for the credential path (the model still loads a strategy's
resolved creds into process env at run time — just sourced from Secret Manager, not a file).

---

## Trade-off Analysis

The three decisions reinforce each other. **Account-per-strategy** is the pivot that makes the rest
cheap: it deletes the attribution ledger (the hardest part of the one-account design), and because
one Alpaca account means exactly one key pair, it makes **per-strategy credentials** a natural unit
rather than a bolted-on concept. The price paid is capital efficiency (no cross-margining) and a
higher operational surface — an acceptable trade for a platform that prioritizes *isolation and
simplicity of attribution* over squeezing margin, especially on paper.

The **Strategy interface** is non-negotiable and must land first: accounts, credentials, sleeves,
and a portfolio-of-strategies dashboard are all meaningless until "strategy" is a first-class,
registered thing. The main discipline is to extract the interface *without* redesigning the
optimizer, risk model, or overlay internals at the same time — wrap what works, prove the seam with
a genuinely different second strategy, then improve behind a stable boundary.

**Credentials** carry the only irreversible risk (leaking live trading keys), so handling stays
conservative from day one even though everything is paper: Secret Manager only (never Postgres, disk,
or backups), write-only UI, gated by the same `SEPI_EXEC_TOKEN` as the console, no payload in logs,
and the assistant never in the value path. The read-only status view ships first as a natural
increment — the per-strategy account view with no secret-handling at all.

---

## Consequences

**Becomes easier**
- Adding a strategy = writing a plugin; execution, reconcile, monitor, risk, backtest, alerts are reused.
- Backtesting any strategy — same `generate()` path as live, parity by construction.
- Per-strategy P&L, risk, and reporting — read natively per account, no attribution math.
- Key rotation and per-strategy account swaps — a dashboard action with an audit trail.
- The SPY overlay generalizes: it becomes its own strategy whose `TargetBook` is a function of another
  book's aggregate exposure — a reusable "portfolio overlay," not a hardwired post-step.

**Becomes harder / new surface**
- N accounts to fund, reconcile, feed, and schedule; capital reallocation is a real transfer.
- No cross-margining — total capital efficiency drops versus one levered account.
- A credential-management surface that will eventually hold live keys — gated by the same exec token
  as the console; write-only, never persisted, the assistant never in the value path.
- The dashboard must grow a portfolio-of-strategies view (aggregate book + per-strategy drill-down).

**To revisit**
- A config/DSL strategy definition (Fork 1 Option C) may emerge once several plugins exist.
- Cross-account risk aggregation (a total-exposure view across strategies) once N > 1 — a *reporting*
  addition, not a change to the account model.

---

## Action Items (build order — each an increment, sketch-before-build, owner greenlights)

**Phase A — Keystone (no new accounts, no credentials touched)**
1. [ ] Define `Strategy` protocol, `TargetBook`, `StrategyContext`, and a `strategies/` registry.
2. [ ] Refactor the current strategy into `strategies/low_beta_overwrite.py` (behavior-identical; tests pin parity).
3. [ ] Make `run_cycle` load the strategy by name; split `settings.yaml` into platform vs per-strategy namespaces.
4. [ ] Route the backtest through `strategy.generate()`; assert live≡backtest on the current strategy.
5. [ ] Prove the interface with a second, trivial strategy (e.g. a monthly ETF-trend sleeve, no overlay, no leverage).

**Phase B — Account-per-strategy plumbing**
6. [ ] `get_client(strategy)`; a `strategies` config/registry carrying `{impl, credential_ref, capital, leverage, schedule, enabled}`.
7. [ ] Per-strategy reconcile / monitor / risk-gate runs; per-strategy `cycle_key` and rebalance lease.
8. [ ] Create the second paper account (multiple paper accounts under one login confirmed available).

**Phase C — Credentials (Secret Manager)**
9. [ ] **Owner:** grant the VM service account `secretmanager.secretVersionAdder` (assistant supplies the exact `gcloud` line).
10. [ ] Engine reads `sepi-strategy-<slug>` at run time → in-process client; never persisted; never logged.
11. [ ] Dashboard **read-only** status panel first: per-strategy account, live connection health, masked key fingerprint.
12. [ ] Dashboard **write** form → `add_secret_version`, gated by the same `SEPI_EXEC_TOKEN` as the Execute console; write-only, masked fingerprint, no payload logged.

**Phase D — Platform dashboard**
13. [ ] Portfolio-of-strategies view: aggregate book + per-strategy drill-down, per-strategy attribution.

**Cross-cutting**
14. [ ] Re-express the SPY overlay as a `Strategy` whose target reads aggregate book exposure.
15. [ ] On acceptance, add the `D37` pointer to DECISIONS.md and update ARCHITECTURE.md's module map.

---

## Guardrails carried from existing constraints

- Paper until Phase 7 (`engine.config.is_paper_env`); the credential model must be live-grade from day one anyway.
- No credentials in source or YAML; the assistant never handles raw key values.
- New per-strategy secrets are **separate** from the legacy `alpaca-secret-key` (still at v2 — do not run `fetch_secrets.sh`).
- Deploys that restart `sharpe-eod` cancel working orders — restart only in a no-open-orders window.
