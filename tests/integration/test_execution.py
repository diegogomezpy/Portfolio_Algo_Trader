"""Integration test for the Phase 3 execution cycle (scripts.run_eod.run_cycle).

Drives the full wiring — reconcile → holiday gate → targets → risk gate → execute →
monitor — with a fake Alpaca client, a fake broker, in-memory sqlite, and a stubbed
target producer (so the factor/optimize math isn't re-exercised here; the glue is).
Real settings.yaml is loaded so the risk-gate caps and execution thresholds are real.
"""

from __future__ import annotations

from datetime import date

import pandas as pd
import pytest
from sqlalchemy import create_engine, select

from engine import db
from engine.alpaca_client import AlpacaAPIError
from engine.config import load_settings
from scripts import run_eod


# --------------------------------------------------------------- fakes ----
class _FakeClient:
    def __init__(self, positions=None, equity=100_000.0, cash=5_000.0, raise_positions=False,
                 calendar=None):
        self._positions = positions or {}        # {symbol: (qty, market_value)}
        self._equity, self._cash = equity, cash
        self._raise = raise_positions
        self._calendar = calendar or []          # ISO date strings of trading days

    def account(self):
        return {"equity": self._equity, "cash": self._cash}

    def all_positions(self):
        if self._raise:
            raise AlpacaAPIError("*", "all_positions", "503 unreachable")
        return [{"symbol": s, "qty": q, "market_value": mv}
                for s, (q, mv) in self._positions.items()]

    def market_calendar(self, start, end):
        return [{"date": d} for d in self._calendar if start <= d <= end]


class _FakeBroker:
    def __init__(self):
        self.submitted = []
        self._orders = {}
        self._n = 0

    def submit_order(self, symbol, qty, side, *, order_type="market", limit_price=None,
                     client_order_id=None):
        self._n += 1
        oid = f"oid-{self._n}"
        od = {"id": oid, "client_order_id": client_order_id, "symbol": symbol, "side": side,
              "qty": float(qty), "order_type": order_type, "status": "filled",
              "limit_price": limit_price, "filled_qty": float(qty), "filled_avg_price": 100.0,
              "submitted_at": "2026-07-01T16:30:00", "filled_at": "2026-07-01T16:31:00"}
        self._orders[oid] = od
        self.submitted.append(dict(od))
        return dict(od)

    def get_order(self, order_id):
        return dict(self._orders[order_id])

    def cancel_order(self, order_id):
        pass


def _engine():
    eng = create_engine("sqlite://")
    db.create_all(eng)
    return eng


def _rows(eng, table):
    with eng.connect() as c:
        return [dict(r) for r in c.execute(select(table)).mappings()]


def _targets(weights, sectors, prices):
    """A stub targets_fn returning fixed weights (signature matches compute_targets)."""
    def _fn(settings, as_of, *, db_engine=None):
        return run_eod.TargetPlan(
            weights=pd.Series(weights), prices=prices,
            universe=set(weights), sector_map=pd.Series(sectors))
    return _fn


# ------------------------------------------------------------- cycles ----
def test_run_cycle_executes_and_persists():
    eng = _engine()
    broker = _FakeBroker()
    targets = _targets(
        {"AAPL": 0.05, "MSFT": 0.04},
        {"AAPL": "Information Technology", "MSFT": "Information Technology"},
        {"AAPL": 100.0, "MSFT": 200.0})
    res = run_eod.run_cycle(
        client=_FakeClient(), broker=broker, db_engine=eng, settings=load_settings(),
        as_of=date(2026, 7, 1), force=True, targets_fn=targets)

    assert res.status == "executed"
    assert res.exec_report.submitted == 2 and res.exec_report.filled == 2
    orders = _rows(eng, db.orders)
    assert {o["symbol"] for o in orders} == {"AAPL", "MSFT"}
    # Deployable base = 2.0× the $100k equity (target_leverage, D32): 10000/100, 8000/200.
    assert {o["qty"] for o in orders} == {100.0, 40.0}
    assert len(_rows(eng, db.fills)) == 2
    rlog = _rows(eng, db.rebalance_log)
    assert len(rlog) == 1 and rlog[0]["risk_gate_passed"] is True
    assert len(_rows(eng, db.snapshots)) == 1               # monitor wrote one


