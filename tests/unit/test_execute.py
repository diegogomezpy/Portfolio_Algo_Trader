"""Unit tests for engine.execute.plan_orders — the pure order planner (3.3a).

Synthetic weights / positions / prices. Guards whole-share sizing, buy/sell deltas,
full liquidation of dropped names, the min-trade filter → pending_adjustments, the
sells-before-buys sequencing, the market-vs-limit order-type rule, and NAV scaling.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pandas as pd
from sqlalchemy import create_engine, select

from engine import db, execute
from engine.alpaca_client import AlpacaAPIError


def _settings():
    return SimpleNamespace(execution=SimpleNamespace(
        min_trade_usd=500, large_cap_adv_threshold=50_000_000,
        mid_cap_adv_threshold=5_000_000, spread_threshold=0.001,
        marketable_limit_bps=50, max_spread_bps=150, ladder_steps=3, child_adv_pct=0.10))


# ---- execution-tactics pure helpers (docs/EXECUTION.md §5–§7) ---------------- #
def test_arrival_reference_spread_guard():
    assert execute.arrival_reference(99.95, 100.05, 100.10) == 100.0        # tight → mid
    assert execute.arrival_reference(94.73, 108.87, 95.1) == 95.1           # INBX wide → trade
    assert execute.arrival_reference(None, 108.87, 95.1) == 95.1            # one-sided → trade
    assert execute.arrival_reference(None, None, None) is None


def test_liquidity_tier_from_adv_and_spread():
    ex = _settings().execution
    assert execute.liquidity_tier(60_000_000, 99.99, 100.01, ex=ex) == "deep"     # big + tight
    assert execute.liquidity_tier(60_000_000, 99.0, 101.0, ex=ex) == "moderate"   # big but wide spread
    assert execute.liquidity_tier(10_000_000, 100.0, 100.1, ex=ex) == "moderate"  # mid-cap
    assert execute.liquidity_tier(1_000_000, 100.0, 100.1, ex=ex) == "thin"       # < $5M ADV


def test_pathological_spread_guard():
    ex = _settings().execution
    assert execute.is_pathological_spread(94.73, 108.87, ex=ex) is True     # ~14% > 150 bps → phantom
    assert execute.is_pathological_spread(99.9, 100.1, ex=ex) is False      # 20 bps → fine


def test_marketable_and_ladder_pricing():
    ex = _settings().execution                                             # 50 bps cross cap
    assert execute.marketable_price(100.0, "buy", ex=ex) == 100.5          # +0.5%
    assert execute.marketable_price(100.0, "sell", ex=ex) == 99.5          # -0.5%
    # ladder buy from mid (step 0) to the marketable cap (step 1)
    assert execute.ladder_price(100.0, 100.0, "buy", 0.0, ex=ex) == 100.0  # start at mid
    assert execute.ladder_price(100.0, 100.0, "buy", 1.0, ex=ex) == 100.5  # ends at the cap
    assert execute.ladder_price(100.0, 100.0, "buy", 0.5, ex=ex) == 100.25 # halfway


def test_child_qty_caps_thin_names_by_adv():
    ex = _settings().execution                                             # child_adv_pct = 10%
    # ADV $1M, price $100 → 10% = $100k = 1000 sh cap; want 5000 → sliced to 1000
    assert execute.child_qty(5000, 1_000_000, 100.0, ex=ex) == 1000
    # small order under the cap passes through whole
    assert execute.child_qty(200, 1_000_000, 100.0, ex=ex) == 200
    # no ADV / no pct → no slicing
    assert execute.child_qty(5000, None, 100.0, ex=ex) == 5000


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
                     limit_price=None, client_order_id=None, time_in_force="day"):
        if symbol in self.reject:
            raise AlpacaAPIError(symbol, "submit_order", "422 rejected")
        self._n += 1
        oid = f"oid-{self._n}"
        od = {"id": oid, "client_order_id": client_order_id, "symbol": symbol,
              "side": side, "qty": float(qty), "order_type": order_type,
              "status": "accepted", "limit_price": limit_price, "filled_qty": 0.0,
              "filled_avg_price": None, "submitted_at": "2026-07-01T16:30:00",
              "filled_at": None, "time_in_force": time_in_force}
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


def test_poll_get_order_transient_failure_does_not_crash():
    """A transient AlpacaAPIError on get_order during polling must not crash the cycle — the order
    stays open and is re-polled, and its fill is still recorded once the read recovers."""
    class _FlakyBroker(_FakeBroker):
        def __init__(self, **kw):
            super().__init__(**kw)
            self.fails_left = 2
        def get_order(self, order_id):
            if self.fails_left > 0:
                self.fails_left -= 1
                raise AlpacaAPIError(order_id, "get_order", "503 transient")
            return super().get_order(order_id)

    eng = _engine()
    broker = _FlakyBroker(fill_plan={"AAPL": [("filled", 50.0)]})
    report = execute.submit_and_track(
        [_po("AAPL", "buy", 50)], broker=broker, db_engine=eng, cycle_key="2026-07-01",
        poll_attempts=5, poll_interval_s=0, sleep=lambda s: None)
    assert report.submitted == 1 and report.filled == 1        # survived the transient reads
    fills = _rows(eng, db.fills)
    assert len(fills) == 1 and fills[0]["symbol"] == "AAPL"     # fill still recorded


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


def test_report_lines_per_symbol():
    """The single-pass report carries a per-symbol outcome line for the breakdown."""
    eng = _engine()
    broker = _FakeBroker(fill_plan={"AAPL": [("filled", 50.0)], "SLOW": [("new", 0), ("new", 0)]})
    rep = execute.submit_and_track(
        [_po("AAPL", "buy", 50), _po("SLOW", "buy", 10, "limit", 100.0)],
        broker=broker, db_engine=eng, cycle_key="2026-07-01", poll_attempts=2, **_NOSLEEP)
    lines = {l["symbol"]: l for l in rep.lines}
    assert lines["AAPL"]["status"] == "filled" and lines["AAPL"]["filled"] == 50
    assert lines["SLOW"]["status"] == "deferred" and lines["SLOW"]["filled"] == 0


# ====================================================================== #
# Chase mode — re-peg to the touch until filled, then a final market order
# ====================================================================== #
# (The legacy touch-chase mode and its tests were removed in the 2026-07 audit —
# the tiered executor is the sole production path; _single_pass remains as the
# no-quote fallback and is covered above.)


def test_order_state_feed_replaces_get_order():
    """When a live-feed order_state reader is supplied, fills are read from it and
    broker.get_order is never called (the feed replaces REST polling)."""
    eng = _engine()
    broker = _FakeBroker()

    def _boom(_oid):
        raise AssertionError("broker.get_order must not be called when the feed is supplied")
    broker.get_order = _boom

    def feed_state(oid):                                  # feed reports the order filled at once
        return {"id": oid, "symbol": "AAPL", "side": "buy", "qty": 50.0, "order_type": "market",
                "status": "filled", "limit_price": None, "filled_qty": 50.0,
                "filled_avg_price": 100.0, "submitted_at": "2026-07-01T16:30:00",
                "filled_at": "2026-07-01T16:31:00"}

    rep = execute.submit_and_track([_po("AAPL", "buy", 50)], broker=broker, db_engine=eng,
                                   cycle_key="c", sleep=lambda _s: None, order_state=feed_state)
    assert rep.filled == 1 and rep.submitted == 1
    assert sum(f["qty"] for f in _rows(eng, db.fills)) == 50.0


# ====================================================================== #
# _tiered — liquidity-tiered, patient-then-guaranteed execution (docs/EXECUTION.md §5–§8)
# ====================================================================== #
_T0 = datetime(2026, 7, 1, 17, 0, tzinfo=timezone.utc)   # 13:00 ET


class _Clock:
    """A now() that advances a fixed step per call, so the ladder progresses and the loop ends."""
    def __init__(self, start=_T0, step_s=1):
        self.t = start
        self.step = timedelta(seconds=step_s)

    def __call__(self):
        v = self.t
        self.t += self.step
        return v


def _ex(**kw):
    base = dict(min_trade_usd=500, large_cap_adv_threshold=50_000_000, mid_cap_adv_threshold=5_000_000,
                spread_threshold=0.001, marketable_limit_bps=50, max_spread_bps=150, ladder_steps=3,
                child_adv_pct=0.10, equity_repeg_s=30, close_buffer_s=0, poll_interval_s=2)
    base.update(kw)
    return SimpleNamespace(**base)


def _quote_of(d):
    return lambda s: d.get(s, (None, None, None))


def _tier(orders, broker, eng, quotes, adv, *, ex=None, clock_step=1, market_close=None, **kw):
    return execute.submit_and_track(
        orders, broker=broker, db_engine=eng, cycle_key="c", quote=_quote_of(quotes), adv=adv,
        ex=ex or _ex(), now=_Clock(step_s=clock_step),
        market_close=market_close or (_T0 + timedelta(hours=3)), sleep=lambda _s: None, **kw)


def test_tiered_deep_fills_marketable_now():
    eng = _engine(); broker = _FakeBroker()
    rep = _tier([_po("AAPL", "buy", 50)], broker, eng,
                {"AAPL": (99.99, 100.01, 100.0)}, {"AAPL": 60_000_000})    # tight + big → deep
    assert rep.filled == 1
    o = broker.submitted[0]
    assert o["order_type"] == "limit" and o["limit_price"] == 100.5        # ref 100 × (1+0.5%)


def test_tiered_moderate_starts_at_the_mid():
    # A moderate name (10M ADV, 0.4% spread) posts its first limit at the MID (ladder level 0),
    # not crossed — patience captures the half-spread when there's a natural counterparty.
    eng = _engine(); broker = _FakeBroker()
    rep = _tier([_po("MOD", "buy", 100)], broker, eng,
                {"MOD": (49.90, 50.10, 50.0)}, {"MOD": 10_000_000})
    assert broker.submitted[0]["limit_price"] == 50.0 and rep.filled == 1


def test_tiered_thin_slices_child_by_adv():
    # Thin name ($500k ADV): first child ≤ 10% of ADV = ⌊0.10×500k/50⌋ = 1,000 sh, not the full 3,000.
    eng = _engine(); broker = _FakeBroker()
    rep = _tier([_po("THN", "buy", 3000)], broker, eng,
                {"THN": (49.90, 50.10, 50.0)}, {"THN": 500_000})
    assert broker.submitted[0]["qty"] == 1000.0 and rep.filled == 1        # fills across sliced rounds


def test_tiered_pathological_posts_passive_and_defers():
    # INBX-like phantom ask (bid 94.73 / ask 108.87 ≈ 14%): every post sits at the guarded ref
    # (~the 95.1 print), NEVER crossing to 108.87; unfilled → cross-day, not the auction.
    eng = _engine(); broker = _FakeBroker(fill_plan={"INBX": [("accepted", 0)]})
    rep = _tier([_po("INBX", "buy", 106)], broker, eng,
                {"INBX": (94.73, 108.87, 95.1)}, {"INBX": 300_000},
                clock_step=30, market_close=_T0 + timedelta(seconds=120))
    assert broker.submitted and all(o["limit_price"] == 95.1 for o in broker.submitted)
    assert rep.filled == 0 and rep.auctioned == 0
    assert any(p["symbol"] == "INBX" for p in _rows(eng, db.pending_adjustments))   # cross-day


def test_tiered_auction_fallback_for_unfilled_liquid():
    # A moderate name that never fills intraday is routed to the closing auction as a CLS limit
    # (limit-on-close), not a naked market order and not cross-day.
    eng = _engine(); broker = _FakeBroker(fill_plan={"MOD": [("accepted", 0)]})
    rep = _tier([_po("MOD", "buy", 100)], broker, eng,
                {"MOD": (49.90, 50.10, 50.0)}, {"MOD": 10_000_000},
                clock_step=60, market_close=_T0 + timedelta(seconds=180))
    assert rep.auctioned == 1 and rep.filled == 0
    cls = [o for o in broker.submitted if o["time_in_force"] == "cls"]
    assert len(cls) == 1 and cls[0]["limit_price"] == 50.25                # LOC at the cap
    assert not any(p["symbol"] == "MOD" for p in _rows(eng, db.pending_adjustments))  # not cross-day


# ====================================================================== #
# order_events — chase telemetry for the execution visualizer (Phase 2)
# ====================================================================== #
def test_chase_events_recorded_for_deep_fill():
    # A deep name that fills round 1 leaves a "post" event (bid/ask/mid + the marketable limit) and
    # a "settle" event carrying the cumulative fill — the raw material for the chase board.
    eng = _engine(); broker = _FakeBroker()
    _tier([_po("AAPL", "buy", 50)], broker, eng,
          {"AAPL": (99.99, 100.01, 100.0)}, {"AAPL": 60_000_000})
    evs = _rows(eng, db.order_events)
    posts = [e for e in evs if e["event"] == "post"]
    settles = [e for e in evs if e["event"] == "settle"]
    assert len(posts) == 1 and len(settles) == 1
    p = posts[0]
    assert (p["symbol"], p["side"], p["tier"], p["round"]) == ("AAPL", "buy", "deep", "r1")
    assert p["bid"] == 99.99 and p["ask"] == 100.01 and p["mid"] == 100.0
    assert p["limit_price"] == 100.5 and p["qty"] == 50 and p["target_qty"] == 50 and p["order_id"]
    assert settles[0]["filled_qty"] == 50 and settles[0]["status"] == "filled"


def test_chase_event_captures_mid_for_moderate_ladder():
    # A moderate name posts its first limit AT the mid; the event captures bid/ask/mid so the board
    # can place the walking marker on the spread.
    eng = _engine(); broker = _FakeBroker()
    _tier([_po("MOD", "buy", 100)], broker, eng,
          {"MOD": (49.90, 50.10, 50.0)}, {"MOD": 10_000_000})
    posts = [e for e in _rows(eng, db.order_events) if e["event"] == "post"]
    assert posts and posts[0]["tier"] == "moderate"
    assert posts[0]["bid"] == 49.90 and posts[0]["ask"] == 50.10
    assert posts[0]["mid"] == 50.0 and posts[0]["limit_price"] == 50.0     # first post at the mid


def test_chase_event_recorded_on_reject():
    # A broker rejection emits a "reject" event (no order_id) so the board can flag the name.
    eng = _engine(); broker = _FakeBroker(reject={"BAD"})
    _tier([_po("BAD", "buy", 10)], broker, eng,
          {"BAD": (10.0, 10.02, 10.0)}, {"BAD": 60_000_000})
    rejects = [e for e in _rows(eng, db.order_events) if e["event"] == "reject"]
    assert len(rejects) == 1 and rejects[0]["symbol"] == "BAD" and rejects[0]["order_id"] is None


def test_chase_telemetry_failure_never_breaks_execution():
    # Telemetry is best-effort and failure-isolated: with order_events missing, the chase still
    # fills and reports normally (a telemetry write must never disrupt order placement).
    eng = _engine()
    with eng.begin() as c:
        c.exec_driver_sql("DROP TABLE order_events")
    broker = _FakeBroker()
    rep = _tier([_po("AAPL", "buy", 50)], broker, eng,
                {"AAPL": (99.99, 100.01, 100.0)}, {"AAPL": 60_000_000})
    assert rep.filled == 1
