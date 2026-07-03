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
    """
    pending = list(pending or [])
    read = order_state or broker.get_order
    out = {o.symbol: {"symbol": o.symbol, "side": o.side, "qty": int(o.qty),
                      "filled": 0, "status": "deferred", "reason": ""} for o in orders}
    if quote is not None:                              # tiered redesign (docs/EXECUTION.md §5–§8)
        return _tiered(orders, broker=broker, db_engine=db_engine, cycle_key=cycle_key,
                       pending=pending, out=out, quote=quote, adv=adv or {}, ex=ex, read=read,
                       now=now or (lambda: datetime.now(timezone.utc)), market_close=market_close,
                       sleep=sleep, alert=alert)
    return _single_pass(orders, broker=broker, db_engine=db_engine, cycle_key=cycle_key,
                        pending=pending, out=out, poll_attempts=poll_attempts,
                        poll_interval_s=poll_interval_s, sleep=sleep, read=read, alert=alert)


def _finalize(out, orders, *, submitted, rejected, pending, db_engine, cycle_key) -> ExecReport:
    """Derive per-symbol statuses from accumulated fills, write pending, build the ExecReport."""
    if pending:
        _write_pending(db_engine, pending)
    by_qty = {o.symbol: int(o.qty) for o in orders}
    filled = partial = 0
    for sym, d in out.items():
        if d["status"] in ("rejected", "auction"):    # terminal states set by the caller; keep them
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
            now, market_close, sleep, alert, max_rounds: int = 500) -> ExecReport:
    """Liquidity-tiered, patient-then-guaranteed equity execution (docs/EXECUTION.md §5–§8).

    Per round, each still-unfilled name is priced by its **tier**: *deep+tight* → a marketable
    limit (cross cap) that fills at once; *moderate/thin* → a **ladder** whose limit walks from the
    mid to the cap as the session elapses (re-anchored each round, cadence ``equity_repeg_s``);
    a **pathological** spread → a passive limit at the guarded reference that never crosses. Thin
    names post one child at a time (``child_adv_pct`` of ADV). Fills are read from ``read`` (the
    live feed). At ``close_buffer_s`` before ``market_close`` the residual is routed to the
    **closing auction** (limit-on-close) for liquid, non-pathological names, else rolled to the
    cross-day queue — there is no naked market order.
    """
    adv = adv or {}

    def _num(name, default):                               # keep a legit 0 (don't let `or` eat it)
        v = getattr(ex, name, None)
        return default if v is None else v
    repeg_s = float(_num("equity_repeg_s", 30.0))
    close_buffer_s = float(_num("close_buffer_s", 300.0))
    tick_s = float(_num("poll_interval_s", 2.0)) or 2.0    # divisor below → must be > 0
    ladder_steps = max(int(_num("ladder_steps", 3)), 1)
    by_sym = {o.symbol: o for o in orders}
    active = {o.symbol for o in orders}
    submitted_n = 0
    rejected = 0
    start = now()
    window_end = (market_close - timedelta(seconds=close_buffer_s)) if market_close else None
    window_s = (window_end - start).total_seconds() if window_end else None
    tier_of: dict[str, str] = {}
    patho_of: dict[str, bool] = {}

    def _price(sym: str) -> tuple[Optional[float], Optional[str], Optional[dict]]:
        """(limit_price, tier, ctx) for this round, or (None, tier, None) when it can't be priced
        now. ``ctx`` carries the round's ``bid``/``ask``/``mid`` for the chase visualizer."""
        o = by_sym[sym]
        try:
            bid, ask, trade = quote(sym)
        except AlpacaAPIError as exc:
            log.warning("quote failed; skipping this round", extra={"symbol": sym, "error": str(exc)})
            return None, tier_of.get(sym), None
        ref = arrival_reference(bid, ask, trade)
        if ref is None:
            return None, tier_of.get(sym), None
        tier = liquidity_tier(adv.get(sym), bid, ask, ex=ex)
        tier_of[sym] = tier
        patho_of[sym] = is_pathological_spread(bid, ask, ex=ex)
        mid = (bid + ask) / 2.0 if (bid and ask and ask >= bid) else float(ref)
        ctx = {"bid": bid, "ask": ask, "mid": mid}
        if patho_of[sym]:
            return round(float(ref), 2), tier, ctx               # passive at the guarded ref; never cross
        if tier == "deep":
            return marketable_price(ref, o.side, ex=ex), tier, ctx
        f = min(max((now() - start).total_seconds() / window_s, 0.0), 1.0) if window_s and window_s > 0 else 1.0
        level = round(f * ladder_steps) / ladder_steps           # discretize mid→cap into ladder_steps
        return ladder_price(ref, mid, o.side, level, ex=ex), tier, ctx

    def _round(tag: str) -> None:
        nonlocal submitted_n, rejected
        live: list[tuple[str, str]] = []
        for sym in list(active):
            o = by_sym[sym]
            residual = o.qty - out[sym]["filled"]
            if residual <= 0:
                active.discard(sym); continue
            price, tier, ctx = _price(sym)
            if price is None:
                continue                                          # can't price now — retry next round
            qty_to_post = child_qty(residual, adv.get(sym), price, ex=ex) if tier == "thin" else residual
            coid = f"{cycle_key}:{sym}:{o.side}:{tag}"
            try:
                resp = broker.submit_order(sym, qty_to_post, o.side, order_type="limit",
                                           limit_price=price, client_order_id=coid)
            except AlpacaAPIError as exc:
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
                active.discard(sym); continue
            submitted_n += 1
            _upsert_order(db_engine, cycle_key, resp)
            _emit_chase_event(db_engine, cycle_key=cycle_key, rnd=tag, symbol=sym, side=o.side,
                              event="post", tier=tier, ctx=ctx, limit_price=price, qty=qty_to_post,
                              filled_qty=out[sym]["filled"], target_qty=o.qty, order_id=resp["id"])
            live.append((resp["id"], sym))

        open_ids = {oid for oid, _ in live}
        for _ in range(max(int(repeg_s / tick_s), 1)):
            if not open_ids:
                break
            sleep(tick_s)
            for oid, sym in live:
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

        for oid, sym in live:                                    # round end: cancel leftover, book fills
            if oid in open_ids:
                try:
                    broker.cancel_order(oid)
                except AlpacaAPIError as exc:
                    log.warning("cancel failed", extra={"id": oid, "error": str(exc)})
            try:
                st = read(oid)
            except AlpacaAPIError:
                continue
            if st is None:
                continue
            _upsert_order(db_engine, cycle_key, st)
            fq = int(st.get("filled_qty") or 0)
            if fq > 0:
                _record_fill(db_engine, st)
                out[sym]["filled"] += fq
            _emit_chase_event(db_engine, cycle_key=cycle_key, rnd=tag, symbol=sym,
                              side=by_sym[sym].side, event="settle", tier=tier_of.get(sym),
                              limit_price=st.get("limit_price"), filled_qty=out[sym]["filled"],
                              target_qty=by_sym[sym].qty, status=st.get("status"), order_id=oid)
            if out[sym]["filled"] >= by_sym[sym].qty:
                active.discard(sym)

    rnd = 0
    while active and rnd < max_rounds:
        if window_end is not None and now() >= window_end:
            break
        rnd += 1
        _round(f"r{rnd}")

    _auction_or_defer(active, by_sym, out, pending, quote=quote, ex=ex, broker=broker,
                      db_engine=db_engine, cycle_key=cycle_key, tier_of=tier_of, patho_of=patho_of)
    submitted_n += sum(1 for d in out.values() if d["status"] == "auction")
    return _finalize(out, orders, submitted=submitted_n, rejected=rejected, pending=pending,
                     db_engine=db_engine, cycle_key=cycle_key)