def test_run_cycle_overlay_closes_then_writes_around_equity():
    eng = _engine()
    broker = _FakeBroker()
    # Fake client reports a held equity position post-trade (what calls will cover).
    client = _FakeClient(positions={"AAPL": (100, 10_000.0)})
    seq = []

    def _close(client, broker, db_engine, *, as_of=None, alert=None):
        seq.append("close")
        return ["closed-1"]

    def _write(client, broker, db_engine, holdings_shares, *, settings, as_of, price_panel, alert=None):
        seq.append(("write", dict(holdings_shares)))
        return (["wrote-1"], [])

    # AAPL is already at target (held 100), so MSFT (unheld) is what actually trades.
    res = run_eod.run_cycle(
        client=client, broker=broker, db_engine=eng, settings=load_settings(),
        as_of=date(2026, 7, 1), force=True, overlay=True,
        targets_fn=_targets({"AAPL": 0.05, "MSFT": 0.04},
                            {"AAPL": "Information Technology", "MSFT": "Information Technology"},
                            {"AAPL": 100.0, "MSFT": 200.0}),
        close_calls_fn=_close, write_calls_fn=_write)

    assert res.status == "executed"
    assert res.calls_closed == 1 and res.calls_written == 1
    # close ran first, write ran last, with the post-trade equity holdings (the held AAPL).
    assert seq[0] == "close" and seq[-1][0] == "write"
    assert seq[-1][1] == {"AAPL": 100.0}
    assert any(o["symbol"] == "MSFT" for o in _rows(eng, db.orders))  # equity ran in between


def test_run_cycle_no_overlay_skips_call_legs():
    eng = _engine()
    called = []
    res = run_eod.run_cycle(
        client=_FakeClient(), broker=_FakeBroker(), db_engine=eng, settings=load_settings(),
        as_of=date(2026, 7, 1), force=True, overlay=False,
        targets_fn=_targets({"AAPL": 0.05}, {"AAPL": "Information Technology"}, {"AAPL": 100.0}),
        close_calls_fn=lambda *a, **k: called.append("close") or [],
        write_calls_fn=lambda *a, **k: called.append("write") or ([], []))
    assert res.status == "executed" and res.calls_closed == 0 and res.calls_written == 0
    assert called == []                                       # overlay legs not invoked


def test_run_cycle_blocked_by_risk_gate_submits_nothing():
    eng = _engine()
    broker = _FakeBroker()
    # 0.50 in one name breaks the 5% single-name cap → gate blocks.
    targets = _targets({"AAPL": 0.50}, {"AAPL": "Information Technology"}, {"AAPL": 100.0})
    res = run_eod.run_cycle(
        client=_FakeClient(), broker=broker, db_engine=eng, settings=load_settings(),
        as_of=date(2026, 7, 1), force=True, targets_fn=targets)

    assert res.status == "blocked_risk" and res.risk.approved is False
    assert broker.submitted == [] and _rows(eng, db.orders) == []
    rlog = _rows(eng, db.rebalance_log)
    assert len(rlog) == 1 and rlog[0]["risk_gate_passed"] is False   # blocked cycle is logged


def test_run_cycle_skips_non_trading_day():
    eng = _engine()
    broker = _FakeBroker()
    called = {"targets": False}

    def _targets_fn(settings, as_of, *, db_engine=None):
        called["targets"] = True
        return run_eod.TargetPlan(pd.Series({"AAPL": 0.05}), {"AAPL": 100.0})

    res = run_eod.run_cycle(
        client=_FakeClient(), broker=broker, db_engine=eng, settings=load_settings(),
        as_of=date(2026, 7, 4), force=False, targets_fn=_targets_fn,
        trading_day_fn=lambda client, d: False)

    assert res.status == "not_trading_day"
    assert called["targets"] is False and broker.submitted == []


def test_run_cycle_blocks_when_alpaca_unreachable():
    eng = _engine()
    with pytest.raises(AlpacaAPIError):
        run_eod.run_cycle(
            client=_FakeClient(raise_positions=True), broker=_FakeBroker(), db_engine=eng,
            settings=load_settings(), as_of=date(2026, 7, 1), force=True,
            targets_fn=_targets({"AAPL": 0.05}, {"AAPL": "x"}, {"AAPL": 100.0}))


