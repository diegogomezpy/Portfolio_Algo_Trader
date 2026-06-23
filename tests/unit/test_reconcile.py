"""Unit tests for engine.reconcile — diff vs DB, correct to Alpaca, block if down.

Fake Alpaca client + in-memory sqlite. Guards the pure diff, the no-divergence path, the
divergence → corrective-snapshot + alert path, the first-run (no prior snapshot) case,
and the block-on-unreachable contract.
"""

from __future__ import annotations

from datetime import datetime

import pytest
from sqlalchemy import create_engine, desc, insert, select

from engine import db, reconcile
from engine.alpaca_client import AlpacaAPIError


class _FakeClient:
    def __init__(self, positions, account=None, raise_positions=False):
        self._positions = positions                  # {symbol: qty}
        self._account = account or {"equity": 100_000.0, "cash": 5_000.0}
        self._raise = raise_positions

    def all_positions(self):
        if self._raise:
            raise AlpacaAPIError("*", "all_positions", "503 unreachable")
        return [{"symbol": s, "qty": q} for s, q in self._positions.items()]

    def account(self):
        return self._account


def _engine():
    eng = create_engine("sqlite://")
    db.create_all(eng)
    return eng


def _seed_snapshot(eng, positions, ts=datetime(2026, 6, 1, 16, 0)):
    with eng.begin() as c:
        c.execute(insert(db.snapshots).values(ts=ts, nav=100_000.0, cash=5_000.0,
                                              positions=positions, weights={}, drift=None))


def _snapshots(eng):
    with eng.connect() as c:
        return [dict(r) for r in c.execute(select(db.snapshots).order_by(db.snapshots.c.ts)).mappings()]


# ------------------------------------------------------------ pure diff ----
def test_diff_positions_flags_only_beyond_threshold():
    live = {"A": 100, "B": 50}
    dbp = {"A": 100, "B": 40, "C": 10}
    d0 = reconcile.diff_positions(live, dbp, threshold=0.0)
    assert {x["symbol"] for x in d0} == {"B", "C"}        # A matches; B off 10, C missing
    assert reconcile.diff_positions(live, dbp, threshold=15) == []   # all within 15


def test_fetch_live_positions_normalizes():
    live = reconcile.fetch_live_positions(_FakeClient({"AAPL": 100, "MSFT": 25}))
    assert live == {"AAPL": 100.0, "MSFT": 25.0}


# --------------------------------------------------------- reconcile() ----
def test_no_divergence_writes_no_snapshot():
    eng = _engine()
    _seed_snapshot(eng, {"AAPL": 100})
    res = reconcile.reconcile(_FakeClient({"AAPL": 100}), eng)
    assert res.corrected is False and res.divergences == []
    assert len(_snapshots(eng)) == 1                     # nothing new written


def test_divergence_corrects_to_alpaca_and_alerts():
    eng = _engine()
    _seed_snapshot(eng, {"AAPL": 100})                   # DB thinks 100…
    alerts = []
    res = reconcile.reconcile(_FakeClient({"AAPL": 120}), eng, alert=alerts.append)  # …Alpaca says 120
    assert res.corrected is True
    assert res.divergences[0]["symbol"] == "AAPL" and res.divergences[0]["delta"] == 20.0
    assert len(alerts) == 1
    snaps = _snapshots(eng)
    assert len(snaps) == 2                               # a corrective snapshot was written
    assert snaps[-1]["positions"] == {"AAPL": 120.0}     # …matching Alpaca
    assert snaps[-1]["nav"] == 100_000.0                 # nav/cash pulled from account()


def test_first_run_with_no_prior_snapshot_corrects():
    eng = _engine()                                      # empty — no snapshots
    res = reconcile.reconcile(_FakeClient({"AAPL": 50}), eng)
    assert res.corrected is True
    assert _snapshots(eng)[-1]["positions"] == {"AAPL": 50.0}


def test_blocks_when_alpaca_unreachable():
    eng = _engine()
    alerts = []
    with pytest.raises(AlpacaAPIError):
        reconcile.reconcile(_FakeClient({}, raise_positions=True), eng, alert=alerts.append)
    assert len(alerts) == 1                              # alerted before blocking


def test_no_db_engine_is_fetch_and_report():
    res = reconcile.reconcile(_FakeClient({"AAPL": 10}), None)
    assert res.live_positions == {"AAPL": 10.0} and res.corrected is False
