"""engine.manual_exec — the execution console's planners and executors.

Planners are pure reads over a fake client; execution runs against the fake broker +
sqlite, asserting the same journaling contract as a rebalance (orders rows, chase
telemetry, manual_actions audit trail) and the market-closed refusal.
"""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine, select

from engine import db, manual_exec, overrides
from engine.covered_calls import _scale_legs

_SPY_SHORT = "SPY260731C00764000"
_SPY_LONG = "SPY260731C00779000"


class _Client:
    """Alpaca read surface: equities + the SPY spread + prices + an open market."""

    def __init__(self, *, positions=None, prices=None, is_open=True, equity=1_000_000.0):
        self._positions = positions if positions is not None else [
            {"symbol": "AAPL", "qty": 400, "asset_class": "us_equity"},
            {"symbol": "XOM", "qty": 900, "asset_class": "us_equity"},
            {"symbol": _SPY_SHORT, "qty": -7, "market_value": -1638.0, "asset_class": "us_option"},
            {"symbol": _SPY_LONG, "qty": 7, "market_value": 543.9, "asset_class": "us_option"},
        ]
        self._prices = prices or {"AAPL": 212.0, "XOM": 112.0, "SPY": 624.0}
        self._is_open = is_open
        self._equity = equity

    def all_positions(self):
        return list(self._positions)

    def latest_trade(self, sym):
        return self._prices[sym]

    def latest_nbbo(self, sym):
        px = self._prices[sym]
        return px - 0.05, px + 0.05

    def latest_option_quote(self, sym):
        return 2.30, 2.40

    def account(self):
        return {"equity": self._equity}

    def market_clock(self):
        return {"is_open": self._is_open, "next_open": "2026-07-06T09:30:00-04:00",
                "next_close": "2026-07-06T16:00:00-04:00"}


class _Broker:
    """Records submissions; everything fills instantly at the limit (or a fake print)."""

    def __init__(self):
        self.orders = []
        self._n = 0

    def submit_order(self, symbol, qty, side, *, order_type="market", limit_price=None,
                     client_order_id=None, time_in_force="day"):
        self._n += 1
        od = {"id": f"o{self._n}", "client_order_id": client_order_id, "symbol": symbol,
              "side": side, "qty": qty, "order_type": order_type, "status": "filled",
              "limit_price": limit_price, "filled_qty": qty,
              "filled_avg_price": limit_price or 100.0, "submitted_at": None, "filled_at": None}
        self.orders.append(od)
        return od

    def submit_option_order(self, option_symbol, contracts, side, *, position_intent,
                            order_type="limit", limit_price=None, client_order_id=None):
        self._n += 1
        od = {"id": f"c{self._n}", "option_symbol": option_symbol, "symbol": option_symbol,
              "side": side, "qty": contracts, "order_type": order_type, "status": "filled",
              "limit_price": limit_price, "filled_qty": contracts,
              "filled_avg_price": limit_price or 2.34, "client_order_id": client_order_id,
              "submitted_at": None, "filled_at": None}
        self.orders.append(od)
        return od

    def get_order(self, order_id):
        return next(o for o in self.orders if o["id"] == order_id)

    def cancel_order(self, order_id):
        pass


class _Settings:
    class portfolio:
        max_leverage = 2.0
        target_leverage = 2.0

    class execution:
        min_trade_usd = 200.0
        large_cap_adv_threshold = 1e9
        spread_threshold = 0.001
        marketable_limit_bps = 10.0
        equity_repeg_s = 0.0
        poll_interval_s = 0.0
        close_buffer_s = 0.0
        ladder_steps = 3

    class covered_calls:
        overlay_mode = "index"
        min_bid_frac = 0.7


def _eng():
    eng = create_engine("sqlite://")
    db.create_all(eng)
    return eng


