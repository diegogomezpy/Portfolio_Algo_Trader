"""Unit tests for engine.execute.plan_orders — the pure order planner (3.3a).

Synthetic weights / positions / prices. Guards whole-share sizing, buy/sell deltas,
full liquidation of dropped names, the min-trade filter → pending_adjustments, the
sells-before-buys sequencing, the market-vs-limit order-type rule, and NAV scaling.
"""

from __future__ import annotations

from types import SimpleNamespace

import pandas as pd
from sqlalchemy import create_engine, select

from engine import db, execute
from engine.alpaca_client import AlpacaAPIError


def _settings():
    return SimpleNamespace(execution=SimpleNamespace(
        min_trade_usd=500, large_cap_adv_threshold=50_000_000,
        mid_cap_adv_threshold=5_000_000, spread_threshold=0.001,
        marketable_limit_bps=50))


def _plan(weights, positions, prices, *, nav=100_000, **kw):
    return execute.plan_orders(pd.Series(weights), positions, prices,
                               nav=nav, settings=_settings(), **kw)


def test_whole_share_sizing_and_buy():
    orders, pending = _plan({"AAPL": 0.05}, {}, {"AAPL": 100.0})
    assert pending == []
    assert len(orders) == 1
    o = orders[0]
    assert (o.symbol, o.side, o.qty) == ("AAPL", "buy", 50)   # $5,000 / $100
    assert o.notional == 5000.0


def test_sizing_floors_fractional_shares():
    orders, _ = _plan({"AAPL": 0.05}, {}, {"AAPL": 300.0})
    assert orders[0].qty == 16                                # floor(5000/300) = 16


def test_sell_to_reduce_existing_position():
    orders, _ = _plan({"AAPL": 0.05}, {"AAPL": 80}, {"AAPL": 100.0})
    assert (orders[0].side, orders[0].qty) == ("sell", 30)    # target 50, hold 80


def test_dropped_name_is_fully_liquidated():
    orders, _ = _plan({"AAPL": 0.05}, {"OLD": 40}, {"AAPL": 100.0, "OLD": 100.0})
    syms = {o.symbol: o for o in orders}
    assert syms["OLD"].side == "sell" and syms["OLD"].qty == 40


def test_no_trade_when_already_on_target():
    orders, pending = _plan({"AAPL": 0.05}, {"AAPL": 50}, {"AAPL": 100.0})
    assert orders == [] and pending == []


def test_sub_min_trade_goes_to_pending():
    # Target 50, hold 47 ⇒ 3 shares × $100 = $300 < min_trade_usd (500).
    orders, pending = _plan({"AAPL": 0.05}, {"AAPL": 47}, {"AAPL": 100.0})
    assert orders == []
    assert len(pending) == 1
    p = pending[0]
    assert p["symbol"] == "AAPL" and p["side"] == "buy" and p["qty"] == 3
    assert p["delta_usd"] == 300.0 and "min_trade_usd" in p["reason"]


def test_unpriceable_held_name_is_flagged_for_exit():
    _, pending = _plan({"AAPL": 0.05}, {"XYZ": 10}, {"AAPL": 100.0})  # no price for XYZ
    flag = [p for p in pending if p["symbol"] == "XYZ"]
    assert flag and flag[0]["side"] == "sell" and "no price" in flag[0]["reason"]


def test_sequencing_sells_before_buys_each_descending():
    weights = {"A": 0.05, "B": 0.03}                 # buys: A $5,000, B $3,000
    positions = {"C": 80, "D": 20}                   # sells: C $8,000, D $2,000 (dropped)
    prices = {s: 100.0 for s in "ABCD"}
    orders, _ = _plan(weights, positions, prices)
    assert [o.symbol for o in orders] == ["C", "D", "A", "B"]
    assert [o.side for o in orders] == ["sell", "sell", "buy", "buy"]


def test_order_type_market_for_deep_and_tight():
    orders, _ = _plan({"AAPL": 0.05}, {}, {"AAPL": 100.0},
                      adv={"AAPL": 100_000_000}, spread={"AAPL": 0.0005})
    assert orders[0].order_type == "market" and orders[0].limit_price is None