# ============================== scheduler (3.7) ============================== #
def test_is_first_trading_day_of_month():
    # July 2026 trading days start Wed Jul 1.
    client = _FakeClient(calendar=["2026-07-01", "2026-07-02", "2026-07-03"])
    assert run_eod.is_first_trading_day_of_month(client, date(2026, 7, 1)) is True
    assert run_eod.is_first_trading_day_of_month(client, date(2026, 7, 2)) is False


def test_daily_job_rebalances_on_first_trading_day():
    eng = _engine()
    broker = _FakeBroker()
    ingested = []
    res = run_eod.daily_job(
        client=_FakeClient(), broker=broker, db_engine=eng, settings=load_settings(),
        as_of=date(2026, 7, 1),
        ingest_fn=ingested.append,
        targets_fn=_targets({"AAPL": 0.05}, {"AAPL": "Information Technology"}, {"AAPL": 100.0}),
        trading_day_fn=lambda c, d: True, first_trading_day_fn=lambda c, d: True)
    assert res.status == "rebalanced" and res.cycle.status == "executed"
    assert ingested == ["2026-07-01"]                    # data refreshed before the cycle
    assert len(_rows(eng, db.orders)) == 1


def test_daily_job_monitors_on_non_rebalance_day():
    eng = _engine()
    broker = _FakeBroker()
    res = run_eod.daily_job(
        client=_FakeClient(), broker=broker, db_engine=eng, settings=load_settings(),
        as_of=date(2026, 7, 2),
        trading_day_fn=lambda c, d: True, first_trading_day_fn=lambda c, d: False,
        rebalanced_this_month_fn=lambda c, d: True)    # the month's rebalance already landed
    assert res.status == "monitored"
    assert broker.submitted == [] and _rows(eng, db.orders) == []   # no trading
    assert len(_rows(eng, db.snapshots)) == 1                       # monitor still snapshots


def test_daily_job_runs_overlay_check_on_non_rebalance_day():
    eng = _engine()
    calls = []
    res = run_eod.daily_job(
        client=_FakeClient(), broker=_FakeBroker(), db_engine=eng, settings=load_settings(),
        as_of=date(2026, 6, 18), overlay=True,
        trading_day_fn=lambda c, d: True, first_trading_day_fn=lambda c, d: False,
        rebalanced_this_month_fn=lambda c, d: True,      # the month's rebalance already landed
        options_check_fn=lambda *a, **k: calls.append("opts"))
    assert res.status == "monitored" and calls == ["opts"]   # overlay safety pass ran


def test_daily_job_catches_up_when_month_missed():
    """A trading day past the 1st with no approved rebalance this month → run it now (catch-up)."""
    eng = _engine()
    broker = _FakeBroker()
    res = run_eod.daily_job(
        client=_FakeClient(), broker=broker, db_engine=eng, settings=load_settings(),
        as_of=date(2026, 7, 2),                          # not the 1st…
        targets_fn=_targets({"AAPL": 0.05}, {"AAPL": "Information Technology"}, {"AAPL": 100.0}),
        trading_day_fn=lambda c, d: True, first_trading_day_fn=lambda c, d: False,
        rebalanced_this_month_fn=lambda c, d: False)     # …and the month hasn't rebalanced yet
    assert res.status == "rebalanced" and res.cycle.status == "executed"
    assert len(_rows(eng, db.orders)) == 1               # it actually traded (caught up)
    assert _rows(eng, db.rebalance_log)[0]["trigger_reason"] == "monthly_catchup"


def test_daily_job_alerts_and_survives_when_rebalance_raises():
    """If the rebalance raises (e.g. Alpaca down), the job alerts and returns rebalance_failed."""
    eng = _engine()
    sent = []
    res = run_eod.daily_job(
        client=_FakeClient(raise_positions=True),        # reconcile inside run_cycle will raise
        broker=_FakeBroker(), db_engine=eng, settings=load_settings(),
        as_of=date(2026, 7, 1),
        targets_fn=_targets({"AAPL": 0.05}, {"AAPL": "Information Technology"}, {"AAPL": 100.0}),
        trading_day_fn=lambda c, d: True, first_trading_day_fn=lambda c, d: True,
        rebalanced_this_month_fn=lambda c, d: False, alert=sent.append)
    assert res.status == "rebalance_failed"
    assert any("failed to complete" in m for m in sent)  # a loud alert went out
    assert _rows(eng, db.orders) == []                   # nothing traded


