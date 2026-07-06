"""Order execution — turn approved target weights into Alpaca orders.

Pure planner + I/O, like the rest of the engine:

* :func:`plan_orders` — target weights + live positions + prices → a sized, sequenced,
  min-trade-filtered order list (plus the sub-threshold deltas to defer). No network, no
  DB; fully unit-tested. **This is increment 3.3a — all the order *logic* lives here.**
* ``submit_and_track`` (increment 3.3b) — idempotent submission via :class:`engine.broker
  .Broker`, fill polling, session-end cancel, and DB persistence.

**Sizing (DECISIONS — whole shares).** Target shares = ``floor(weight × NAV / price)``;
the sub-share rounding residual simply stays in cash. ``NAV`` is the live Alpaca account
equity (passed in by the driver), not the static ``settings.portfolio.nav``.

**Sequencing (ARCHITECTURE).** Equity sells (descending by notional) before buys
(descending), so sale proceeds fund the purchases. Force-closing expiring options and
covered-call writes are Phase 4 — equities only here.

**Order type (ARCHITECTURE).** Market if ``ADV ≥ large_cap_adv_threshold`` and
``spread < spread_threshold`` (deep, tight names), else a limit at the mid price.

**Minimum trade filter.** A trade whose notional is below ``execution.min_trade_usd`` is
not sent; it is emitted as a ``pending_adjustment`` that accumulates and rolls into the
next rebalance, exactly like a sub-threshold delta.
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Callable, Mapping, Optional, Sequence

import pandas as pd

from engine.alpaca_client import AlpacaAPIError
from engine.logger import get_logger

log = get_logger(__name__)

# Order states from which no further fill is possible (poll stops here).
_TERMINAL = {"filled", "canceled", "cancelled", "rejected", "expired", "done_for_day"}

# HTTP statuses that mean "try again shortly", not "this order is unplaceable".
_TRANSIENT_HTTP = {408, 429, 500, 502, 503, 504}


def is_transient_error(exc) -> bool:
    """Whether a broker/API failure is worth retrying next round rather than deferring the
    name for the day: rate limits, server hiccups, and **insufficient buying power** — which
    self-resolves as this cycle's sells fill (sells post first; treating a BP reject as
    permanent was deferring names a round-two retry would have completed)."""
    if getattr(exc, "status_code", None) in _TRANSIENT_HTTP:
        return True
    msg = str(exc).lower()
    return any(t in msg for t in ("buying power", "too many requests", "rate limit",
                                  "timeout", "timed out", "temporarily unavailable"))


# Operator stage control (2026-07-06, Diego): the dashboard's "express-finish" button sets this
# override; the stage CURRENTLY chasing consumes it at its next round boundary and jumps to its
# guaranteed endgame — the equity chase sweeps residuals at market, option closes jump to the
# market sweep, writes jump to the touch, the spread write drops to its credit floor. One press
# finishes ONE stage (press again for the next); every fresh cycle clears it first, so a stale
# press can never express a run that hasn't started yet.
_XFIN_KEY = "express_finish"
_XFIN_MAX_AGE_S = 3600.0


def express_finish_requested(db_engine) -> bool:
    """Whether the operator pressed express-finish. Best-effort AND freshness-gated: an
    unreadable DB or a press older than an hour reads False — this can never break a chase."""
    if db_engine is None:
        return False
    try:
        from engine import overrides
        rec = overrides.info(db_engine, _XFIN_KEY)
        if not rec or not rec.get("value"):
            return False
        ts = rec.get("updated_at")
        if ts:
            try:
                dt = datetime.fromisoformat(str(ts))
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                if (datetime.now(timezone.utc) - dt).total_seconds() > _XFIN_MAX_AGE_S:
                    return False
            except ValueError:
                pass
        return True
    except Exception:  # noqa: BLE001 — advisory control; never disturb execution
        return False


def clear_express_finish(db_engine) -> None:
    """Consume (or pre-emptively clear) the express-finish press. No-op on any failure."""
    if db_engine is None:
        return
    try:
        from engine import overrides
        if overrides.get(db_engine, _XFIN_KEY) is not None:
            overrides.clear(db_engine, _XFIN_KEY)
    except Exception:  # noqa: BLE001
        pass


def _settle_after_cancel(read, order_id: str, sleep, *, attempts: int = 8,
                         interval_s: float = 0.5):
    """Post-cancel read: poll (bounded) until the order reaches a terminal state before
    trusting ``filled_qty``. Alpaca cancels are async — an immediate read can catch the order
    in ``pending_cancel`` with fills still landing; booking that undercount makes the caller
    re-post an oversized residual (the overfill race). Returns the last readable state or
    ``None``."""
    st = None
    for i in range(attempts):
        try:
            st = read(order_id) or st
        except AlpacaAPIError:
            pass                                     # transient read — keep the last good state
        if st is not None and st.get("status") in _TERMINAL:
            return st
        if i < attempts - 1:
            sleep(interval_s)
    return st


@dataclass
class PlannedOrder:
    """One equity order to submit: whole-share ``qty``, ``side`` buy/sell."""
    symbol: str
    side: str                       # "buy" | "sell"
    qty: int                        # whole shares (> 0)
    order_type: str                 # "market" | "limit"
    limit_price: Optional[float] = None
    notional: float = 0.0           # qty × price at planning time (for sequencing/audit)


# ---------------------------------------------------------------------------- #
# Execution tactics — pure decision helpers (docs/EXECUTION.md §5–§7).
# Liquidity tier → tactic; a spread-guarded reference; two price ceilings; ADV slicing.
# Kept side-effect-free so they unit-test on plain numbers.
# ---------------------------------------------------------------------------- #
# Reference/guard defaults, used when a settings knob is absent (so the helpers work standalone).
_ARRIVAL_MAX_SPREAD = 0.02        # trust the mid only within 2% of it, else the trade print
_DEFAULT_MAX_SPREAD_BPS = 150.0   # above this quoted spread we refuse to cross (pathological)


def arrival_reference(bid, ask, trade, *, max_spread: float = _ARRIVAL_MAX_SPREAD):
    """The 'arrival price' to judge/anchor a fill against — robust to stale / one-sided quotes.

    The NBBO **mid** when the quote is two-sided and tight (spread ≤ ``max_spread`` of the mid);
    otherwise the **last trade** price (a real executed value). A wide or one-sided quote — common
    on thin names via the IEX feed, e.g. INBX's phantom $108.87 ask while it traded ~$95 — makes
    the mid meaningless, so we trust the print instead. Returns ``None`` if nothing is usable.
    Shared by the execution pricer and the dashboard slippage benchmark so both agree.
    """
    bid = float(bid) if bid and float(bid) > 0 else None
    ask = float(ask) if ask and float(ask) > 0 else None
    trade = float(trade) if trade and float(trade) > 0 else None
    if bid and ask and ask >= bid:
        mid = (bid + ask) / 2.0
        if (ask - bid) / mid <= max_spread:
            return mid
        return trade if trade else mid          # wide quote → trust the executed print
    return trade if trade else (bid or ask)     # one-sided / no quote → trade, else the lone side


def spread_frac(bid, ask) -> Optional[float]:
    """Fractional quoted spread ``(ask − bid) / mid``, or ``None`` if not a valid two-sided quote."""
    bid = float(bid) if bid and float(bid) > 0 else None
    ask = float(ask) if ask and float(ask) > 0 else None
    if bid is None or ask is None or ask < bid:
        return None
    mid = (bid + ask) / 2.0
    return (ask - bid) / mid if mid else None


def liquidity_tier(adv, bid, ask, *, ex) -> str:
    """``"deep"`` / ``"moderate"`` / ``"thin"`` from ADV + quoted spread (docs/EXECUTION.md §5)."""
    a = float(adv or 0.0)
    s = spread_frac(bid, ask)
    if a >= ex.large_cap_adv_threshold and s is not None and s < ex.spread_threshold:
        return "deep"
    if a >= getattr(ex, "mid_cap_adv_threshold", 0.0):
        return "moderate"
    return "thin"


def is_pathological_spread(bid, ask, *, ex) -> bool:
    """True when the quoted spread is too wide to trust — we refuse to cross it (the INBX guard)."""
    s = spread_frac(bid, ask)
    cap = float(getattr(ex, "max_spread_bps", _DEFAULT_MAX_SPREAD_BPS) or _DEFAULT_MAX_SPREAD_BPS) / 1e4
    return s is not None and s > cap


def marketable_price(reference: float, side: str, *, ex) -> float:
    """A marketable limit: the reference crossed toward the touch by ``marketable_limit_bps`` (the
    cross cap) — buy above / sell below. Never crosses more than the cap beyond fair value."""
    cap = float(getattr(ex, "marketable_limit_bps", 0.0) or 0.0) / 1e4
    mult = (1.0 + cap) if side == "buy" else (1.0 - cap)
    return round(float(reference) * mult, 2)


def ladder_price(reference: float, mid: float, side: str, step: float, *, ex) -> float:
    """Patient ladder price: interpolate from the ``mid`` (``step=0``) to the marketable cap
    (``step=1``). Early rounds sit near the mid to capture the half-spread; later rounds cross to
    guarantee the fill (docs/EXECUTION.md §7). ``step`` is clamped to ``[0, 1]``."""
    target = marketable_price(reference, side, ex=ex)
    step = min(max(float(step), 0.0), 1.0)
    return round(float(mid) + (target - float(mid)) * step, 2)


def child_qty(total: int, adv, price, *, ex) -> int:
    """Slice cap for thin names: at most ``child_adv_pct`` of ADV per child order, so our own
    order doesn't move the book. Returns ``total`` unchanged when no cap applies (docs §7)."""
    pct = getattr(ex, "child_adv_pct", None)
    total = int(total)
    if not pct or not adv or not price:
        return total
    cap = int((float(pct) * float(adv)) / float(price))
    return max(1, min(total, cap)) if cap > 0 else total