def _auction_or_defer(active, by_sym, out, pending, *, quote, ex, broker, db_engine, cycle_key,
                      tier_of, patho_of) -> None:
    """Residual at the close: closing-auction LOC for liquid, non-pathological names; else cross-day."""
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
                try:
                    resp = broker.submit_order(sym, residual, o.side, order_type="limit",
                                               limit_price=loc, client_order_id=f"{cycle_key}:{sym}:{o.side}:close",
                                               time_in_force="cls")
                    _upsert_order(db_engine, cycle_key, resp)
                    out[sym].update(status="auction", reason="limit-on-close (fills at 4pm auction)")
                    continue
                except AlpacaAPIError as exc:
                    log.warning("LOC submit failed; deferring", extra={"symbol": sym, "error": str(exc)})
        pending.append({"symbol": sym, "side": o.side, "delta_usd": None,
                        "qty": residual, "reason": "unfilled at close; deferred to cross-day"})


def _single_pass(orders, *, broker, db_engine, cycle_key, pending, out,
                 poll_attempts, poll_interval_s, sleep, read, alert) -> ExecReport:
    already = _existing_client_ids(db_engine, cycle_key)
    live: list[tuple[str, PlannedOrder]] = []   # (order_id, planned)
    rejected = 0
    for o in orders:
        coid = f"{cycle_key}:{o.symbol}:{o.side}"
        if coid in already:
            log.info("order already submitted this cycle; skipping", extra={"coid": coid})
            out.pop(o.symbol, None)
            continue
        try:
            resp = broker.submit_order(o.symbol, o.qty, o.side, order_type=o.order_type,
                                       limit_price=o.limit_price, client_order_id=coid)
        except AlpacaAPIError as exc:           # permanent rejection — defer, no retry
            rejected += 1
            pending.append({"symbol": o.symbol, "side": o.side,
                            "delta_usd": round(o.notional, 2), "qty": o.qty,
                            "reason": f"rejected: {exc}"})
            out[o.symbol].update(status="rejected", reason=f"rejected: {exc}")
            log.error("order rejected; deferring", extra={"symbol": o.symbol, "error": str(exc)})
            if alert:
                alert(f"order rejected {o.symbol} {o.side} {o.qty}: {exc}")
            continue
        _upsert_order(db_engine, cycle_key, resp)
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

    for oid, o in live:
        if oid not in open_ids:
            continue
        try:
            broker.cancel_order(oid)
        except AlpacaAPIError as exc:
            log.warning("cancel failed at session end", extra={"id": oid, "error": str(exc)})
        try:
            st = read(oid)
        except AlpacaAPIError as exc:   # can't read final state — leave it; reconcile corrects from Alpaca
            log.warning("session-end read failed; leaving to reconcile",
                        extra={"id": oid, "error": str(exc)})
            continue
        if st is None:
            continue
        _upsert_order(db_engine, cycle_key, st)
        filled_qty = int(st.get("filled_qty") or 0)
        if filled_qty > 0 and oid not in recorded_fill:
            _record_fill(db_engine, st); recorded_fill.add(oid)
        residual = o.qty - filled_qty
        if residual > 0:
            pending.append({"symbol": o.symbol, "side": o.side, "delta_usd": None,
                            "qty": residual, "reason": "unfilled at session end"})

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
