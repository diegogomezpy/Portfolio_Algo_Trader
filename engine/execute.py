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
from dataclasses import dataclass
from typing import Mapping, Optional

import pandas as pd

from engine.logger import get_logger

log = get_logger(__name__)


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
    symbol: str, price: float, *, adv: Mapping[str, float], spread: Mapping[str, float], ex
) -> tuple[str, Optional[float]]:
    """Market for deep+tight names, else a limit at the mid (ARCHITECTURE).

    ``spread`` is the fractional bid/ask (or high-low proxy) per symbol; a name is sent
    market only when it is both deep (ADV ≥ large-cap threshold) and tight (spread <
    ``spread_threshold``). Otherwise a limit is placed at the supplied mid price.
    """
    a = float(adv.get(symbol, 0.0) or 0.0)
    s = spread.get(symbol)
    deep_and_tight = a >= ex.large_cap_adv_threshold and (s is not None and float(s) < ex.spread_threshold)
    if deep_and_tight:
        return "market", None
    return "limit", round(float(price), 2)


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

        otype, lp = _order_type_for(sym, mids.get(sym, px), adv=adv, spread=spread, ex=ex)
        orders.append(PlannedOrder(sym, side, qty, otype, lp, round(notional, 2)))

    # Sells (desc by notional) before buys (desc), so proceeds fund purchases.
    sells = sorted((o for o in orders if o.side == "sell"), key=lambda o: -o.notional)
    buys = sorted((o for o in orders if o.side == "buy"), key=lambda o: -o.notional)
    ordered = sells + buys
    log.info("order plan built",
             extra={"orders": len(ordered), "sells": len(sells), "buys": len(buys),
                    "deferred": len(pending), "nav": float(nav)})
    return ordered, pending