def _fresh_trade(price, ts, stale_max_s) -> Optional[float]:
    """The trade price, or ``None`` when the print is older than ``stale_max_s`` — a stale
    print anchoring the chase is the INBX phantom quote in mirror image."""
    if price is None:
        return None
    if ts is not None and stale_max_s:
        try:
            age = (datetime.now(timezone.utc) - ts).total_seconds()
        except TypeError:                            # naive/foreign timestamp — trust the print
            return float(price)
        if age > float(stale_max_s):
            return None
    return float(price)


def quote_fn_for(client, *, stale_trade_max_s: float | None = None):
    """Build the per-symbol ``(bid, ask, trade)`` quote fn for the tiered chase, or ``None``
    when the client has no NBBO surface (→ single-pass fallback).

    Uses the RAW quote (no trade fallback — the spread guard must see missing sides) and a
    freshness-gated last print. Shared by the rebalance driver and the manual console, which
    used to carry duplicate copies.
    """
    if not hasattr(client, "latest_nbbo"):
        return None

    def _q(sym):
        try:
            try:
                bid, ask = client.latest_nbbo(sym, fallback_to_trade=False)
            except TypeError:                        # a client without the raw-quote flag
                bid, ask = client.latest_nbbo(sym)
        except AlpacaAPIError:
            bid = ask = None
        price = ts = None
        try:
            if hasattr(client, "latest_trade_at"):
                price, ts = client.latest_trade_at(sym)
            else:
                price = client.latest_trade(sym)
        except AlpacaAPIError:
            price = None
        return bid, ask, _fresh_trade(price, ts, stale_trade_max_s)
    return _q


def batch_quote_fn(client, *, stale_trade_max_s: float | None = None):
    """Build the per-round batched quote map fn (``[symbols] -> {sym: (bid, ask, trade)}``),
    or ``None`` when the client lacks batch surfaces.

    One multi-symbol NBBO call + one trades call per round instead of one pair per name —
    the difference between ~360 req/min at full book size (over Alpaca's 200/min limit,
    where the 429s used to become permanent rejects) and ~10. Returns ``None`` from a round
    when the batch itself fails, so the chase falls back to per-symbol quotes for that round.
    """
    if not (hasattr(client, "latest_nbbo_batch") and hasattr(client, "latest_trades_batch")):
        return None

    def _batch(symbols):
        try:
            quotes = client.latest_nbbo_batch(symbols) or {}
        except AlpacaAPIError as exc:
            log.warning("batch quote failed; per-symbol fallback this round",
                        extra={"error": str(exc)})
            return None
        try:
            trades = client.latest_trades_batch(symbols) or {}
        except AlpacaAPIError:
            trades = {}
        out = {}
        for s in symbols:
            bid, ask = quotes.get(s) or (None, None)
            price, ts = trades.get(s) or (None, None)
            out[s] = (bid, ask, _fresh_trade(price, ts, stale_trade_max_s))
        return out
    return _batch


def _order_type_for(
    symbol: str, price: float, side: str, *,
    adv: Mapping[str, float], spread: Mapping[str, float], ex
) -> tuple[str, Optional[float]]:
    """Market for deep+tight names, else a **marketable** limit (ARCHITECTURE).

    ``spread`` is the fractional bid/ask (or high-low proxy) per symbol; a name is sent
    market only when it is both deep (ADV ≥ large-cap threshold) and tight (spread <
    ``spread_threshold``). Otherwise a limit is placed at the mid **crossed by up to
    ``marketable_limit_bps``** in the trade's direction (buy above / sell below) — a price ceiling
    that lets the order fill at the touch in-session rather than resting passively at the mid (a
    passive mid limit often never fills before the session-end cancel). With ``marketable_limit_bps``
    absent or 0 this is exactly the old mid limit.
    """
    a = float(adv.get(symbol, 0.0) or 0.0)
    s = spread.get(symbol)
    deep_and_tight = a >= ex.large_cap_adv_threshold and (s is not None and float(s) < ex.spread_threshold)
    if deep_and_tight:
        return "market", None
    cap = float(getattr(ex, "marketable_limit_bps", 0.0) or 0.0) / 1e4
    mult = (1.0 + cap) if side == "buy" else (1.0 - cap)
    return "limit", round(float(price) * mult, 2)


