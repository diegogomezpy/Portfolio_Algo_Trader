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
    def __init__(self, positions=None, equity=100_000.0, cash=5_000.0, raise_positions=False):
        self._positions = positions or {}        # {symbol: (qty, market_value)}
        self._equity, self._cash = equity, cash
        self._raise = raise_positions

    def account(self):
        return {"equity": self._equity, "cash": self._cash}

    def all_positions(self):
        if self._raise:
            raise AlpacaAPIError("*", "all_positions", "503 unreachable")
        return [{"symbol": s, "qty": q, "market_value": mv}
                for s, (q, mv) in self._positions.items()]


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
    assert {o["qty"] for o in orders} == {50.0, 20.0}        # 5000/100, 4000/200
    assert len(_rows(eng, db.fills)) == 2
    rlog = _rows(eng, db.rebalance_log)
    assert len(rlog) == 1 and rlog[0]["risk_gate_passed"] is True
    assert len(_rows(eng, db.snapshots)) == 1               # monitor wrote one


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
