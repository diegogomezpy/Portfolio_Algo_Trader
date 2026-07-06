"""Manual execution actions — the dashboard's execution console.

Three actions, each planned as a concrete order list *before* anything is sent (the
dashboard shows the plan in its confirm modal, then executes the same plan):

* ``liquidate`` — sell ``pct``% of every equity position, pro-rata, and trim the SPY
  call-spread overlay by the same fraction (β coverage stays honest; 100% closes it).
* ``trade`` — one name: buy ``$usd`` worth, or sell ``pct``% of the position.
* ``leverage`` — scale every position so gross exposure hits ``target ×`` equity, and
  **persist the target as a sticky override** (:mod:`engine.overrides`) so subsequent
  rebalances size to it until cleared. Lever-down trims the overlay proportionally;
  lever-up leaves it for the next overlay pass to resize (writing more spread needs the
  full chain machinery — deliberately not a console action).

Two modes:

* ``normal`` — the real tiered executor (:func:`engine.execute.submit_and_track` with a
  live NBBO quote fn): patient mid→touch ladders, chase telemetry, the works.
* ``express`` — straight market orders through the same single-pass path (still
  idempotent, still journaled, still on the chase board via its telemetry).

Every run gets its own ``cycle_key`` (``manual-<action>-<UTC timestamp>``), so orders,
fills, and ``order_events`` journal exactly like a rebalance's — and the execution
visualizer, which follows the latest cycle, picks the run up automatically. Each run is
also recorded in ``manual_actions`` (started → done/failed) as the audit trail.

Planners raise ``ValueError`` with operator-readable messages; the dashboard surfaces
them verbatim.
"""

from __future__ import annotations

import math
from datetime import date, datetime, timezone

from engine import covered_calls, execute, overrides, symbols
from engine.alpaca_client import AlpacaAPIError
from engine.execute import PlannedOrder, submit_and_track
from engine.logger import get_logger

log = get_logger(__name__)

ACTIONS = ("liquidate", "trade", "leverage")
MODES = ("normal", "express")


# ====================================================================== #
# Planning (pure reads — safe for the dashboard's in-process preview)
# ====================================================================== #
def _equity_positions(client) -> dict[str, float]:
    """Current equity positions ``{symbol: qty}`` (options excluded)."""
    out: dict[str, float] = {}
    for p in client.all_positions():
        sym = str(p.get("symbol", ""))
        if symbols.is_equity(sym) and float(p.get("qty", 0)):
            out[sym] = float(p["qty"])
    return out


def _last_price(client, sym: str) -> float | None:
    try:
        px = float(client.latest_trade(sym))
        return px if px > 0 else None
    except (AlpacaAPIError, TypeError, ValueError):
        return None


def _order(sym: str, side: str, qty: int, px: float | None) -> dict:
    return {"symbol": sym, "side": side, "qty": int(qty), "est_price": px,
            "est_notional": round(qty * px, 2) if px else None}


def _overlay_state(client, market: str) -> tuple[int, list]:
    """(total short contracts, short legs) of the SPY spread — what a trim would scale."""
    shorts, _longs = covered_calls.index_overwrite_legs(client, market)
    return sum(int(abs(float(p.get("qty", 0)))) for p in shorts), shorts