def plan_orders(
    target_weights: pd.Series,
    live_positions: Mapping[str, float],
    prices: Mapping[str, float],
    *,
    nav: float,
    settings,
    adv: Mapping[str, float] | None = None,
    spread: Mapping[str, float] | None = None,
    mid_prices: Mapping[str, float] | None = None,
) -> tuple[list[PlannedOrder], list[dict]]:
    """Plan the equity orders that move ``live_positions`` to ``target_weights``.

    Args:
        target_weights: approved (post-risk-gate) target weights, fractions of NAV.
        live_positions: ``{symbol: qty}`` currently held (from reconcile; Alpaca truth).
        prices: ``{symbol: last_price}`` used to size shares.
        nav: live account equity (target dollars = weight × nav).
        adv / spread: per-symbol liquidity for the order-type rule (default empty).
        mid_prices: per-symbol mid for limit pricing (defaults to ``prices``).

    Returns:
        ``(orders, pending)`` — ``orders`` sequenced sells-then-buys (each descending by
        notional); ``pending`` is a list of ``pending_adjustments`` dicts (sub-min-trade
        deltas and any name we can't price an exit for) to roll into the next cycle.
    """
    ex = settings.execution
    adv = adv or {}
    spread = spread or {}
    mids = mid_prices or prices

    names = set(target_weights.index) | set(live_positions)
    orders: list[PlannedOrder] = []
    pending: list[dict] = []

    for sym in names:
        px = prices.get(sym)
        cur_shares = int(round(float(live_positions.get(sym, 0.0))))
        if px is None or float(px) <= 0:
            # Can't size without a price. If we hold it, flag the exit; else skip.
            if cur_shares:
                pending.append({"symbol": sym, "side": "sell", "delta_usd": None,
                                "qty": None, "reason": "no price available to size exit"})
            continue
        px = float(px)

        tgt_w = float(target_weights.get(sym, 0.0))
        tgt_shares = int(math.floor(tgt_w * nav / px)) if tgt_w > 0 else 0
        delta = tgt_shares - cur_shares
        if delta == 0:
            continue

        side = "buy" if delta > 0 else "sell"
        qty = abs(delta)
        notional = qty * px
        if notional < ex.min_trade_usd:
            pending.append({"symbol": sym, "side": side, "delta_usd": round(notional, 2),
                            "qty": qty, "reason": f"below min_trade_usd ({ex.min_trade_usd})"})
            continue

        otype, lp = _order_type_for(sym, mids.get(sym, px), side, adv=adv, spread=spread, ex=ex)
        orders.append(PlannedOrder(sym, side, qty, otype, lp, round(notional, 2)))

    # Sells (desc by notional) before buys (desc), so proceeds fund purchases.
    sells = sorted((o for o in orders if o.side == "sell"), key=lambda o: -o.notional)
    buys = sorted((o for o in orders if o.side == "buy"), key=lambda o: -o.notional)
    ordered = sells + buys
    log.info("order plan built",
             extra={"orders": len(ordered), "sells": len(sells), "buys": len(buys),
                    "deferred": len(pending), "nav": float(nav)})
    return ordered, pending


# ====================================================================== #
# I/O — idempotent submission, fill polling, persistence (increment 3.3b)
# ====================================================================== #
@dataclass
class ExecReport:
    """Outcome of one submit-and-track pass."""
    submitted: int
    filled: int
    partial: int
    rejected: int
    deferred: int          # pending_adjustments rolled to the next cycle
    auctioned: int = 0     # residual routed to the closing auction (LOC); fills at the 4pm print
    # Per-symbol outcome for the post-rebalance breakdown:
    # {symbol, side, qty, filled, status ∈ filled/partial/deferred/rejected/auction, reason}.
    lines: list = field(default_factory=list)


def submit_and_track(
    orders: Sequence[PlannedOrder],
    *,
    broker,
    db_engine,
    cycle_key: str,
    pending: list[dict] | None = None,
    poll_attempts: int = 30,
    poll_interval_s: float = 2.0,
    sleep: Callable[[float], None] = time.sleep,
    quote: Callable[[str], tuple] | None = None,
    adv: Mapping[str, float] | None = None,
    ex=None,
    now: Callable[[], datetime] | None = None,
    market_close: datetime | None = None,
    order_state: Callable[[str], dict] | None = None,
    alert: Callable[[str], None] | None = None,
    cancel_leftover: bool = True,
    quote_batch: Callable[[Sequence[str]], dict] | None = None,
) -> ExecReport:
    """Submit ``orders``, poll fills, persist to DB, and return a per-symbol :class:`ExecReport`.

    Two modes:

    * **Tiered** (``quote`` provided — the production path, docs/EXECUTION.md §5–§8): each
      round prices every still-unfilled name by its liquidity tier and walks the patient
      ladder; residual at the close → auction or cross-day.
    * **Single pass** (``quote is None``) — submit once (idempotent ``client_order_id =
      "{cycle}:{symbol}:{side}"``), poll up to ``poll_attempts``, cancel any leftover, roll the
      unfilled residual to ``pending_adjustments``. The no-quote fallback (a client without an
      NBBO surface). The old touch-chase middle mode was removed in the 2026-07 audit (no
      production caller since the tiered redesign).

    ``pending`` carries the planner's sub-min-trade deltas, persisted alongside. ``sleep`` is
    injectable so tests run instantly. ``ExecReport.lines`` lists each name's outcome.

    ``order_state(order_id) -> dict`` is where fill state is read from — the live order feed
    (:class:`engine.order_feed.LiveOrderFeed`) when available (instant, no REST/rate-limit), else
    it defaults to ``broker.get_order`` (the pre-feed behaviour). Submits/cancels always go
    through ``broker``.

    ``cancel_leftover=False`` (single-pass only) leaves unfilled orders **working** instead of
    cancelling them at poll end — the console's express mode off-hours: market orders queue and
    fill at the next open, reported as ``queued`` rather than rolled to ``pending_adjustments``.

    ``quote_batch`` (tiered only) prefetches the whole round's ``(bid, ask, trade)`` map in one
    request (see :func:`batch_quote_fn`); ``quote`` remains the per-symbol fallback.
    """
    pending = list(pending or [])
    read = order_state or broker.get_order
    out = {o.symbol: {"symbol": o.symbol, "side": o.side, "qty": int(o.qty),
                      "filled": 0, "status": "deferred", "reason": ""} for o in orders}
    if quote is not None:                              # tiered redesign (docs/EXECUTION.md §5–§8)
        return _tiered(orders, broker=broker, db_engine=db_engine, cycle_key=cycle_key,
                       pending=pending, out=out, quote=quote, adv=adv or {}, ex=ex, read=read,
                       now=now or (lambda: datetime.now(timezone.utc)), market_close=market_close,
                       sleep=sleep, alert=alert, quote_batch=quote_batch)
    return _single_pass(orders, broker=broker, db_engine=db_engine, cycle_key=cycle_key,
                        pending=pending, out=out, poll_attempts=poll_attempts,
                        poll_interval_s=poll_interval_s, sleep=sleep, read=read, alert=alert,
                        cancel_leftover=cancel_leftover)


def _finalize(out, orders, *, submitted, rejected, pending, db_engine, cycle_key) -> ExecReport:
    """Derive per-symbol statuses from accumulated fills, write pending, build the ExecReport."""
    if pending:
        _write_pending(db_engine, pending)
    by_qty = {o.symbol: int(o.qty) for o in orders}
    filled = partial = 0
    for sym, d in out.items():
        if d["status"] in ("rejected", "auction", "queued"):   # terminal states set by the caller
            continue
        q, f = by_qty.get(sym, 0), d["filled"]
        if f >= q and f > 0:
            d["status"] = "filled"; filled += 1
        elif f > 0:
            d["status"] = "partial"; d["reason"] = d["reason"] or "partial fill; residual deferred"; partial += 1
        else:
            d["status"] = "deferred"; d["reason"] = d["reason"] or "unfilled"
    auctioned = sum(1 for d in out.values() if d["status"] == "auction")
    report = ExecReport(submitted=submitted, filled=filled, partial=partial,
                        rejected=rejected, deferred=len(pending), auctioned=auctioned,
                        lines=list(out.values()))
    log.info("execution complete",
             extra={k: v for k, v in vars(report).items() if k != "lines"} | {"cycle": cycle_key})
    return report


