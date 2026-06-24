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
        # two snapshots (for day P&L) — newest last; last_equity = prior-day close (P&L basis)
        c.execute(insert(db.snapshots).values(
            ts=datetime(2026, 6, 30, 16), nav=200_000.0, cash=10_000.0, last_equity=198_000.0,
            weights={"AAPL": 0.05}, positions={"AAPL": 100}, drift=0.0))
        c.execute(insert(db.snapshots).values(
            ts=datetime(2026, 7, 1, 16), nav=202_000.0, cash=9_000.0, last_equity=200_000.0,
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
    assert s["day_pnl"] == 2_000.0                       # 202k − last_equity 200k (NOT prev snapshot)
    assert s["risk_gate_passed"] is True
    assert s["premium_collected"] == 400.0 + 150.0 - 120.0
    by_sym = {r["symbol"]: r for r in s["positions"]}
    assert by_sym["MSFT"]["weight"] == 0.04 and by_sym["MSFT"]["target_weight"] == 0.05


def test_api_state_leverage_gross_and_market_value():
    eng = _engine()
    _seed(eng)
    s = data.api_state(eng)
    # leverage = Σ weights = gross / equity; gross = nav × leverage
    assert abs(s["leverage"] - 0.09) < 1e-9          # 0.05 + 0.04
    assert abs(s["gross_exposure"] - 202_000.0 * 0.09) < 1e-6
    assert s["n_positions"] == 2
    assert abs(s["day_pnl_pct"] - 0.01) < 1e-9        # 2000 / 200000
    by_sym = {r["symbol"]: r for r in s["positions"]}
    assert abs(by_sym["AAPL"]["market_value"] - 0.05 * 202_000.0) < 1e-6


def test_day_pnl_uses_last_equity_not_prior_snapshot():
    # last_equity (prior-day close) is the P&L basis, NOT the previous 60s snapshot's NAV.
    eng = _engine()
    with eng.begin() as c:
        c.execute(insert(db.snapshots).values(
            ts=datetime(2026, 7, 1, 15, 59), nav=205_000.0, cash=9_000.0, last_equity=200_000.0,
            weights={"AAPL": 0.05}, positions={"AAPL": 100}, drift=0.0))
        c.execute(insert(db.snapshots).values(   # 60s later — only NAV moved a touch
            ts=datetime(2026, 7, 1, 16, 0), nav=205_100.0, cash=9_000.0, last_equity=200_000.0,
            weights={"AAPL": 0.05}, positions={"AAPL": 100}, drift=0.0))
    s = data.api_state(eng)
    assert s["day_pnl"] == 5_100.0                       # 205.1k − last_equity 200k, not − 205k
    assert abs(s["day_pnl_pct"] - 5_100.0 / 200_000.0) < 1e-12


def test_leverage_and_count_exclude_written_options():
    # A short call shares the snapshot but must not deflate the equity-leverage gauge.
    eng = _engine()
    with eng.begin() as c:
        c.execute(insert(db.snapshots).values(
            ts=datetime(2026, 7, 1, 16), nav=200_000.0, cash=9_000.0, last_equity=200_000.0,
            weights={"AAPL": 0.05, "MSFT": 0.04, "AAPL260821C00215000": -0.001},
            positions={"AAPL": 100, "MSFT": 40, "AAPL260821C00215000": -1}, drift=0.0))
    s = data.api_state(eng)
    assert abs(s["leverage"] - 0.09) < 1e-9              # equity only; the -0.001 call is excluded
    assert s["n_positions"] == 2                          # AAPL + MSFT, not the call


def test_api_nav_history_oldest_first():
    eng = _engine()
    _seed(eng)
    hist = data.api_nav_history(eng)
    assert [h["nav"] for h in hist] == [200_000.0, 202_000.0]   # ascending by ts
    assert hist[-1]["cash"] == 9_000.0


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


def test_series_stats_pure():
    s = data.series_stats([100, 110])
    assert abs(s["total_return"] - 0.10) < 1e-12 and s["max_drawdown"] == 0.0
    s2 = data.series_stats([100, 90, 99])
    assert abs(s2["total_return"] - (-0.01)) < 1e-12 and abs(s2["max_drawdown"] - (-0.10)) < 1e-12
    assert data.series_stats([100])["total_return"] is None        # <2 points → insufficient


def test_api_track_record_curve_and_premium():
    eng = _engine()
    with eng.begin() as c:
        for i, nv in enumerate([100_000.0, 100_500.0, 99_800.0, 101_200.0]):
            c.execute(insert(db.snapshots).values(
                ts=datetime(2026, 6, 1 + i, 16), nav=nv, cash=5_000.0, last_equity=100_000.0,
                weights={}, positions={}, drift=0.0))
        c.execute(insert(db.options_lifecycle).values(
            ts=datetime(2026, 6, 1), event_type="write", underlying="AAPL",
            option_symbol="AAPLX", strike=100.0, contracts=1, premium=300.0))
    tr = data.api_track_record(eng)
    assert tr["available"] and tr["days"] == 4 and tr["mature"] is False     # <10 days
    assert tr["nav0"] == 100_000.0 and tr["nav_now"] == 101_200.0
    assert abs(tr["total_return"] - 0.012) < 1e-9 and tr["premium_collected"] == 300.0
    assert tr["norm"][0] == 1.0 and abs(tr["max_drawdown"] - (99_800.0 / 100_500.0 - 1)) < 1e-9


def test_api_track_record_empty():
    tr = data.api_track_record(_engine())
    assert tr["available"] is False and tr["dates"] == [] and tr["premium_collected"] == 0.0


def test_api_slippage_signs_and_aggregate():
    eng = _engine()
    with eng.begin() as c:
        # buy filled BELOW limit → favorable (negative bps); sell filled ABOVE limit → favorable
        c.execute(insert(db.orders).values(id="b1", symbol="AAPL", side="buy", qty=100,
                  order_type="limit", status="filled", limit_price=100.0, filled_qty=100,
                  filled_avg_price=99.90))
        c.execute(insert(db.orders).values(id="s1", symbol="MSFT", side="sell", qty=10,
                  order_type="limit", status="filled", limit_price=200.0, filled_qty=10,
                  filled_avg_price=200.40))
        c.execute(insert(db.orders).values(id="m1", symbol="KO", side="buy", qty=5,
                  order_type="market", status="filled", limit_price=None, filled_qty=5,
                  filled_avg_price=60.0))                                    # market → excluded
    sl = data.api_slippage(eng)
    assert sl["n_fills"] == 2                                                # market order excluded
    by = {f["symbol"]: f for f in sl["fills"]}
    assert by["AAPL"]["slippage_bps"] == -10.0 and by["AAPL"]["slippage_usd"] == -10.0  # favorable
    assert by["MSFT"]["slippage_bps"] == -20.0                              # sell above limit = good
    assert sl["total_slippage_usd"] == -14.0                                # -10 + (-4)


def test_empty_db_is_safe():
    eng = _engine()
    s = data.api_state(eng)
    assert s["nav"] is None and s["positions"] == [] and s["premium_collected"] == 0.0
    assert s["leverage"] is None and s["gross_exposure"] is None and s["n_positions"] == 0
    assert data.api_orders(eng) == [] and data.api_calls(eng) == [] and data.api_factors(eng) == []
    assert data.api_nav_history(eng) == []