def plan_liquidate(client, pct: float, *, market: str = "SPY") -> dict:
    """Sell ``pct``% of every equity position; trim the overlay spread by the same fraction."""
    pct = float(pct)
    if not 0 < pct <= 100:
        raise ValueError(f"liquidation pct must be in (0, 100], got {pct}")
    frac = pct / 100.0
    positions = _equity_positions(client)
    if not positions:
        raise ValueError("no equity positions to liquidate")

    orders, skipped = [], []
    for sym, qty in sorted(positions.items()):
        if qty <= 0:
            continue                                   # shorts are the engine's to flatten, not ours
        sell = int(qty) if pct >= 100 else int(math.floor(qty * frac))
        if sell < 1:
            skipped.append({"symbol": sym, "reason": f"{pct}% of {int(qty)} sh rounds to 0"})
            continue
        orders.append(_order(sym, "sell", sell, _last_price(client, sym)))
    orders.sort(key=lambda o: -(o["est_notional"] or 0))

    n_short, _ = _overlay_state(client, market)
    trim = n_short if pct >= 100 else int(round(n_short * frac))
    warnings = [w for w in (
        "closes the ENTIRE book and SPY spread" if pct >= 100 else None,
        f"spread too small to trim at {pct}% ({n_short} contract(s) — untouched)"
        if n_short and not trim and pct < 100 else None,
    ) if w]
    raise_usd = round(sum(o["est_notional"] or 0 for o in orders), 2)
    return {"action": "liquidate", "params": {"pct": pct}, "orders": orders, "skipped": skipped,
            "overlay": {"market": market, "contracts": n_short, "close_contracts": trim},
            "totals": {"sell_notional": raise_usd, "buy_notional": 0.0, "orders": len(orders)},
            "warnings": warnings}


def plan_trade(client, symbol: str, side: str, *, usd: float | None = None,
               pct: float | None = None) -> dict:
    """One name: buy ``$usd`` at the last price, or sell ``pct``% of the position."""
    sym = str(symbol).upper().strip()
    if side not in ("buy", "sell"):
        raise ValueError(f"side must be buy or sell, got {side!r}")
    if not symbols.is_equity(sym):
        raise ValueError(f"{sym} is not an equity symbol — the console trades stocks only")
    px = _last_price(client, sym)
    if px is None:
        raise ValueError(f"no live price for {sym} — can't size the order")
    positions = _equity_positions(client)
    warnings = []

    if side == "buy":
        if not usd or float(usd) <= 0:
            raise ValueError("buys are sized in dollars — pass usd > 0")
        qty = int(math.floor(float(usd) / px))
        if qty < 1:
            raise ValueError(f"${float(usd):,.0f} buys less than 1 share of {sym} (${px:,.2f})")
        if sym not in positions:
            warnings.append(f"{sym} is not in the current book — this adds an off-model "
                            f"position the next rebalance will likely unwind")
    else:
        held = positions.get(sym, 0.0)
        if held <= 0:
            raise ValueError(f"no {sym} position to sell")
        p = 100.0 if pct is None else float(pct)
        if not 0 < p <= 100:
            raise ValueError(f"sell pct must be in (0, 100], got {p}")
        qty = int(held) if p >= 100 else int(math.floor(held * p / 100.0))
        if qty < 1:
            raise ValueError(f"{p}% of {int(held)} {sym} shares rounds to 0")

    o = _order(sym, side, qty, px)
    return {"action": "trade", "params": {"symbol": sym, "side": side, "usd": usd, "pct": pct},
            "orders": [o], "skipped": [],
            "overlay": {"market": None, "contracts": 0, "close_contracts": 0},
            "totals": {("buy_notional" if side == "buy" else "sell_notional"): o["est_notional"] or 0.0,
                       ("sell_notional" if side == "buy" else "buy_notional"): 0.0,
                       "orders": 1},
            "warnings": warnings}