def _tiered(orders, *, broker, db_engine, cycle_key, pending, out, quote, adv, ex, read,
            now, market_close, sleep, alert, max_rounds: int = 500,
            quote_batch=None) -> ExecReport:
    """Liquidity-tiered, patient-then-guaranteed equity execution (docs/EXECUTION.md §5–§8).

    Per round, each still-unfilled name is priced by its **tier**: *deep+tight* → a marketable
    limit (cross cap) that fills at once; *moderate/thin* → a **ladder** whose limit walks from the
    mid to the cap as the session elapses (cadence ``equity_repeg_s``); a **pathological** spread
    → a passive limit at the guarded reference that never crosses. Thin names post one child at a
    time (``child_adv_pct`` of ADV). Fills are read from ``read`` (the live feed). At
    ``close_buffer_s`` before ``market_close`` the residual is routed to the **closing auction**
    (limit-on-close) for liquid, non-pathological names, else rolled to the cross-day queue —
    there is no naked market order.

    2026-07 hardening (execution-algo review):

    * **Persistent orders** — a resting limit is left WORKING across rounds while its ladder
      price is unchanged; it is cancelled only to move the price. The old cancel/re-post every
      round surrendered queue priority at the level ~60×/session and tripled REST volume.
    * **Settle-wait** — every cancel is followed by a poll to a terminal state before
      ``filled_qty`` is booked (an immediate read races in-flight fills → oversized residual).
      Fills are booked as **deltas per order id**, so partials on a resting order accrue safely.
    * **Transient-reject retry** — 429/5xx/insufficient-buying-power submit failures retry next
      round (names post in planner order, sells first, so buying power frees up as sells fill)
      instead of deferring the name for the day.
    * **Chase cap** — no limit is posted beyond ``max_chase_bps`` of the name's round-1 arrival
      reference. The marketable cap re-anchors to the moving market each round, so in a trend
      the give-up vs decision price was otherwise unbounded.
    * **Adaptive ladder** — a name behind the time schedule steps up one rung early; a mid that
      has drifted adversely past half the chase cap jumps straight to marketable (passive
      posting into a runaway price is pure adverse selection).
    * **Batched quotes** — ``quote_batch`` prefetches the round's quote map in one request.
    """
    adv = adv or {}

    def _num(name, default):                               # keep a legit 0 (don't let `or` eat it)
        v = getattr(ex, name, None)
        return default if v is None else v
    repeg_s = float(_num("equity_repeg_s", 30.0))
    close_buffer_s = float(_num("close_buffer_s", 300.0))
    tick_s = float(_num("poll_interval_s", 2.0)) or 2.0    # divisor below → must be > 0
    ladder_steps = max(int(_num("ladder_steps", 3)), 1)
    cap_frac = float(_num("max_chase_bps", 150.0)) / 1e4   # hard ceiling vs round-1 arrival
    transient_max = 10                                     # rounds of transient failures → defer
    by_sym = {o.symbol: o for o in orders}
    seq = [o.symbol for o in orders]                       # planner order: sells fund the buys
    active = {o.symbol for o in orders}
    submitted_n = 0
    rejected = 0
    start = now()
    window_end = (market_close - timedelta(seconds=close_buffer_s)) if market_close else None
    window_s = (window_end - start).total_seconds() if window_end else None
    tier_of: dict[str, str] = {}
    patho_of: dict[str, bool] = {}
    arrival: dict[str, float] = {}                         # round-1 reference — the chase-cap anchor
    working: dict[str, dict] = {}                          # sym -> {"oid", "price"} resting order
    booked: dict[str, int] = {}                            # order_id -> fills already credited
    recorded: set[str] = set()                             # order_ids with a fills row written
    settled: set[str] = set()                              # order_ids with a settle event emitted
    transient: dict[str, int] = {}

    def _book(sym: str, st: dict) -> None:
        """Credit new fills (delta vs already-booked) and record the fills row once terminal."""
        oid = st.get("id")
        if not oid:
            return
        fq = int(st.get("filled_qty") or 0)
        d = fq - booked.get(oid, 0)
        if d > 0:
            booked[oid] = fq
            out[sym]["filled"] += d
        if st.get("status") in _TERMINAL and fq > 0 and oid not in recorded:
            _record_fill(db_engine, st)
            recorded.add(oid)

    def _settle_event(sym: str, st: dict, tag: str) -> None:
        oid = st.get("id")
        if not oid or oid in settled:
            return
        settled.add(oid)
        _emit_chase_event(db_engine, cycle_key=cycle_key, rnd=tag, symbol=sym,
                          side=by_sym[sym].side, event="settle", tier=tier_of.get(sym),
                          limit_price=st.get("limit_price"), filled_qty=out[sym]["filled"],
                          target_qty=by_sym[sym].qty, status=st.get("status"), order_id=oid)

    def _cap_px(sym: str, side: str, price: float) -> float:
        """Clamp a candidate limit to the chase cap vs arrival — bounded shortfall by design."""
        anchor = arrival.get(sym)
        if anchor is None or not cap_frac:
            return price
        ceil_px = round(anchor * ((1.0 + cap_frac) if side == "buy" else (1.0 - cap_frac)), 2)
        return min(price, ceil_px) if side == "buy" else max(price, ceil_px)

    def _refresh(sym: str, tag: str) -> None:
        """Read + book the resting order; release it from ``working`` once terminal."""
        w = working.get(sym)
        if w is None:
            return
        try:
            st = read(w["oid"])
        except AlpacaAPIError:
            return
        if st is None:
            return
        _upsert_order(db_engine, cycle_key, st)
        _book(sym, st)
        if st.get("status") in _TERMINAL:
            working.pop(sym, None)
            _settle_event(sym, st, tag)

    def _pull(sym: str, tag: str) -> None:
        """Cancel the resting order, settle-wait, book the final fill state."""
        w = working.pop(sym, None)
        if w is None:
            return
        try:
            broker.cancel_order(w["oid"])
        except AlpacaAPIError as exc:
            log.warning("cancel failed", extra={"id": w["oid"], "error": str(exc)})
        st = _settle_after_cancel(read, w["oid"], sleep)
        if st is None:
            return
        _upsert_order(db_engine, cycle_key, st)
        _book(sym, st)
        _settle_event(sym, st, tag)

    def _price(sym: str, pre: Optional[dict]) -> tuple[Optional[float], Optional[str], Optional[dict]]:
        """(limit_price, tier, ctx) for this round, or (None, tier, None) when it can't be priced
        now. ``ctx`` carries the round's ``bid``/``ask``/``mid`` for the chase visualizer."""
        o = by_sym[sym]
        if pre is not None and sym in pre:
            bid, ask, trade = pre[sym]
        else:
            try:
                bid, ask, trade = quote(sym)
            except AlpacaAPIError as exc:
                log.warning("quote failed; skipping this round", extra={"symbol": sym, "error": str(exc)})
                return None, tier_of.get(sym), None
        ref = arrival_reference(bid, ask, trade)
        if ref is None:
            return None, tier_of.get(sym), None
        arrival.setdefault(sym, float(ref))
        tier = liquidity_tier(adv.get(sym), bid, ask, ex=ex)
        tier_of[sym] = tier
        patho_of[sym] = is_pathological_spread(bid, ask, ex=ex)
        mid = (bid + ask) / 2.0 if (bid and ask and ask >= bid) else float(ref)
        ctx = {"bid": bid, "ask": ask, "mid": mid}
        if patho_of[sym]:
            return round(float(ref), 2), tier, ctx               # passive at the guarded ref; never cross
        if tier == "deep":
            return _cap_px(sym, o.side, marketable_price(ref, o.side, ex=ex)), tier, ctx
        f = min(max((now() - start).total_seconds() / window_s, 0.0), 1.0) if window_s and window_s > 0 else 1.0
        level = round(f * ladder_steps) / ladder_steps           # discretize mid→cap into ladder_steps
        fill_frac = out[sym]["filled"] / o.qty if o.qty else 1.0
        if fill_frac + 0.25 < f:                                 # behind schedule → one rung early
            level = min(level + 1.0 / ladder_steps, 1.0)
        anchor = arrival[sym]
        drift = (mid - anchor) / anchor if anchor else 0.0
        if (drift if o.side == "buy" else -drift) > cap_frac / 2.0:
            level = 1.0                                          # runaway price → stop being patient
        return _cap_px(sym, o.side, ladder_price(ref, mid, o.side, level, ex=ex)), tier, ctx

    def _round(tag: str) -> None:
        nonlocal submitted_n, rejected
        pre = None
        if quote_batch is not None:
            try:
                pre = quote_batch([s for s in seq if s in active])
            except Exception as exc:  # noqa: BLE001 — the batch is an optimization, never fatal
                log.warning("quote batch failed; per-symbol fallback", extra={"error": str(exc)})
                pre = None
        posted = False
        for sym in [s for s in seq if s in active]:              # sells first (funding order)
            o = by_sym[sym]
            _refresh(sym, tag)
            residual = o.qty - out[sym]["filled"]
            if residual <= 0:
                _pull(sym, tag)                                  # overshoot safety: want no more
                active.discard(sym)
                continue
            price, tier, ctx = _price(sym, pre)
            if price is None:
                continue                                         # unpriceable now — keep any resting order
            w = working.get(sym)
            if w is not None:
                if abs(w["price"] - float(price)) < 0.005:
                    continue                                     # same tick — stay in the queue
                _pull(sym, tag)                                  # move the price: cancel + settle first
                residual = o.qty - out[sym]["filled"]
                if residual <= 0:
                    active.discard(sym)
                    continue
            qty_to_post = child_qty(residual, adv.get(sym), price, ex=ex) if tier == "thin" else residual
            coid = f"{cycle_key}:{sym}:{o.side}:{tag}"
            try:
                resp = broker.submit_order(sym, qty_to_post, o.side, order_type="limit",
                                           limit_price=price, client_order_id=coid)
            except AlpacaAPIError as exc:
                if is_transient_error(exc) and transient.get(sym, 0) < transient_max:
                    transient[sym] = transient.get(sym, 0) + 1
                    log.warning("transient submit failure; retrying next round",
                                extra={"symbol": sym, "attempt": transient[sym], "error": str(exc)})
                    continue
                rejected += 1
                out[sym].update(status="rejected", reason=f"rejected: {exc}")
                pending.append({"symbol": sym, "side": o.side, "delta_usd": None,
                                "qty": residual, "reason": f"rejected: {exc}"})
                log.error("order rejected; deferring", extra={"symbol": sym, "error": str(exc)})
                _emit_chase_event(db_engine, cycle_key=cycle_key, rnd=tag, symbol=sym, side=o.side,
                                  event="reject", tier=tier, ctx=ctx, limit_price=price,
                                  qty=residual, filled_qty=out[sym]["filled"], target_qty=o.qty)
                if alert:
                    alert(f"order rejected {sym} {o.side} {residual}: {exc}")
                active.discard(sym)
                continue
            transient.pop(sym, None)
            posted = True
            submitted_n += 1
            _upsert_order(db_engine, cycle_key, resp)
            _emit_chase_event(db_engine, cycle_key=cycle_key, rnd=tag, symbol=sym, side=o.side,
                              event="post", tier=tier, ctx=ctx, limit_price=price, qty=qty_to_post,
                              filled_qty=out[sym]["filled"], target_qty=o.qty, order_id=resp["id"])
            working[sym] = {"oid": resp["id"], "price": float(price)}
            booked.setdefault(resp["id"], 0)

        if not working:
            if not posted and active:
                sleep(tick_s)                                    # nothing to watch — don't spin hot
            return
        for _ in range(max(int(repeg_s / tick_s), 1)):           # poll window: book fills live
            sleep(tick_s)
            for sym in list(working):
                _refresh(sym, tag)
                if out[sym]["filled"] >= by_sym[sym].qty:
                    active.discard(sym)
            if not working:
                break

    def _express_sweep(tag: str) -> None:
        """Operator escape hatch (the express-finish button): stop being patient — pull every
        resting limit and sweep the residuals with MARKET orders. A sweep that doesn't confirm
        within ~a minute is left WORKING (status ``queued``), never cancelled — the guarantee
        stands even if the poll misses the fill."""
        nonlocal submitted_n, rejected
        swept: list[tuple[str, str]] = []
        for sym in [s for s in seq if s in active]:
            o = by_sym[sym]
            _pull(sym, tag)
            residual = o.qty - out[sym]["filled"]
            if residual <= 0:
                active.discard(sym)
                continue
            coid = f"{cycle_key}:{sym}:{o.side}:{tag}"
            try:
                resp = broker.submit_order(sym, residual, o.side, order_type="market",
                                           client_order_id=coid)
            except AlpacaAPIError as exc:
                rejected += 1
                out[sym].update(status="rejected", reason=f"rejected: {exc}")
                pending.append({"symbol": sym, "side": o.side, "delta_usd": None,
                                "qty": residual, "reason": f"rejected: {exc}"})
                log.error("express sweep rejected; deferring", extra={"symbol": sym, "error": str(exc)})
                _emit_chase_event(db_engine, cycle_key=cycle_key, rnd=tag, symbol=sym, side=o.side,
                                  event="reject", tier="express", limit_price=None,
                                  qty=residual, filled_qty=out[sym]["filled"], target_qty=o.qty)
                if alert:
                    alert(f"order rejected {sym} {o.side} {residual}: {exc}")
                active.discard(sym)
                continue
            submitted_n += 1
            _upsert_order(db_engine, cycle_key, resp)
            _emit_chase_event(db_engine, cycle_key=cycle_key, rnd=tag, symbol=sym, side=o.side,
                              event="post", tier="express", limit_price=None, qty=residual,
                              filled_qty=out[sym]["filled"], target_qty=o.qty, order_id=resp["id"])
            booked.setdefault(resp["id"], 0)
            swept.append((resp["id"], sym))
        open_ids = {oid for oid, _ in swept}
        for _ in range(max(int(60.0 / tick_s), 1)):              # markets confirm in seconds
            if not open_ids:
                break
            sleep(tick_s)
            for oid, sym in swept:
                if oid not in open_ids:
                    continue
                try:
                    st = read(oid)
                except AlpacaAPIError:
                    continue
                if st is None:
                    continue
                _upsert_order(db_engine, cycle_key, st)
                _book(sym, st)
                if st.get("status") in _TERMINAL:
                    open_ids.discard(oid)
                    _settle_event(sym, st, tag)
                if out[sym]["filled"] >= by_sym[sym].qty:
                    active.discard(sym)
        for oid, sym in swept:
            if oid in open_ids and sym in active:                # unconfirmed → left working
                out[sym].update(status="queued", reason="express sweep left working")
                active.discard(sym)

    rnd = 0
    while active and rnd < max_rounds:
        if window_end is not None and now() >= window_end:
            break
        if express_finish_requested(db_engine):
            clear_express_finish(db_engine)
            log.warning("express-finish pressed: sweeping equity residuals at market",
                        extra={"cycle": cycle_key, "names": len(active)})
            if alert:
                alert(f"express-finish: sweeping {len(active)} residual equity name(s) at market")
            _express_sweep("xfin")
            break
        rnd += 1
        _round(f"r{rnd}")

    for sym in list(working):                                    # teardown: true residuals for the auction
        _pull(sym, f"r{max(rnd, 1)}")
    _auction_or_defer(active, by_sym, out, pending, quote=quote, ex=ex, broker=broker,
                      db_engine=db_engine, cycle_key=cycle_key, tier_of=tier_of,
                      patho_of=patho_of, arrival=arrival)
    submitted_n += sum(1 for d in out.values() if d["status"] == "auction")
    return _finalize(out, orders, submitted=submitted_n, rejected=rejected, pending=pending,
                     db_engine=db_engine, cycle_key=cycle_key)


