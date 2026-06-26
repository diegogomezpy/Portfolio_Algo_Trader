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
from dataclasses import dataclass
from datetime import datetime
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
    alert: Callable[[str], None] | None = None,
) -> ExecReport:
    """Submit ``orders`` via ``broker``, poll fills, cancel leftovers, persist to DB.

    **Idempotent** — each order carries ``client_order_id = "{cycle}:{symbol}:{side}"``,
    and any client_order_id already recorded in the ``orders`` table for this
    ``cycle_key`` is skipped, so a re-run after a crash resumes without double-submitting.

    Flow: submit each new order (a 4xx rejection is permanent — skip, defer the delta to
    ``pending_adjustments``, alert, no retry); poll open orders up to ``poll_attempts``
    times; at session end cancel anything still working and roll the unfilled residual to
    ``pending_adjustments``. ``pending`` carries the planner's sub-min-trade deltas, which
    are persisted alongside. ``sleep`` is injectable so tests run instantly.
    """
    pending = list(pending or [])
    already = _existing_client_ids(db_engine, cycle_key)

    live: list[tuple[str, PlannedOrder]] = []   # (order_id, planned)
    rejected = 0
    for o in orders:
        coid = f"{cycle_key}:{o.symbol}:{o.side}"
        if coid in already:
            log.info("order already submitted this cycle; skipping", extra={"coid": coid})
            continue
        try:
            resp = broker.submit_order(o.symbol, o.qty, o.side, order_type=o.order_type,
                                       limit_price=o.limit_price, client_order_id=coid)
        except AlpacaAPIError as exc:           # permanent rejection — defer, no retry
            rejected += 1
            pending.append({"symbol": o.symbol, "side": o.side,
                            "delta_usd": round(o.notional, 2), "qty": o.qty,
                            "reason": f"rejected: {exc}"})
            log.error("order rejected; deferring", extra={"symbol": o.symbol, "error": str(exc)})
            if alert:
                alert(f"order rejected {o.symbol} {o.side} {o.qty}: {exc}")
            continue
        _upsert_order(db_engine, cycle_key, resp)
        live.append((resp["id"], o))

    # Poll open orders for fills.
    open_ids = {oid for oid, _ in live}
    recorded_fill: set[str] = set()
    for _ in range(poll_attempts):
        if not open_ids:
            break
        sleep(poll_interval_s)
        for oid, _o in live:
            if oid not in open_ids:
                continue
            st = broker.get_order(oid)
            _upsert_order(db_engine, cycle_key, st)
            if st["status"] in _TERMINAL:
                open_ids.discard(oid)
                if (st.get("filled_qty") or 0) > 0 and oid not in recorded_fill:
                    _record_fill(db_engine, st)
                    recorded_fill.add(oid)

    # Session end: cancel anything still working, record any partial fill, roll residual.
    for oid, o in live:
        if oid not in open_ids:
            continue
        try:
            broker.cancel_order(oid)
        except AlpacaAPIError as exc:
            log.warning("cancel failed at session end", extra={"id": oid, "error": str(exc)})
        st = broker.get_order(oid)
        _upsert_order(db_engine, cycle_key, st)
        filled_qty = int(st.get("filled_qty") or 0)
        if filled_qty > 0 and oid not in recorded_fill:
            _record_fill(db_engine, st)
            recorded_fill.add(oid)
        residual = o.qty - filled_qty
        if residual > 0:
            pending.append({"symbol": o.symbol, "side": o.side, "delta_usd": None,
                            "qty": residual, "reason": "unfilled at session end"})

    if pending:
        _write_pending(db_engine, pending)

    planned_qty = {oid: o.qty for oid, o in live}
    filled = partial = 0
    for oid in planned_qty:
        st = broker.get_order(oid)
        fq = int(st.get("filled_qty") or 0)
        if fq >= planned_qty[oid] and fq > 0:
            filled += 1
        elif fq > 0:
            partial += 1
    report = ExecReport(submitted=len(live), filled=filled, partial=partial,
                        rejected=rejected, deferred=len(pending))
    log.info("execution complete", extra=vars(report) | {"cycle": cycle_key})
    return report


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