def plan_leverage(client, db_engine, settings, target: float, *, market: str = "SPY") -> dict:
    """Scale every equity position so gross exposure = ``target`` × account equity."""
    target = float(target)
    cap = float(getattr(settings.portfolio, "max_leverage", 2.0))
    if target <= 0:
        raise ValueError(f"target leverage must be positive, got {target}")
    if target > cap:
        raise ValueError(f"target {target:.2f}× exceeds max_leverage {cap:.2f}× — "
                         f"raise the cap in settings.yaml first if you mean it")
    eq = client.account().get("equity")
    if eq is None or float(eq) <= 0:
        raise ValueError(f"account equity unavailable ({eq!r}) — refusing to size")
    equity = float(eq)

    positions = _equity_positions(client)
    priced = {s: (_last_price(client, s), q) for s, q in positions.items()}
    gross = sum(px * q for px, q in priced.values() if px and q > 0)
    if gross <= 0:
        raise ValueError("no long equity exposure to scale — run a rebalance first")
    current = gross / equity
    factor = (target * equity) / gross

    min_trade = float(getattr(settings.execution, "min_trade_usd", 0.0))
    orders, skipped = [], []
    for sym, (px, qty) in sorted(priced.items()):
        if qty <= 0:
            continue
        if px is None:
            skipped.append({"symbol": sym, "reason": "no live price"})
            continue
        delta = int(math.floor(qty * factor)) - int(qty)
        if delta == 0:
            continue
        if abs(delta) * px < min_trade:
            skipped.append({"symbol": sym, "reason": f"below min_trade_usd ({min_trade:,.0f})"})
            continue
        orders.append(_order(sym, "buy" if delta > 0 else "sell", abs(delta), px))
    sells = sorted((o for o in orders if o["side"] == "sell"), key=lambda o: -(o["est_notional"] or 0))
    buys = sorted((o for o in orders if o["side"] == "buy"), key=lambda o: -(o["est_notional"] or 0))
    orders = sells + buys

    n_short, _ = _overlay_state(client, market)
    trim = int(round(n_short * (1.0 - factor))) if factor < 1.0 else 0
    warnings = [w for w in (
        f"sticky: future rebalances will size to {target:.2f}× until the override is cleared",
        "lever-up leaves the SPY spread as-is; the next overlay pass resizes it"
        if factor > 1.0 and n_short else None,
    ) if w]
    return {"action": "leverage", "params": {"target": target},
            "orders": orders, "skipped": skipped,
            "overlay": {"market": market, "contracts": n_short, "close_contracts": trim},
            "totals": {"sell_notional": round(sum(o["est_notional"] or 0 for o in sells), 2),
                       "buy_notional": round(sum(o["est_notional"] or 0 for o in buys), 2),
                       "orders": len(orders), "current_leverage": round(current, 3),
                       "target_leverage": target},
            "warnings": warnings}


def build_plan(action: str, client, db_engine, settings, **params) -> dict:
    """Dispatch to the right planner — the single entrypoint preview and execute share."""
    if action == "liquidate":
        return plan_liquidate(client, params["pct"])
    if action == "trade":
        return plan_trade(client, params["symbol"], params["side"],
                          usd=params.get("usd"), pct=params.get("pct"))
    if action == "leverage":
        return plan_leverage(client, db_engine, settings, params["target"])
    raise ValueError(f"unknown action {action!r} (want one of {ACTIONS})")


# ====================================================================== #
# Execution
# ====================================================================== #
def _quote_fns(client, settings):
    """``(quote_fn, quote_batch)`` for the tiered executor — the shared builders in
    :mod:`engine.execute` (raw NBBO, stale-print guard, one batched sweep per round)."""
    stale = float(getattr(settings.execution, "stale_trade_max_s", 900) or 0)
    return (execute.quote_fn_for(client, stale_trade_max_s=stale),
            execute.batch_quote_fn(client, stale_trade_max_s=stale))


def _market_close(client):
    try:
        nc = client.market_clock().get("next_close")
        return datetime.fromisoformat(nc) if nc else None
    except Exception:  # noqa: BLE001 — run without a close guarantee rather than block
        return None


def _option_chase(client, mkt_close, settings) -> covered_calls.OptionChase | None:
    if not hasattr(client, "latest_option_quote"):
        return None

    def _touch(option_symbol, side):
        bid, ask = client.latest_option_quote(option_symbol)
        return ask if side == "buy" else bid

    mbf = getattr(settings.covered_calls, "min_bid_frac", None)
    kw = {"min_bid_frac": float(mbf)} if mbf is not None else {}
    kw["ladder_rounds"] = int(getattr(settings.covered_calls, "chase_ladder_rounds", 0) or 0)
    return covered_calls.OptionChase(touch=_touch, market_close=mkt_close, **kw)