def _auction_or_defer(active, by_sym, out, pending, *, quote, ex, broker, db_engine, cycle_key,
                      tier_of, patho_of, arrival=None) -> None:
    """Residual at the close: closing-auction LOC for liquid, non-pathological names; else cross-day.

    Every LOC route ALSO writes a ``pending_adjustments`` row: if the auction print lands outside
    the limit the order dies unfilled, and — with equities re-planned only at the *monthly*
    rebalance — nothing else would notice for weeks. The next-day top-up recomputes the delta from
    live positions, so a filled LOC self-resolves as "already at target" and costs nothing.
    """
    arrival = arrival or {}
    cap_frac = float(getattr(ex, "max_chase_bps", 150.0) or 150.0) / 1e4
    for sym in list(active):
        o = by_sym[sym]
        residual = o.qty - out[sym]["filled"]
        if residual <= 0:
            continue
        eligible = tier_of.get(sym) in ("deep", "moderate") and not patho_of.get(sym, False)
        if eligible:
            try:
                bid, ask, trade = quote(sym)
            except AlpacaAPIError:
                bid = ask = trade = None
            ref = arrival_reference(bid, ask, trade)
            if ref is not None:
                loc = marketable_price(ref, o.side, ex=ex)       # generous LOC limit at the cap
                anchor = arrival.get(sym)
                if anchor:                                       # chase cap holds at the auction too
                    ceil_px = round(anchor * ((1.0 + cap_frac) if o.side == "buy" else (1.0 - cap_frac)), 2)
                    loc = min(loc, ceil_px) if o.side == "buy" else max(loc, ceil_px)
                try:
                    resp = broker.submit_order(sym, residual, o.side, order_type="limit",
                                               limit_price=loc, client_order_id=f"{cycle_key}:{sym}:{o.side}:close",
                                               time_in_force="cls")
                    _upsert_order(db_engine, cycle_key, resp)
                    out[sym].update(status="auction", reason="limit-on-close (fills at 4pm auction)")
                    pending.append({"symbol": sym, "side": o.side, "delta_usd": None, "qty": residual,
                                    "reason": "routed to closing auction; next-day top-up verifies the fill"})
                    continue
                except AlpacaAPIError as exc:
                    log.warning("LOC submit failed; deferring", extra={"symbol": sym, "error": str(exc)})
        pending.append({"symbol": sym, "side": o.side, "delta_usd": None,
                        "qty": residual, "reason": "unfilled at close; deferred to cross-day"})


