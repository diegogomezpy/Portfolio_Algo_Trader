# Execution — how the engine turns target weights into fills

The authoritative reference for order execution. Read alongside `ARCHITECTURE.md` (system shape)
and `DECISIONS.md` (why). If you change execution behavior, update this file in the same commit.

---

## 1. Philosophy

The fund rebalances **monthly** (first trading day, 13:00 ET), holds **~18–20 names** at
**~$200k × 2× leverage**, and its mandate is **capital preservation**. Two consequences drive
every execution choice:

- **We are not latency-sensitive.** We have the whole session, and a laggard can roll to the next
  day. So we should pay only as much spread/impact as necessary — patience is free.
- **We never chase a stale or phantom quote.** Thin names (e.g. INBX) can show a quote whose one
  side is a ghost (INBX's ask sat at \$108.87 while it traded ~\$95). Crossing to that "touch" is
  the classic ad-hoc mistake; we guard against it explicitly.

Every fill is judged by **implementation shortfall** against a spread-guarded **arrival price**
(§9), so the knobs below are tuned from real data, not guesswork.

---

## 2. Where it lives

| Concern | Module |
|---|---|
| Equity planning + working loop | `engine/execute.py` (`plan_orders`, `submit_and_track` → `_chase`) |
| Covered-call overlay execution | `engine/covered_calls.py` (`OptionChase`, `_execute_option_leg`) |
| Alpaca order I/O | `engine/broker.py` (`submit_order`, `submit_option_order`, `get_order`, `cancel_order`) |
| Orchestration + cross-day retry | `scripts/run_eod.py` (`run_cycle`, `daily_job`, `work_pending_adjustments`) |
| Position/order reconciliation | `engine/reconcile.py` (`reconcile`, `reconcile_orders`) |
| Slippage measurement | `dashboard/data.py` (`arrival_reference`, `_slippage_core`), `dashboard/app.py` (`_arrival_mid`) |

---

## 3. Lifecycle at a glance

```
13:00 ET ─► compute targets ─► risk gate ─► plan_orders (size / filter / sequence / tier)
         ─► [sells then buys]  work each order with the tiered tactic:
              deep+tight  → marketable now
              moderate    → patient ladder mid→touch (capped)
              thin/wide   → passive + slice + cap-the-cross
         ─► residual at close: closing auction (eligible) else roll to cross-day
         ─► reconcile positions + order statuses ─► measure shortfall
next days ─► cross-day catch-up completes deferred names until the book is filled
```

---

## 4. Sizing & filtering — `plan_orders`

- **Deployable base** = `target_leverage × account_equity` (live Alpaca equity, not the static
  config NAV). Weights are fractions of this base.
- **Target shares** = `floor(weight × base / price)` (whole shares; the rounding residual stays in
  cash). `delta = target − held`; `delta == 0` is skipped.
- **Minimum-trade filter:** a trade whose notional `< execution.min_trade_usd` (\$500) is **not
  sent** — it becomes a `pending_adjustments` row and rolls into the cross-day catch-up (§8).
- **Sequencing:** **sells first (descending notional), then buys (descending)** — sale proceeds
  fund the purchases (T+1 settlement is respected because Alpaca lets buys use unsettled proceeds
  within the day).

---

## 5. Liquidity tiers — the tactic selector

Each name is classified once, from **ADV** and the **quoted spread**, into the tactic it gets:

| Tier | Condition | Tactic |
|---|---|---|
| **Deep + tight** | `ADV ≥ large_cap_adv_threshold` (\$50M) **and** `spread < spread_threshold` (0.1%) | Send **market / marketable limit immediately** — spread is negligible, no reason to wait. |
| **Moderate** | `mid_cap_adv_threshold` (\$5M) `≤ ADV < \$50M`, or a wider-but-sane spread | **Patient ladder** (§7): start near the mid, step toward the touch over the session. |
| **Thin / wide** | `ADV < \$5M`, or a **pathological spread** (§6) | **Passive + sliced + cap-the-cross** (§6, §7). Never pay a blown-out spread; defer if needed. |

---

## 6. Price reference & the two caps (the INBX guard)

Working orders are **not** priced off the raw touch. They are priced off a robust reference, with
two ceilings:

1. **Reference = spread-guarded arrival price** (`arrival_reference(bid, ask, trade)`): the NBBO
   **mid** when the quote is two-sided and tight; otherwise the **last trade** print. A wide or
   one-sided quote makes the mid meaningless, so we anchor to a real execution instead.
2. **Cross cap** `max_cross_bps` (= `execution.marketable_limit_bps`, 50 = 0.5%): a marketable
   order is a limit at `reference × (1 ± max_cross_bps)` — buy above / sell below. We never cross
   more than this beyond fair value, so a bad print can't drag our limit far.
3. **Pathological-spread guard** `max_spread_bps` (**NEW**, default ~150 bps): if
   `(ask − bid) / mid > max_spread_bps`, the quote is treated as untrustworthy — we **do not cross
   at all**. We post passively at the reference and, if it doesn't fill, **defer** (§8) rather than
   pay the spread. INBX's ~1400 bps spread is never crossed.

---

## 7. The working loop — patient ladder + slicing

For a moderate/thin name the working order **ladders from the mid toward the touch** instead of
being marketable from the first second:

- **Round 0..k:** limit starts at (or just through) the **mid** and steps toward
  `reference × (1 ± max_cross_bps)` across rounds — capturing the half-spread on names that fill
  early, guaranteeing urgency as the session runs on.
- Each round: submit → poll `poll_attempts × poll_interval_s` → cancel the remainder → re-price →
  repeat. A fresh `client_order_id` per round (`…:{side}:r{n}`).
- **Close gating:** stop laddering once the session is within `close_buffer_s` of the close; the
  residual goes to the fallback (§8).
- **Deep+tight** names skip the ladder — straight to marketable (the spread isn't worth waiting on).

**Slicing (thin names only):** each child order is capped at `child_adv_pct` (**NEW**, ~10%) of the
name's ADV, so our own order doesn't move a thin book; the next slice goes after the prior one
fills. Liquid names trade in a single clip.

---

## 8. Residual fallback — how we still get invested

When a name isn't filled by the close gate:

- **Eligible (liquid enough) residual → the closing auction.** A **limit-on-close** at the cap
  (Alpaca TIF `CLS`) fills at the single closing print — the day's deepest, lowest-impact
  liquidity — instead of walking the intraday book.
- **Thin / pathological names → cross-day.** The residual becomes a `pending_adjustments` row; we'd
  rather be one name short for a day than pay a blown-out spread. There is **no naked intraday
  market order** (the old behavior is retired).

**Cross-day catch-up** (`work_pending_adjustments`, run by `daily_job` on non-rebalance days):
re-derive each deferred name's residual vs the still-current target, chase the ones still wanted,
mark them applied. A name deferred today is completed on a later trading day — until the next
monthly rebalance resets targets.

---

## 9. Covered-call overlay execution

The option overlay mirrors the equity philosophy (`_execute_option_leg`, driven by `OptionChase`):

- **Writes** (sell-to-open) chase to the **bid**; **closes** (buy-to-close) chase to the **ask**.
- The `options_lifecycle` row is written **only on a real fill, at the real fill price** — so
  premium accounting can never count premium that wasn't collected.
- **Closes** end in a final market sweep (get the risk off); **writes** leave a name uncovered
  rather than chase a bad option fill (an income miss, not a breach).

---

## 10. Idempotency & safety

- **`client_order_id`** = `{cycle}:{symbol}:{side}[:round]` (equities) / `cc:{date}:{underlying}:{event}:{tag}`
  (options). Alpaca rejects duplicate ids, so re-running a cycle never double-trades.
- **A restart cancels every open order.** `graceful_shutdown` calls `broker.cancel_all_orders()` on
  SIGTERM, so `deploy/update.sh` (which restarts `sharpe-eod`) must run only when **no orders rest**
  (after the close, or before 13:00 ET). Verify with a live `get_orders(status="open")` == 0 first.
- **Reconciliation:** `reconcile` corrects DB positions to Alpaca (Alpaca is truth); `reconcile_orders`
  syncs the `orders` table to Alpaca's current order statuses each cycle/day (blotter parity).

---

## 10.1 Slippage measurement

`slippage = fill − arrival_reference`, signed so **positive = adverse** (paid up on a buy / received
less on a sell), reported in bps of the reference and in dollars, notional-weighted. The reference
is the spread-guarded arrival price at submit (§6), reconstructed on the dashboard from historical
NBBO + the nearest trade. Benchmarking against our own marketable limit is wrong — it's padded to
the touch, so a fill inside it looks like a fake "gain" (this produced INBX's bogus −\$1,470).

---

## 11. Parameters (`config/settings.yaml → execution`)

| Knob | Meaning | Default |
|---|---|---|
| `min_trade_usd` | Below this notional a delta is deferred, not traded | 500 |
| `large_cap_adv_threshold` | ADV ≥ this ⇒ large-cap tier | 50M |
| `mid_cap_adv_threshold` | ADV ≥ this ⇒ mid-cap tier (below ⇒ thin) | 5M |
| `spread_threshold` | Spread < this ⇒ "tight" (deep+tight ⇒ market) | 0.001 (0.1%) |
| `marketable_limit_bps` | **Cross cap** — max cross beyond the reference | 50 (0.5%) |
| `max_spread_bps` | **NEW** — pathological-spread guard; above this we don't cross | ~150 |
| `ladder_start_bps` / `ladder_steps` | **NEW** — patient ladder start (from mid) + step count | tbd |
| `child_adv_pct` | **NEW** — slice cap as a fraction of ADV (thin names) | ~0.10 |
| `close_buffer_s` | Stop working this many seconds before the close | 300 |
| `poll_attempts` / `poll_interval_s` | Fill-poll cadence per round | 30 / 2s |
| `rebalance_hour_et` / `rebalance_minute_et` | When the daily job fires | 13:00 |

Backtest cost model (separate, `cost_bps_large/mid/small` = 5/10/20) estimates the half-spread by
ADV tier; it should stay consistent with the live tiers above.

---

## 12. Failure modes

| Situation | Behavior |
|---|---|
| Wide / phantom quote | `max_spread_bps` guard: don't cross; post passive, defer if unfilled |
| Not filled by the close | Closing auction (eligible) else cross-day retry |
| Order rejected (permanent) | Logged + alerted, rolled to `pending_adjustments` |
| Transient poll/read hiccup | Order left open, re-polled; reconcile corrects from Alpaca |
| Alpaca unreachable | `reconcile` blocks the pipeline (no trading on stale state) |

---

## 13. Implementation status

- **Live today:** sizing, min-trade defer, sells-then-buys sequencing, cross-to-touch chase with
  a close buffer, cross-day catch-up, the covered-call chase (§9), reconciliation (§10), and the
  arrival-price slippage benchmark (§10.1).
- **Landing in the execution redesign (this change):** liquidity-tiered tactics (§5), the
  spread-guarded reference for limit pricing + the `max_spread_bps` pathological guard (§6), the
  patient mid→touch ladder (§7), thin-name slicing (`child_adv_pct`, §7), the closing-auction
  residual fallback (§8), and **retiring the naked intraday market order**. Deployed only in a
  no-open-orders window (§10).