# ---------------------------------------------------------------- planners
def test_plan_liquidate_pro_rata_with_overlay_trim():
    plan = manual_exec.plan_liquidate(_Client(), 25)
    by = {o["symbol"]: o for o in plan["orders"]}
    assert by["AAPL"]["qty"] == 100 and by["XOM"]["qty"] == 225      # floor(qty × 25%)
    assert all(o["side"] == "sell" for o in plan["orders"])
    assert plan["orders"][0]["est_notional"] >= plan["orders"][1]["est_notional"]  # desc
    assert plan["overlay"] == {"market": "SPY", "contracts": 7, "close_contracts": 2}
    assert plan["totals"]["sell_notional"] == pytest.approx(100 * 212 + 225 * 112)


def test_plan_liquidate_100_closes_everything():
    plan = manual_exec.plan_liquidate(_Client(), 100)
    assert {o["symbol"]: o["qty"] for o in plan["orders"]} == {"AAPL": 400, "XOM": 900}
    assert plan["overlay"]["close_contracts"] == 7
    assert any("ENTIRE" in w for w in plan["warnings"])


def test_plan_liquidate_validates_pct():
    with pytest.raises(ValueError, match="pct"):
        manual_exec.plan_liquidate(_Client(), 0)
    with pytest.raises(ValueError, match="pct"):
        manual_exec.plan_liquidate(_Client(), 101)


def test_plan_trade_buy_and_sell_sizing():
    c = _Client(prices={"AAPL": 212.0, "XOM": 112.0, "NVDA": 140.0, "SPY": 624.0})
    buy = manual_exec.plan_trade(c, "NVDA", "buy", usd=5000)
    assert buy["orders"][0]["qty"] == 35                              # floor(5000/140)
    assert any("off-model" in w for w in buy["warnings"])             # not in the book
    sell = manual_exec.plan_trade(c, "XOM", "sell", pct=50)
    assert sell["orders"][0]["qty"] == 450
    assert sell["warnings"] == []
    with pytest.raises(ValueError, match="less than 1 share"):
        manual_exec.plan_trade(c, "NVDA", "buy", usd=5)
    with pytest.raises(ValueError, match="no .* position"):
        manual_exec.plan_trade(c, "NVDA", "sell", pct=50)


def test_plan_leverage_scales_and_caps():
    # gross = 400×212 + 900×112 = 185,600 on 1M equity → 0.1856×; target 0.37 ≈ double
    c = _Client()
    plan = manual_exec.plan_leverage(c, None, _Settings, 0.3712)
    by = {o["symbol"]: o for o in plan["orders"]}
    assert by["AAPL"]["side"] == "buy" and by["AAPL"]["qty"] == 400   # 2× the book
    assert by["XOM"]["qty"] == 900
    assert plan["totals"]["current_leverage"] == pytest.approx(0.186, abs=1e-3)
    assert plan["overlay"]["close_contracts"] == 0                    # lever-up: spread untouched
    assert any("sticky" in w for w in plan["warnings"])
    with pytest.raises(ValueError, match="max_leverage"):
        manual_exec.plan_leverage(c, None, _Settings, 2.5)


def test_plan_leverage_down_trims_overlay():
    plan = manual_exec.plan_leverage(_Client(), None, _Settings, 0.0928)   # halve the book
    assert all(o["side"] == "sell" for o in plan["orders"])
    assert plan["overlay"]["close_contracts"] == 4                    # round(7 × 0.5)


# ---------------------------------------------------------------- overlay scaling
def test_scale_legs_keeps_spread_balanced_and_drops_zeros():
    shorts = [{"symbol": _SPY_SHORT, "qty": -7}]
    longs = [{"symbol": _SPY_LONG, "qty": 7}]
    assert _scale_legs(shorts, 2 / 7)[0]["qty"] == -2
    assert _scale_legs(longs, 2 / 7)[0]["qty"] == 2
    assert _scale_legs([{"symbol": _SPY_SHORT, "qty": -1}], 0.25) == []