def test_order_type_marketable_limit_buy_above_mid():
    # Thin name (low ADV) ⇒ marketable limit; a BUY crosses up from the mid by marketable_limit_bps.
    orders, _ = _plan({"AAPL": 0.05}, {}, {"AAPL": 100.0},
                      adv={"AAPL": 1_000_000}, spread={"AAPL": 0.0005},
                      mid_prices={"AAPL": 100.0})
    assert orders[0].order_type == "limit" and orders[0].limit_price == 100.5   # 100.0 × (1 + 50bps)


def test_order_type_marketable_limit_sell_below_mid():
    # Trim an oversized position in a thin name ⇒ marketable limit SELL crosses down from the mid.
    orders, _ = _plan({"AAPL": 0.05}, {"AAPL": 80}, {"AAPL": 100.0},
                      adv={"AAPL": 1_000_000}, spread={"AAPL": 0.0005},
                      mid_prices={"AAPL": 100.0})
    assert orders[0].side == "sell"
    assert orders[0].order_type == "limit" and orders[0].limit_price == 99.5    # 100.0 × (1 − 50bps)


def test_wide_spread_forces_limit_even_if_deep():
    orders, _ = _plan({"AAPL": 0.05}, {}, {"AAPL": 100.0},
                      adv={"AAPL": 100_000_000}, spread={"AAPL": 0.01})  # 1% > 0.1%
    assert orders[0].order_type == "limit"


def test_nav_scales_share_count():
    small, _ = _plan({"AAPL": 0.05}, {}, {"AAPL": 100.0}, nav=100_000)
    big, _ = _plan({"AAPL": 0.05}, {}, {"AAPL": 100.0}, nav=200_000)
    assert big[0].qty == 2 * small[0].qty                    # 100 vs 50


# ====================================================================== #
# submit_and_track (3.3b) — fake broker + in-memory sqlite
# ====================================================================== #
class _FakeBroker:
    """Programmable broker: ``fill_plan[symbol]`` is the (status, filled_qty) sequence
    successive get_order calls walk through (sticking at the last); default = fills fully
    on the first poll. ``reject`` symbols raise AlpacaAPIError on submit."""

    def __init__(self, fill_plan=None, reject=()):
        self.fill_plan = fill_plan or {}
        self.reject = set(reject)
        self.submitted = []
        self.cancelled = []
        self._orders = {}
        self._seq = {}
        self._n = 0

    def submit_order(self, symbol, qty, side, *, order_type="market",
                     limit_price=None, client_order_id=None):
        if symbol in self.reject:
            raise AlpacaAPIError(symbol, "submit_order", "422 rejected")
        self._n += 1
        oid = f"oid-{self._n}"
        od = {"id": oid, "client_order_id": client_order_id, "symbol": symbol,
              "side": side, "qty": float(qty), "order_type": order_type,
              "status": "accepted", "limit_price": limit_price, "filled_qty": 0.0,
              "filled_avg_price": None, "submitted_at": "2026-07-01T16:30:00",
              "filled_at": None}
        self._orders[oid] = od
        self.submitted.append(dict(od))
        self._seq[oid] = iter(self.fill_plan.get(symbol, [("filled", float(qty))]))
        return dict(od)

    def get_order(self, order_id):
        od = self._orders[order_id]
        try:
            status, fq = next(self._seq[order_id])
            od["status"] = status
            od["filled_qty"] = float(fq)
            if fq > 0:
                od["filled_avg_price"] = 100.0
                od["filled_at"] = "2026-07-01T16:31:00"
        except StopIteration:
            pass                                    # stick at last reported state
        return dict(od)

    def cancel_order(self, order_id):
        self.cancelled.append(order_id)
        od = self._orders[order_id]
        if od["status"] != "filled":
            od["status"] = "canceled"


def _engine():
    eng = create_engine("sqlite://")
    db.create_all(eng)
    return eng


def _rows(eng, table):
    with eng.connect() as c:
        return [dict(r) for r in c.execute(select(table)).mappings().all()]


def _po(symbol, side, qty, otype="market", lp=None, notional=None):
    return execute.PlannedOrder(symbol, side, qty, otype, lp,
                                notional if notional is not None else qty * 100.0)


_NOSLEEP = dict(sleep=lambda *_: None, poll_interval_s=0)


