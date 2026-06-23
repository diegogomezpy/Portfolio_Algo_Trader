"""Unit tests for dashboard.data — Postgres-only read functions, on in-memory sqlite."""

from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import create_engine, insert

from dashboard import data
from engine import db


def _engine():
    eng = create_engine("sqlite://")
    db.create_all(eng)
    return eng


def _seed(eng):
    with eng.begin() as c:
        # two snapshots (for day P&L) — newest last
        c.execute(insert(db.snapshots).values(
            ts=datetime(2026, 6, 30, 16), nav=200_000.0, cash=10_000.0,
            weights={"AAPL": 0.05}, positions={"AAPL": 100}, drift=0.0))
        c.execute(insert(db.snapshots).values(
            ts=datetime(2026, 7, 1, 16), nav=202_000.0, cash=9_000.0,
            weights={"AAPL": 0.05, "MSFT": 0.04}, positions={"AAPL": 100, "MSFT": 40}, drift=0.03))
        c.execute(insert(db.rebalance_log).values(
            ts=datetime(2026, 7, 1, 16), trigger_reason="monthly",
            target_weights={"AAPL": 0.05, "MSFT": 0.05}, risk_gate_passed=True,
            risk_gate_reason="ok"))
        c.execute(insert(db.orders).values(
            id="o1", symbol="MSFT", side="buy", qty=40, order_type="limit", status="filled",
            filled_qty=40, filled_avg_price=200.0, submitted_at=datetime(2026, 7, 1, 16)))
        # an open call (write 2) and a fully-closed one (write 1, close 1)
        c.execute(insert(db.options_lifecycle).values(
            ts=datetime(2026, 7, 1), event_type="write", underlying="AAPL",
            option_symbol="AAPL260821C00215000", strike=215.0, expiration=date(2026, 8, 21),
            delta=0.30, contracts=2, premium=400.0))
        c.execute(insert(db.options_lifecycle).values(
            ts=datetime(2026, 7, 1), event_type="write", underlying="XYZ",
            option_symbol="XYZ260821C00100000", strike=100.0, expiration=date(2026, 8, 21),
            delta=0.30, contracts=1, premium=150.0))
        c.execute(insert(db.options_lifecycle).values(
            ts=datetime(2026, 7, 2), event_type="close", underlying="XYZ",
            option_symbol="XYZ260821C00100000", strike=100.0, expiration=date(2026, 8, 21),
            delta=0.30, contracts=1, premium=-120.0))
        c.execute(insert(db.factor_scores).values(
            date=date(2026, 7, 1), symbol="AAPL", composite_score=1.2, quality_score=0.5,
            value_score=0.3, momentum_score=0.9, lowvol_score=0.1))
        c.execute(insert(db.factor_scores).values(
            date=date(2026, 7, 1), symbol="OTHER", composite_score=0.4))   # not held
        c.execute(insert(db.alerts).values(
            ts=datetime(2026, 7, 1, 16), alert_type="rebalance_completed",
            message="rebalance 2026-07-01 complete", delivered=False))


def test_api_state_merges_snapshot_target_and_pnl():
    eng = _engine()
    _seed(eng)
    s = data.api_state(eng)
    assert s["nav"] == 202_000.0 and s["cash"] == 9_000.0 and s["drift"] == 0.03
    assert s["day_pnl"] == 2_000.0                       # 202k − 200k
    assert s["risk_gate_passed"] is True
    assert s["premium_collected"] == 400.0 + 150.0 - 120.0
    by_sym = {r["symbol"]: r for r in s["positions"]}
    assert by_sym["MSFT"]["weight"] == 0.04 and by_sym["MSFT"]["target_weight"] == 0.05


def test_api_orders_returns_recent():
    eng = _engine()
    _seed(eng)
    orders = data.api_orders(eng)
    assert len(orders) == 1 and orders[0]["symbol"] == "MSFT" and orders[0]["status"] == "filled"


def test_api_calls_only_open_positions():
    eng = _engine()
    _seed(eng)
    calls = data.api_calls(eng)
    syms = {c["underlying"] for c in calls}
    assert syms == {"AAPL"}                              # XYZ written then closed → not open
    aapl = next(c for c in calls if c["underlying"] == "AAPL")
    assert aapl["contracts"] == 2 and aapl["strike"] == 215.0


def test_api_factors_only_held_names():
    eng = _engine()
    _seed(eng)
    facs = data.api_factors(eng)
    syms = {f["symbol"] for f in facs}
    assert "AAPL" in syms and "OTHER" not in syms       # OTHER isn't a held position


def test_api_alerts_recent():
    eng = _engine()
    _seed(eng)
    al = data.api_alerts(eng)
    assert al[0]["type"] == "rebalance_completed" and al[0]["delivered"] is False


def test_empty_db_is_safe():
    eng = _engine()
    s = data.api_state(eng)
    assert s["nav"] is None and s["positions"] == [] and s["premium_collected"] == 0.0
    assert data.api_orders(eng) == [] and data.api_calls(eng) == [] and data.api_factors(eng) == []