# ---------------------------------------------------------------- execution + audit
def test_run_action_express_places_market_orders_and_journals():
    eng, broker = _eng(), _Broker()
    res = manual_exec.run_action("liquidate", mode="express", client=_Client(), broker=broker,
                                 db_engine=eng, settings=_Settings, pct=25)
    equity_orders = [o for o in broker.orders if o["symbol"] in ("AAPL", "XOM")]
    assert {o["order_type"] for o in equity_orders} == {"market"}
    assert res["filled"] == 2 and res["overlay_closed"] >= 1
    assert res["cycle_key"].startswith("manual-liquidate-")
    with eng.connect() as c:
        rows = c.execute(select(db.orders.c.symbol, db.orders.c.rebalance_cycle)).all()
        events = c.execute(select(db.order_events.c.symbol, db.order_events.c.tier,
                                  db.order_events.c.event)).all()
        (audit,) = c.execute(select(db.manual_actions)).mappings().all()
    assert {r[1] for r in rows if r[0] in ("AAPL", "XOM")} == {res["cycle_key"]}
    assert {e[1] for e in events if e[0] == "AAPL"} == {"express"}     # chase board tactic label
    assert {e[2] for e in events if e[0] == "AAPL"} == {"post", "settle"}
    assert audit["action"] == "liquidate" and audit["status"] == "done"
    assert audit["result"]["filled"] == 2


def test_run_action_normal_refuses_when_market_closed_express_trades_anyway():
    eng = _eng()
    with pytest.raises(RuntimeError, match="market closed"):
        manual_exec.run_action("liquidate", mode="normal", client=_Client(is_open=False),
                               broker=_Broker(), db_engine=eng, settings=_Settings, pct=10)
    with eng.connect() as c:                     # refused before planning → no audit row
        assert c.execute(select(db.manual_actions)).all() == []
    # Express ignores the clock entirely — same closed market, orders go out.
    res = manual_exec.run_action("liquidate", mode="express", client=_Client(is_open=False),
                                 broker=_Broker(), db_engine=eng, settings=_Settings, pct=10)
    assert res["submitted"] == 2


def test_single_pass_leave_open_keeps_orders_working():
    # cancel_leftover=False (express off-hours): unfilled market orders are NOT cancelled at
    # poll end — they stay queued for the next open, reported as "queued", no pending rows.
    from engine.execute import PlannedOrder, submit_and_track

    class _SleepyBroker(_Broker):
        """Orders never fill (market closed); records any cancel attempts."""

        def __init__(self):
            super().__init__()
            self.cancelled = []

        def submit_order(self, symbol, qty, side, **kw):
            od = super().submit_order(symbol, qty, side, **kw)
            od.update(status="new", filled_qty=0, filled_avg_price=None)
            return od

        def cancel_order(self, order_id):
            self.cancelled.append(order_id)

    eng, broker = _eng(), _SleepyBroker()
    rep = submit_and_track([PlannedOrder("AAPL", "sell", 100, "market", None, 21200.0)],
                           broker=broker, db_engine=eng, cycle_key="manual-x",
                           poll_attempts=2, sleep=lambda s: None, cancel_leftover=False)
    assert broker.cancelled == []                            # left working, not cancelled
    (line,) = rep.lines
    assert line["status"] == "queued" and rep.deferred == 0  # no pending_adjustments rolled


def test_run_action_records_failure():
    eng = _eng()

    class _NoPos(_Client):
        def all_positions(self):
            return []

    with pytest.raises(ValueError, match="no equity positions"):
        manual_exec.run_action("liquidate", mode="express", client=_NoPos(), broker=_Broker(),
                               db_engine=eng, settings=_Settings, pct=10)


def test_run_action_leverage_sets_sticky_override():
    eng = _eng()
    manual_exec.run_action("leverage", mode="express", client=_Client(), broker=_Broker(),
                           db_engine=eng, settings=_Settings, target=0.3712)
    assert overrides.get(eng, "target_leverage") == pytest.approx(0.3712)
    assert overrides.effective_target_leverage(eng, _Settings) == pytest.approx(0.3712)
    overrides.clear(eng, "target_leverage")
    assert overrides.effective_target_leverage(eng, _Settings) == 2.0   # back to settings


def test_effective_target_leverage_clamps_to_cap():
    eng = _eng()
    overrides.set(eng, "target_leverage", 9.9)
    assert overrides.effective_target_leverage(eng, _Settings) == 2.0   # max_leverage clamp