def _sp_tier(o: PlannedOrder) -> str:
    """Single-pass tactic label for the chase board: express market sweep vs one passive limit."""
    return "express" if o.order_type == "market" else "single"


def _single_pass(orders, *, broker, db_engine, cycle_key, pending, out,
                 poll_attempts, poll_interval_s, sleep, read, alert,
                 cancel_leftover: bool = True) -> ExecReport:
    already = _existing_client_ids(db_engine, cycle_key)
    live: list[tuple[str, PlannedOrder]] = []   # (order_id, planned)
    rejected = 0
    for o in orders:
        coid = f"{cycle_key}:{o.symbol}:{o.side}"
        if coid in already:
            log.info("order already submitted this cycle; skipping", extra={"coid": coid})
            out.pop(o.symbol, None)
            continue
        resp = None
        reject_exc = None
        for attempt in range(3):                # bounded retry for TRANSIENT failures (429/5xx/BP)
            try:
                resp = broker.submit_order(o.symbol, o.qty, o.side, order_type=o.order_type,
                                           limit_price=o.limit_price, client_order_id=coid)
                break
            except AlpacaAPIError as exc:
                if attempt < 2 and is_transient_error(exc):
                    log.warning("transient submit failure; retrying",
                                extra={"symbol": o.symbol, "attempt": attempt + 1, "error": str(exc)})
                    sleep(1.0 + attempt)
                    continue
                reject_exc = exc                # permanent rejection (or out of retries) — defer
                break
        if resp is None:
            rejected += 1
            pending.append({"symbol": o.symbol, "side": o.side,
                            "delta_usd": round(o.notional, 2), "qty": o.qty,
                            "reason": f"rejected: {reject_exc}"})
            out[o.symbol].update(status="rejected", reason=f"rejected: {reject_exc}")
            log.error("order rejected; deferring", extra={"symbol": o.symbol, "error": str(reject_exc)})
            _emit_chase_event(db_engine, cycle_key=cycle_key, rnd="r1", symbol=o.symbol,
                              side=o.side, event="reject", tier=_sp_tier(o),
                              limit_price=o.limit_price, qty=o.qty, filled_qty=0, target_qty=o.qty)
            if alert:
                alert(f"order rejected {o.symbol} {o.side} {o.qty}: {reject_exc}")
            continue
        _upsert_order(db_engine, cycle_key, resp)
        # Telemetry so single-pass runs (express market orders, no-quote fallback) show on the
        # chase board too — tier doubles as the board's tactic label.
        _emit_chase_event(db_engine, cycle_key=cycle_key, rnd="r1", symbol=o.symbol, side=o.side,
                          event="post", tier=_sp_tier(o), limit_price=o.limit_price,
                          qty=o.qty, filled_qty=0, target_qty=o.qty, order_id=resp["id"])
        live.append((resp["id"], o))

    open_ids = {oid for oid, _ in live}
    recorded_fill: set[str] = set()
    for _ in range(poll_attempts):
        if not open_ids:
            break
        sleep(poll_interval_s)
        for oid, _o in live:
            if oid not in open_ids:
                continue
            try:
                st = read(oid)
            except AlpacaAPIError as exc:   # transient read hiccup — keep the order open, re-poll next pass
                log.warning("poll read failed; will retry next pass",
                            extra={"id": oid, "error": str(exc)})
                continue
            if st is None:                  # feed hasn't seen it yet / fallback unresolved — retry next pass
                continue
            _upsert_order(db_engine, cycle_key, st)
            if st["status"] in _TERMINAL:
                open_ids.discard(oid)
                if (st.get("filled_qty") or 0) > 0 and oid not in recorded_fill:
                    _record_fill(db_engine, st); recorded_fill.add(oid)
                _emit_chase_event(db_engine, cycle_key=cycle_key, rnd="r1", symbol=_o.symbol,
                                  side=_o.side, event="settle", tier=_sp_tier(_o),
                                  limit_price=st.get("limit_price"),
                                  filled_qty=st.get("filled_qty"), target_qty=_o.qty,
                                  status=st.get("status"), order_id=oid)

    for oid, o in live:
        if oid not in open_ids:
            continue
        if cancel_leftover:
            try:
                broker.cancel_order(oid)
            except AlpacaAPIError as exc:
                log.warning("cancel failed at session end", extra={"id": oid, "error": str(exc)})
            # Settle-wait: a cancel is async — read only once the order is terminal, or the
            # residual rolled to pending can double-count a fill that was still in flight.
            st = _settle_after_cancel(read, oid, sleep)
        else:
            try:
                st = read(oid)
            except AlpacaAPIError as exc:   # can't read final state — leave it; reconcile corrects
                log.warning("session-end read failed; leaving to reconcile",
                            extra={"id": oid, "error": str(exc)})
                continue
        if st is None:
            continue
        _upsert_order(db_engine, cycle_key, st)
        filled_qty = int(st.get("filled_qty") or 0)
        if filled_qty > 0 and oid not in recorded_fill:
            _record_fill(db_engine, st); recorded_fill.add(oid)
        _emit_chase_event(db_engine, cycle_key=cycle_key, rnd="r1", symbol=o.symbol,
                          side=o.side, event="settle", tier=_sp_tier(o),
                          limit_price=st.get("limit_price"), filled_qty=filled_qty,
                          target_qty=o.qty, status=st.get("status"), order_id=oid)
        residual = o.qty - filled_qty
        if residual > 0:
            if cancel_leftover:
                pending.append({"symbol": o.symbol, "side": o.side, "delta_usd": None,
                                "qty": residual, "reason": "unfilled at session end"})
            else:               # express off-hours: the order STAYS working for the next open
                out[o.symbol].update(status="queued",
                                     reason="left working; fills at the next session")

    for oid, o in live:
        try:
            st = read(oid)
        except AlpacaAPIError as exc:   # tally only — a read hiccup must not crash a completed cycle
            log.warning("tally read failed; counting unknown", extra={"id": oid, "error": str(exc)})
            continue
        if st is None:
            continue
        out[o.symbol]["filled"] = int(st.get("filled_qty") or 0)
    return _finalize(out, orders, submitted=len(live), rejected=rejected, pending=pending,
                     db_engine=db_engine, cycle_key=cycle_key)


