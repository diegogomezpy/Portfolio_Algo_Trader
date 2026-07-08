"""Unit tests for engine.monitor — NAV/weights/drift + snapshot writes.

Pure math (compute_weights, l1_drift) plus the monitor_once I/O on a fake client +
in-memory sqlite: snapshot persistence, drift flagging, and the no-target path.
"""

from __future__ import annotations

import pandas as pd
import pytest
from sqlalchemy import create_engine, insert, select

from engine import db, monitor


class _FakeClient:
    def __init__(self, positions, equity=100_000.0, cash=5_000.0):
        # positions: {symbol: (qty, market_value)}
        self._positions = positions
        self._equity = equity
        self._cash = cash

    def account(self):
        return {"equity": self._equity, "cash": self._cash}

    def all_positions(self):
        return [{"symbol": s, "qty": q, "market_value": mv}
                for s, (q, mv) in self._positions.items()]


def _engine():
    eng = create_engine("sqlite://")
    db.create_all(eng)
    return eng


def _snapshots(eng):
    with eng.connect() as c:
        return [dict(r) for r in c.execute(select(db.snapshots)).mappings()]


# ------------------------------------------------------------ pure math ----
def test_compute_weights_from_market_values():
    positions = [{"symbol": "AAPL", "market_value": 50_000.0},
                 {"symbol": "MSFT", "market_value": 25_000.0}]
    w = monitor.compute_weights(positions, nav=100_000.0)
    assert w["AAPL"] == pytest.approx(0.5) and w["MSFT"] == pytest.approx(0.25)


def test_compute_weights_guards_zero_nav():
    assert monitor.compute_weights([{"symbol": "AAPL", "market_value": 1.0}], nav=0).empty


def test_l1_drift_over_union_of_names():
    cur = {"A": 0.5, "B": 0.5}
    tgt = {"A": 0.4, "C": 0.5}
    # |0.5-0.4| + |0.5-0| + |0-0.5| = 0.1 + 0.5 + 0.5 = 1.1
    assert monitor.l1_drift(cur, tgt) == pytest.approx(1.1)


# --------------------------------------------------------- monitor_once ----
def test_monitor_once_writes_snapshot():
    eng = _engine()
    client = _FakeClient({"AAPL": (100, 50_000.0), "MSFT": (50, 25_000.0)})
    res = monitor.monitor_once(client, eng)
    assert res.nav == 100_000.0 and res.cash == 5_000.0
    snaps = _snapshots(eng)
    assert len(snaps) == 1
    assert snaps[0]["positions"] == {"AAPL": 100.0, "MSFT": 50.0}
    assert snaps[0]["weights"]["AAPL"] == pytest.approx(0.5)
    assert snaps[0]["drift"] is None                     # no target supplied
    assert snaps[0]["account"] == "primary"              # tracked-sleeves: default to the primary book


def test_monitor_once_tags_the_account():
    # A per-account monitor pass tags its snapshot with that account (tracked sleeves).
    eng = _engine()
    client = _FakeClient({"AAPL": (100, 50_000.0)})
    monitor.monitor_once(client, eng, account="trend")
    assert _snapshots(eng)[0]["account"] == "trend"


def test_monitor_once_computes_drift_as_telemetry():
    # Drift is computed against the target and stored, but it triggers nothing (D31).
    eng = _engine()
    client = _FakeClient({"AAPL": (100, 80_000.0)})      # 0.8 weight vs 0.5 target → drift 0.3
    res = monitor.monitor_once(client, eng, target_weights={"AAPL": 0.5})
    assert res.drift == pytest.approx(0.3)
    assert _snapshots(eng)[0]["drift"] == pytest.approx(0.3)
    assert not hasattr(res, "drift_breach")              # no breach/trigger concept anymore


def test_last_target_weights_reads_latest():
    eng = _engine()
    from datetime import datetime
    with eng.begin() as c:
        c.execute(insert(db.rebalance_log).values(
            ts=datetime(2026, 6, 1), trigger_reason="monthly",
            target_weights={"AAPL": 0.2}, risk_gate_passed=True))
        c.execute(insert(db.rebalance_log).values(
            ts=datetime(2026, 7, 1), trigger_reason="monthly",
            target_weights={"AAPL": 0.05, "MSFT": 0.05}, risk_gate_passed=True))
    assert monitor.last_target_weights(eng) == {"AAPL": 0.05, "MSFT": 0.05}


def test_last_target_weights_none_when_empty():
    assert monitor.last_target_weights(_engine()) is None
