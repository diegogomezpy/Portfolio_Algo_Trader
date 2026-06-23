"""Unit tests for engine.execute.plan_orders — the pure order planner (3.3a).

Synthetic weights / positions / prices. Guards whole-share sizing, buy/sell deltas,
full liquidation of dropped names, the min-trade filter → pending_adjustments, the
sells-before-buys sequencing, the market-vs-limit order-type rule, and NAV scaling.
"""

from __future__ import annotations

from types import SimpleNamespace

import pandas as pd

from engine import execute


def _settings():
    return SimpleNamespace(execution=SimpleNamespace(
        min_trade_usd=500, large_cap_adv_threshold=50_000_000,
        mid_cap_adv_threshold=5_000_000, spread_threshold=0.001))


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


def test_order_type_limit_otherwise_at_mid():
    # Thin name (low ADV) ⇒ limit; limit price comes from the supplied mid.
    orders, _ = _plan({"AAPL": 0.05}, {}, {"AAPL": 100.0},
                      adv={"AAPL": 1_000_000}, spread={"AAPL": 0.0005},
                      mid_prices={"AAPL": 100.5})
    assert orders[0].order_type == "limit" and orders[0].limit_price == 100.5


def test_wide_spread_forces_limit_even_if_deep():
    orders, _ = _plan({"AAPL": 0.05}, {}, {"AAPL": 100.0},
                      adv={"AAPL": 100_000_000}, spread={"AAPL": 0.01})  # 1% > 0.1%
    assert orders[0].order_type == "limit"


def test_nav_scales_share_count():
    small, _ = _plan({"AAPL": 0.05}, {}, {"AAPL": 100.0}, nav=100_000)
    big, _ = _plan({"AAPL": 0.05}, {}, {"AAPL": 100.0}, nav=200_000)
    assert big[0].qty == 2 * small[0].qty                    # 100 vs 50