def _ext_session(dt_utc: datetime) -> bool:
    """Whether US extended-hours equity trading is plausibly live: a weekday, 4:00–9:30 or
    16:00–20:00 ET. Holidays aren't checked — an ext-hours limit on a holiday simply never
    fills and falls through to the queued-market leg, which is the correct outcome anyway."""
    from zoneinfo import ZoneInfo
    et = dt_utc.astimezone(ZoneInfo("America/New_York"))
    if et.weekday() >= 5:
        return False
    m = et.hour * 60 + et.minute
    return (4 * 60 <= m < 9 * 60 + 30) or (16 * 60 <= m < 20 * 60)


def submit_express(
    orders: Sequence[PlannedOrder],
    *,
    broker,
    db_engine,
    cycle_key: str,
    quote: Callable[[str], tuple] | None = None,
    clock=None,
    ex=None,
    pending: list[dict] | None = None,
    poll_attempts: int = 30,
    poll_interval_s: float = 2.0,
    sleep: Callable[[float], None] = time.sleep,
    order_state: Callable[[str], dict] | None = None,
    alert: Callable[[str], None] | None = None,
    now: Callable[[], datetime] | None = None,
) -> ExecReport:
    """Express execution: fills GUARANTEED, cost collared where the session allows.

    Session-aware (2026-07-06 execution review — raw market orders for everything meant paying
    any spread during RTH and, off-hours, queuing to the gap-prone opening print even when
    extended-hours liquidity was live):

    * **RTH** — market orders for tight names; a *pathological* spread posts a wide marketable
      limit first (``express_collar_bps`` past the reference), and any leftover sweeps to market
      after the poll window. The collar can only save money — the market sweep keeps the
      guarantee.
    * **Extended hours** (4:00–9:30 / 16:00–20:00 ET weekdays) — extended-hours limit orders at
      the collar, so express actually EXECUTES now instead of waiting for the open; the unfilled
      remainder is re-queued as a market order for the next session (never cancelled).
    * **Overnight / weekend / holiday** — market orders queued for the next open (the legacy
      behaviour, unchanged).

    ``clock`` is the market-clock reader (``is_open`` gates RTH); ``now`` is injectable for
    tests. Telemetry lands on the chase board under the ``express`` tactic label.
    """
    pending = list(pending or [])
    read = order_state or broker.get_order
    now_fn = now or (lambda: datetime.now(timezone.utc))
    out = {o.symbol: {"symbol": o.symbol, "side": o.side, "qty": int(o.qty),
                      "filled": 0, "status": "deferred", "reason": ""} for o in orders}
    collar = float(getattr(ex, "express_collar_bps", 200.0) or 0.0) / 1e4 if ex is not None else 0.02

    is_open = False
    if clock is not None:
        try:
            clk = clock() if callable(clock) else clock
            is_open = bool(clk.get("is_open"))
        except Exception as exc:  # noqa: BLE001 — no clock → treat as closed (queued markets)
            log.warning("express clock read failed; assuming closed", extra={"error": str(exc)})
    session = "rth" if is_open else ("ext" if _ext_session(now_fn()) else "closed")

    if quote is None or session == "closed":
        # No liquidity to collar against (or no quote surface): the legacy queued-market path.
        return _single_pass(orders, broker=broker, db_engine=db_engine, cycle_key=cycle_key,
                            pending=pending, out=out, poll_attempts=poll_attempts,
                            poll_interval_s=poll_interval_s, sleep=sleep, read=read,
                            alert=alert, cancel_leftover=False)

    def _phase1_terms(sym: str, side: str) -> tuple[str, Optional[float], bool]:
        """(order_type, limit_price, extended_hours) for the first pass."""
        try:
            bid, ask, trade = quote(sym)
        except AlpacaAPIError:
            bid = ask = trade = None
        ref = arrival_reference(bid, ask, trade)
        mult = (1.0 + collar) if side == "buy" else (1.0 - collar)
        if session == "rth":
            if ref is not None and ex is not None and is_pathological_spread(bid, ask, ex=ex):
                return "limit", round(float(ref) * mult, 2), False   # collar the phantom spread
            return "market", None, False
        if ref is None:
            return "market", None, False                             # unquotable → queue for open
        return "limit", round(float(ref) * mult, 2), True            # ext-hours: executable NOW

    def _submit(o: PlannedOrder, qty: int, otype: str, lp, coid: str, ext_flag: bool) -> dict:
        for attempt in range(3):
            try:
                if ext_flag:
                    try:
                        return broker.submit_order(o.symbol, qty, o.side, order_type=otype,
                                                   limit_price=lp, client_order_id=coid,
                                                   extended_hours=True)
                    except TypeError:            # a broker without the flag — still a valid limit
                        return broker.submit_order(o.symbol, qty, o.side, order_type=otype,
                                                   limit_price=lp, client_order_id=coid)
                return broker.submit_order(o.symbol, qty, o.side, order_type=otype,
                                           limit_price=lp, client_order_id=coid)
            except AlpacaAPIError as exc:
                if attempt < 2 and is_transient_error(exc):
                    log.warning("transient express submit failure; retrying",
                                extra={"symbol": o.symbol, "attempt": attempt + 1, "error": str(exc)})
                    sleep(1.0 + attempt)
                    continue
                raise
        raise AssertionError("unreachable")

    submitted_n = 0
    rejected = 0
    live: list[tuple[str, PlannedOrder, bool]] = []      # (order_id, planned, is_limit)

    def _reject(o: PlannedOrder, exc) -> None:
        nonlocal rejected
        rejected += 1
        pending.append({"symbol": o.symbol, "side": o.side, "delta_usd": round(o.notional, 2),
                        "qty": o.qty, "reason": f"rejected: {exc}"})
        out[o.symbol].update(status="rejected", reason=f"rejected: {exc}")
        log.error("express order rejected; deferring", extra={"symbol": o.symbol, "error": str(exc)})
        _emit_chase_event(db_engine, cycle_key=cycle_key, rnd="x1", symbol=o.symbol, side=o.side,
                          event="reject", tier="express", limit_price=None, qty=o.qty,
                          filled_qty=0, target_qty=o.qty)
        if alert:
            alert(f"order rejected {o.symbol} {o.side} {o.qty}: {exc}")

    for o in orders:
        otype, lp, ext_flag = _phase1_terms(o.symbol, o.side)
        try:
            resp = _submit(o, int(o.qty), otype, lp, f"{cycle_key}:{o.symbol}:{o.side}:x1", ext_flag)
        except AlpacaAPIError as exc:
            _reject(o, exc)
            continue
        submitted_n += 1
        _upsert_order(db_engine, cycle_key, resp)
        _emit_chase_event(db_engine, cycle_key=cycle_key, rnd="x1", symbol=o.symbol, side=o.side,
                          event="post", tier="express", limit_price=lp, qty=int(o.qty),
                          filled_qty=0, target_qty=o.qty, order_id=resp["id"])
        live.append((resp["id"], o, otype == "limit"))

    def _poll(entries: list[tuple[str, PlannedOrder]], tag: str) -> set[str]:
        """Poll to terminal; book fills + settle events. Returns the still-open order ids."""
        open_ids = {oid for oid, _ in entries}
        for _ in range(poll_attempts):
            if not open_ids:
                break
            sleep(poll_interval_s)
            for oid, o in entries:
                if oid not in open_ids:
                    continue
                try:
                    st = read(oid)
                except AlpacaAPIError:
                    continue
                if st is None:
                    continue
                _upsert_order(db_engine, cycle_key, st)
                if st["status"] in _TERMINAL:
                    open_ids.discard(oid)
                    fq = int(st.get("filled_qty") or 0)
                    if fq > 0:
                        _record_fill(db_engine, st)
                        out[o.symbol]["filled"] += fq
                    _emit_chase_event(db_engine, cycle_key=cycle_key, rnd=tag, symbol=o.symbol,
                                      side=o.side, event="settle", tier="express",
                                      limit_price=st.get("limit_price"),
                                      filled_qty=out[o.symbol]["filled"], target_qty=o.qty,
                                      status=st.get("status"), order_id=oid)
        return open_ids

    open1 = _poll([(oid, o) for oid, o, _lim in live], "x1")

    sweep: list[tuple[str, PlannedOrder]] = []
    for oid, o, is_limit in live:
        if oid not in open1:
            continue
        if not is_limit:
            # A market order still working (queued off-session / mid-print): leave it — that IS
            # the guarantee. Reported queued; reconcile books the eventual fill.
            out[o.symbol].update(status="queued", reason="market order left working")
            continue
        try:
            broker.cancel_order(oid)
        except AlpacaAPIError as exc:
            log.warning("express collar cancel failed", extra={"id": oid, "error": str(exc)})
        st = _settle_after_cancel(read, oid, sleep)
        residual = int(o.qty)
        if st is not None:
            _upsert_order(db_engine, cycle_key, st)
            fq = int(st.get("filled_qty") or 0)
            if fq > 0:
                _record_fill(db_engine, st)
                out[o.symbol]["filled"] += fq
            _emit_chase_event(db_engine, cycle_key=cycle_key, rnd="x1", symbol=o.symbol,
                              side=o.side, event="settle", tier="express",
                              limit_price=st.get("limit_price"),
                              filled_qty=out[o.symbol]["filled"], target_qty=o.qty,
                              status=st.get("status"), order_id=oid)
            residual = int(o.qty) - out[o.symbol]["filled"]
        if residual <= 0:
            continue
        try:
            resp = _submit(o, residual, "market", None, f"{cycle_key}:{o.symbol}:{o.side}:x2", False)
        except AlpacaAPIError as exc:
            _reject(o, exc)
            continue
        submitted_n += 1
        _upsert_order(db_engine, cycle_key, resp)
        _emit_chase_event(db_engine, cycle_key=cycle_key, rnd="x2", symbol=o.symbol, side=o.side,
                          event="post", tier="express", limit_price=None, qty=residual,
                          filled_qty=out[o.symbol]["filled"], target_qty=o.qty, order_id=resp["id"])
        if session == "rth":
            sweep.append((resp["id"], o))
        else:                                    # ext leftover → queued market for the next open
            out[o.symbol].update(status="queued",
                                 reason="ext-hours collar unfilled; market order queued for the open")

    if sweep:
        open2 = _poll(sweep, "x2")
        for oid, o in sweep:
            if oid in open2:                     # an RTH market that didn't confirm — leave working
                out[o.symbol].update(status="queued", reason="market order left working")

    return _finalize(out, orders, submitted=submitted_n, rejected=rejected, pending=pending,
                     db_engine=db_engine, cycle_key=cycle_key)