def test_submit_records_orders_and_fills():
    eng = _engine()
    broker = _FakeBroker()                          # both fill fully on first poll
    rep = execute.submit_and_track(
        [_po("AAPL", "buy", 50), _po("MSFT", "buy", 30, "limit", 100.0)],
        broker=broker, db_engine=eng, cycle_key="2026-07-01", **_NOSLEEP)
    assert (rep.submitted, rep.filled, rep.rejected, rep.deferred) == (2, 2, 0, 0)
    orders = _rows(eng, db.orders)
    assert len(orders) == 2 and all(o["status"] == "filled" for o in orders)
    assert all(o["rebalance_cycle"] == "2026-07-01" for o in orders)
    assert {o["client_order_id"] for o in orders} == {"2026-07-01:AAPL:buy", "2026-07-01:MSFT:buy"}
    assert len(_rows(eng, db.fills)) == 2


def test_idempotent_skips_already_submitted_this_cycle():
    eng = _engine()
    # Simulate a prior partial run: AAPL buy already recorded for this cycle.
    from sqlalchemy import insert
    with eng.begin() as c:
        c.execute(insert(db.orders).values(
            id="pre-1", client_order_id="2026-07-01:AAPL:buy", rebalance_cycle="2026-07-01",
            symbol="AAPL", side="buy", status="filled"))
    broker = _FakeBroker()
    rep = execute.submit_and_track([_po("AAPL", "buy", 50)], broker=broker,
                                   db_engine=eng, cycle_key="2026-07-01", **_NOSLEEP)
    assert rep.submitted == 0 and broker.submitted == []     # not re-submitted


def test_rejection_defers_to_pending_and_alerts():
    eng = _engine()
    alerts = []
    broker = _FakeBroker(reject={"BAD"})
    rep = execute.submit_and_track([_po("BAD", "buy", 10)], broker=broker, db_engine=eng,
                                   cycle_key="2026-07-01", alert=alerts.append, **_NOSLEEP)
    assert rep.rejected == 1 and rep.submitted == 0
    pend = _rows(eng, db.pending_adjustments)
    assert len(pend) == 1 and pend[0]["symbol"] == "BAD" and pend[0]["reason"].startswith("rejected")
    assert len(alerts) == 1


def test_unfilled_order_cancelled_and_residual_rolled():
    eng = _engine()
    broker = _FakeBroker(fill_plan={"SLOW": [("new", 0), ("new", 0)]})
    rep = execute.submit_and_track([_po("SLOW", "buy", 10, "limit", 100.0)], broker=broker,
                                   db_engine=eng, cycle_key="2026-07-01",
                                   poll_attempts=2, **_NOSLEEP)
    assert broker.cancelled == ["oid-1"]
    assert rep.filled == 0 and rep.partial == 0
    pend = _rows(eng, db.pending_adjustments)
    assert pend[0]["symbol"] == "SLOW" and pend[0]["qty"] == 10
    assert pend[0]["reason"] == "unfilled at session end"


def test_partial_fill_records_fill_and_rolls_residual():
    eng = _engine()
    broker = _FakeBroker(fill_plan={"PART": [("partially_filled", 4)]})
    rep = execute.submit_and_track([_po("PART", "buy", 10, "limit", 100.0)], broker=broker,
                                   db_engine=eng, cycle_key="2026-07-01",
                                   poll_attempts=2, **_NOSLEEP)
    assert rep.partial == 1
    fills = _rows(eng, db.fills)
    assert len(fills) == 1 and fills[0]["qty"] == 4
    pend = _rows(eng, db.pending_adjustments)
    assert pend[0]["qty"] == 6 and pend[0]["reason"] == "unfilled at session end"


def test_incoming_pending_is_persisted():
    eng = _engine()
    pending_in = [{"symbol": "TINY", "side": "buy", "delta_usd": 300.0, "qty": 3,
                   "reason": "below min_trade_usd (500)"}]
    rep = execute.submit_and_track([_po("AAPL", "buy", 50)], broker=_FakeBroker(),
                                   db_engine=eng, cycle_key="2026-07-01",
                                   pending=pending_in, **_NOSLEEP)
    assert rep.deferred == 1
    pend = _rows(eng, db.pending_adjustments)
    assert len(pend) == 1 and pend[0]["symbol"] == "TINY"