def test_rebalance_established_this_month():
    from datetime import datetime
    from sqlalchemy import insert
    eng = _engine()
    est = run_eod.rebalance_established_this_month
    assert est(eng, date(2026, 7, 2)) is False           # empty → False
    with eng.begin() as c:
        # a blocked (passed=False) attempt this month does NOT count as rebalanced
        c.execute(insert(db.rebalance_log).values(
            ts=datetime(2026, 7, 1, 20, 10), trigger_reason="monthly",
            target_weights={"AAPL": 0.05}, risk_gate_passed=False, risk_gate_reason="blocked"))
        # an APPROVED row this month, but the orders never filled → the book is still empty
        c.execute(insert(db.rebalance_log).values(
            ts=datetime(2026, 7, 1, 20, 10), trigger_reason="monthly",
            target_weights={"AAPL": 0.05}, risk_gate_passed=True, risk_gate_reason="ok"))
        c.execute(insert(db.snapshots).values(
            ts=datetime(2026, 7, 1, 20, 11), nav=100_000.0, cash=100_000.0, last_equity=100_000.0,
            weights={}, positions={}, drift=None))       # empty book
    assert est(eng, date(2026, 7, 2)) is False           # gate passed but no positions → NOT done
    with eng.begin() as c:                               # now the orders fill → book holds equity
        c.execute(insert(db.snapshots).values(
            ts=datetime(2026, 7, 1, 20, 12), nav=100_000.0, cash=5_000.0, last_equity=100_000.0,
            weights={"AAPL": 0.25}, positions={"AAPL": 100}, drift=0.0))
    assert est(eng, date(2026, 7, 2)) is True            # approved + positions established → done
    assert est(eng, date(2026, 8, 3)) is False           # next month → False


def test_catchup_requires_substantial_fill_not_one_position():
    """A rebalance that filled only a name or two must NOT count as done — the catch-up keeps going
    until the book covers most of the target, so the unfilled names aren't stranded for a month."""
    from datetime import datetime
    from sqlalchemy import insert
    eng = _engine()
    est = run_eod.rebalance_established_this_month
    five = {f"NM{i}": 0.05 for i in range(5)}            # 5-name target → need ceil(0.8×5) = 4
    with eng.begin() as c:
        c.execute(insert(db.rebalance_log).values(
            ts=datetime(2026, 7, 1, 18, 0), trigger_reason="monthly_catchup",
            target_weights=five, risk_gate_passed=True, risk_gate_reason="ok"))
        c.execute(insert(db.snapshots).values(                # only 2 of 5 names filled
            ts=datetime(2026, 7, 1, 18, 1), nav=100_000.0, cash=80_000.0, last_equity=100_000.0,
            weights=five, positions={"NM0": 100, "NM1": 100}, drift=None))
    assert est(eng, date(2026, 7, 2)) is False           # 2/5 < 4 → keep catching up
    with eng.begin() as c:                                # now 4 of 5 are held
        c.execute(insert(db.snapshots).values(
            ts=datetime(2026, 7, 1, 18, 2), nav=100_000.0, cash=20_000.0, last_equity=100_000.0,
            weights=five, positions={f"NM{i}": 100 for i in range(4)}, drift=None))
    assert est(eng, date(2026, 7, 2)) is True            # 4/5 ≥ 4 → established


def test_daily_job_skips_non_trading_day():
    eng = _engine()
    broker = _FakeBroker()
    ingested = []
    res = run_eod.daily_job(
        client=_FakeClient(), broker=broker, db_engine=eng, settings=load_settings(),
        as_of=date(2026, 7, 4), ingest_fn=ingested.append,
        trading_day_fn=lambda c, d: False)
    assert res.status == "not_trading_day"
    assert ingested == [] and broker.submitted == []                # nothing ran


def test_graceful_shutdown_finishes_stage_then_cancels():
    calls = []

    class _Sched:
        def shutdown(self, wait=False):
            calls.append(("shutdown", wait))

    class _Brk:
        def cancel_all_orders(self):
            calls.append(("cancel_all",))
            return 3

    run_eod.graceful_shutdown(_Sched(), _Brk())
    assert calls == [("shutdown", True), ("cancel_all",)]           # finish stage, then cancel