# --- DB helpers (lazy sqlalchemy import, like factors.write_factor_scores) ----------- #
def _existing_client_ids(db_engine, cycle_key: str) -> set[str]:
    """client_order_ids already recorded for this cycle (the idempotency set)."""
    from sqlalchemy import select
    from engine.db import orders as orders_t
    with db_engine.connect() as conn:
        rows = conn.execute(
            select(orders_t.c.client_order_id).where(orders_t.c.rebalance_cycle == cycle_key)
        ).all()
    return {r[0] for r in rows if r[0] is not None}


def _order_row(cycle_key: str, od: dict) -> dict:
    return {
        "id": od["id"],
        "client_order_id": od.get("client_order_id"),
        "rebalance_cycle": cycle_key,
        "symbol": od.get("symbol"),
        "side": od.get("side"),
        "qty": od.get("qty"),
        "order_type": od.get("order_type"),
        "status": od.get("status"),
        "limit_price": od.get("limit_price"),
        "filled_qty": od.get("filled_qty"),
        "filled_avg_price": od.get("filled_avg_price"),
        "submitted_at": _dt(od.get("submitted_at")),
        "filled_at": _dt(od.get("filled_at")),
    }


def _upsert_order(db_engine, cycle_key: str, od: dict) -> None:
    """Insert the order, or update it in place if its id is already recorded."""
    from sqlalchemy import insert, select, update
    from engine.db import orders as orders_t
    row = _order_row(cycle_key, od)
    with db_engine.begin() as conn:
        exists = conn.execute(select(orders_t.c.id).where(orders_t.c.id == row["id"])).first()
        if exists:
            conn.execute(update(orders_t).where(orders_t.c.id == row["id"])
                         .values(**{k: v for k, v in row.items() if k != "id"}))
        else:
            conn.execute(insert(orders_t).values(**row))


def _record_fill(db_engine, od: dict) -> None:
    from sqlalchemy import insert
    from engine.db import fills as fills_t
    with db_engine.begin() as conn:
        conn.execute(insert(fills_t).values(
            order_id=od["id"], symbol=od.get("symbol"),
            qty=od.get("filled_qty"), price=od.get("filled_avg_price"),
            filled_at=_dt(od.get("filled_at")),
        ))


def _emit_chase_event(db_engine, *, cycle_key: str, rnd: str, symbol: str, side: str, event: str,
                      tier=None, ctx: Optional[dict] = None, limit_price=None, qty=None,
                      filled_qty=None, target_qty=None, status=None, order_id=None) -> None:
    """Append one chase-telemetry row for the execution visualizer (Phase 2).

    Best-effort and **failure-isolated**: any error (missing table, DB hiccup) is logged and
    swallowed so telemetry can never disrupt order placement or fills. ``ctx`` carries the
    round's bid/ask/mid from :func:`_price`. Uses a real UTC timestamp (not the injected ``now``)
    so it never perturbs the ladder's clock.
    """
    if db_engine is None:
        return
    try:
        from sqlalchemy import insert
        from engine.db import order_events as oe_t
        c = ctx or {}
        _int = lambda v: int(v) if v is not None else None       # noqa: E731 — tiny local coercion
        with db_engine.begin() as conn:
            conn.execute(insert(oe_t).values(
                ts=datetime.now(timezone.utc), cycle_key=cycle_key, round=rnd, symbol=symbol,
                side=side, event=event, tier=tier, bid=c.get("bid"), ask=c.get("ask"),
                mid=c.get("mid"), limit_price=limit_price, qty=_int(qty),
                filled_qty=_int(filled_qty), target_qty=_int(target_qty), status=status,
                order_id=order_id))
    except Exception as exc:  # noqa: BLE001 — telemetry must never break execution
        log.warning("chase-event emit failed (telemetry only)",
                    extra={"symbol": symbol, "event": event, "error": str(exc)})


def _write_pending(db_engine, pending: list[dict]) -> None:
    from sqlalchemy import insert
    from engine.db import pending_adjustments as pa_t
    rows = [{"symbol": p.get("symbol"), "side": p.get("side"),
             "delta_usd": p.get("delta_usd"), "qty": p.get("qty"),
             "reason": p.get("reason")} for p in pending]
    with db_engine.begin() as conn:
        conn.execute(insert(pa_t), rows)


def _dt(value) -> Optional[datetime]:
    """Parse an ISO timestamp string (or pass a datetime) for a DateTime column."""
    if value is None or isinstance(value, datetime):
        return value
    try:
        return datetime.fromisoformat(str(value))
    except ValueError:
        return None