def execute_plan(plan: dict, mode: str, *, client, broker, db_engine, settings,
                 cycle_key: str, alert=None) -> dict:
    """Run a planned action: overlay trim first (risk-off), then the equity legs.

    Returns a JSON-able result summary (fills, notional, overlay contracts closed).
    """
    if mode not in MODES:
        raise ValueError(f"mode must be one of {MODES}, got {mode!r}")
    execute.clear_express_finish(db_engine)   # a stale press must never express a FRESH action

    ov = plan.get("overlay") or {}
    overlay_closed = 0
    if ov.get("close_contracts"):
        frac = min(ov["close_contracts"] / ov["contracts"], 1.0) if ov.get("contracts") else 1.0
        chase = None if mode == "express" else _option_chase(client, _market_close(client), settings)
        closed = covered_calls.close_index_overwrite(
            client, broker, db_engine, as_of=date.today(), market=ov.get("market") or "SPY",
            chase=chase, alert=alert, fraction=frac)
        overlay_closed = len(closed or [])

    planned = [PlannedOrder(o["symbol"], o["side"], int(o["qty"]),
                            "market" if mode == "express" else "limit", None,
                            float(o["est_notional"] or 0.0))
               for o in plan.get("orders") or []]
    report = None
    if planned:
        if mode == "express":
            # Session-aware express (engine.execute.submit_express): RTH market orders with a
            # collar on pathological spreads, ext-hours limits that EXECUTE now off-hours, and
            # queued markets otherwise. Leftovers are never cancelled — express always executes.
            quote_fn, _batch = _quote_fns(client, settings)
            report = execute.submit_express(
                planned, broker=broker, db_engine=db_engine, cycle_key=cycle_key,
                quote=quote_fn,
                clock=(client.market_clock if hasattr(client, "market_clock") else None),
                ex=settings.execution, alert=alert)
        else:
            quote_fn, quote_batch = _quote_fns(client, settings)
            report = submit_and_track(planned, broker=broker, db_engine=db_engine,
                                      cycle_key=cycle_key, quote=quote_fn, adv={},
                                      ex=settings.execution, market_close=_market_close(client),
                                      alert=alert, quote_batch=quote_batch)
    return {
        "cycle_key": cycle_key, "mode": mode, "overlay_closed": overlay_closed,
        "submitted": report.submitted if report else 0,
        "filled": report.filled if report else 0,
        "partial": report.partial if report else 0,
        "rejected": report.rejected if report else 0,
        "deferred": report.deferred if report else 0,
        "lines": report.lines if report else [],
    }


def _exec_conflict(db_engine, *, window_s: float = 180.0) -> str | None:
    """A description of another execution that looks in-flight, or ``None``.

    Best-effort concurrency guard (2026-07-06 execution review): nothing stopped a console
    action from trading the same names as the 13:00 rebalance mid-chase, each blind to the
    other's fills. Two signals, no schema change:

    * a ``manual_actions`` row still ``started`` (younger than 2 h, so a crashed run can't
      block forever) — another console/CLI action is running;
    * ``order_events`` telemetry from a NON-manual cycle in the last ``window_s`` — the
      scheduled rebalance emits at least one event per repeg round (≤30 s apart) while it
      works, so a 3-minute quiet period means it is done.

    Failure-isolated: an unreadable DB returns ``None`` (the guard must never be the thing
    that blocks trading).
    """
    try:
        from sqlalchemy import select
        from engine.db import manual_actions as ma_t, order_events as oe_t
        now = datetime.now(timezone.utc)

        def _age(ts) -> float:
            ts = ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)
            return (now - ts).total_seconds()

        with db_engine.connect() as conn:
            row = conn.execute(select(ma_t.c.cycle_key, ma_t.c.ts)
                               .where(ma_t.c.status == "started")
                               .order_by(ma_t.c.ts.desc()).limit(1)).first()
            if row and row[1] is not None and _age(row[1]) < 7200:
                return f"manual action {row[0]} is still running (started {int(_age(row[1]))}s ago)"
            ev = conn.execute(select(oe_t.c.cycle_key, oe_t.c.ts)
                              .order_by(oe_t.c.ts.desc()).limit(1)).first()
            if (ev and ev[1] is not None and not str(ev[0]).startswith("manual-")
                    and _age(ev[1]) < window_s):
                return (f"cycle {ev[0]} placed orders {int(_age(ev[1]))}s ago "
                        f"(the scheduled rebalance is likely mid-chase)")
    except Exception as exc:  # noqa: BLE001
        log.warning("exec-conflict check failed (continuing)", extra={"error": str(exc)})
    return None


# ====================================================================== #
# Audit trail + top-level runner (what the CLI calls)
# ====================================================================== #
def _record_start(db_engine, *, action, mode, params, cycle_key) -> int | None:
    try:
        from sqlalchemy import insert
        from engine.db import manual_actions as t
        with db_engine.begin() as conn:
            r = conn.execute(insert(t).values(
                ts=datetime.now(timezone.utc), action=action, mode=mode, params=params,
                status="started", cycle_key=cycle_key))
            return r.inserted_primary_key[0] if r.inserted_primary_key else None
    except Exception as exc:  # noqa: BLE001 — the audit row must never block the action
        log.warning("manual_actions start row failed", extra={"error": str(exc)})
        return None


def _record_finish(db_engine, row_id, *, status, result) -> None:
    if row_id is None:
        return
    try:
        from sqlalchemy import update
        from engine.db import manual_actions as t
        with db_engine.begin() as conn:
            conn.execute(update(t).where(t.c.id == row_id).values(status=status, result=result))
    except Exception as exc:  # noqa: BLE001
        log.warning("manual_actions finish row failed", extra={"error": str(exc)})


def run_action(action: str, *, mode: str, client, broker, db_engine, settings,
               cycle_key: str | None = None, alert=None, force: bool = False,
               **params) -> dict:
    """Plan → journal → execute one manual action; returns the result summary.

    **Normal** mode refuses when the market is closed (the chase needs live quotes).
    **Express** trades regardless of the clock: session-aware sweeps (collared limits in
    RTH/ext-hours, queued markets otherwise) that are never cancelled. Both modes refuse —
    unless ``force`` — while another execution looks in-flight (:func:`_exec_conflict`);
    trading the same names as a mid-chase rebalance double-executes. The leverage action
    persists its sticky override *before* trading, so even a partially-filled lever-move is
    honored by the next rebalance.
    """
    if not force and mode != "express":
        try:
            clk = client.market_clock()
        except Exception as exc:  # noqa: BLE001 — can't read the clock → don't trade blind
            raise RuntimeError(f"market clock unreadable ({exc}) — refusing to trade blind") from exc
        if not clk.get("is_open"):
            raise RuntimeError(f"market closed — next open {clk.get('next_open')} "
                               f"(express mode trades any time)")
    if not force:
        conflict = _exec_conflict(db_engine)
        if conflict:
            raise RuntimeError(f"another execution appears in-flight — {conflict}. "
                               f"Wait for it to finish, or re-run with force to override.")

    cycle_key = cycle_key or f"manual-{action}-{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}"
    plan = build_plan(action, client, db_engine, settings, **params)
    row_id = _record_start(db_engine, action=action, mode=mode,
                           params=plan["params"], cycle_key=cycle_key)
    try:
        if action == "leverage":
            overrides.set(db_engine, "target_leverage", float(params["target"]))
        result = execute_plan(plan, mode, client=client, broker=broker, db_engine=db_engine,
                              settings=settings, cycle_key=cycle_key, alert=alert)
    except Exception as exc:
        _record_finish(db_engine, row_id, status="failed", result={"error": str(exc)})
        raise
    _record_finish(db_engine, row_id, status="done", result=result)
    log.warning("manual action complete", extra={"action": action, "mode": mode, **{
        k: v for k, v in result.items() if k != "lines"}})
    return result
