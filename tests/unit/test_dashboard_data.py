"""Unit tests for dashboard.data — Postgres-only read functions, on in-memory sqlite."""

from __future__ import annotations

from datetime import date, datetime, timezone

from sqlalchemy import create_engine, insert

from dashboard import data
from engine import db

UTC = timezone.utc


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
            weights={"AAPL": 0.05, "MSFT": 0.04, "AAPL260821C00215000": -0.002},
            positions={"AAPL": 100, "MSFT": 40, "AAPL260821C00215000": -2}, drift=0.03))
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
    # The book is sourced from the live snapshot's short-call positions, enriched from the
    # write log. AAPL has a -2 position → shows; XYZ was written+closed and carries no
    # position → must not show.
    eng = _engine()
    _seed(eng)
    calls = data.api_calls(eng)
    syms = {c["underlying"] for c in calls}
    assert syms == {"AAPL"}
    aapl = next(c for c in calls if c["underlying"] == "AAPL")
    assert aapl["contracts"] == 2 and aapl["strike"] == 215.0
    assert aapl["delta"] == 0.30 and aapl["premium"] == 400.0   # enriched from the write log


def test_api_calls_ignores_unfilled_write_with_no_position():
    # The exact discrepancy bug: a write is logged but the order never filled (no position in
    # the snapshot). The dashboard must NOT show it — it reads Alpaca truth, not the write log.
    eng = _engine()
    with eng.begin() as c:
        c.execute(insert(db.snapshots).values(
            ts=datetime(2026, 7, 1, 16), nav=200_000.0, cash=9_000.0, last_equity=200_000.0,
            weights={"AAPL": 0.05}, positions={"AAPL": 100}, drift=0.0))   # no short call held
        c.execute(insert(db.options_lifecycle).values(   # phantom write, never filled/positioned
            ts=datetime(2026, 7, 1), event_type="write", underlying="SLV",
            option_symbol="SLV260731C00057500", strike=57.5, expiration=date(2026, 7, 31),
            delta=0.31, contracts=1, premium=120.0))
    assert data.api_calls(eng) == []


def test_api_calls_renders_position_without_metadata_from_occ():
    # A short call held with no write row (e.g. pre-existing) still renders, parsed from OCC.
    eng = _engine()
    with eng.begin() as c:
        c.execute(insert(db.snapshots).values(
            ts=datetime(2026, 7, 1, 16), nav=200_000.0, cash=9_000.0, last_equity=200_000.0,
            weights={"IAU": 0.04, "IAU260807C00079000": -0.001},
            positions={"IAU": 200, "IAU260807C00079000": -2}, drift=0.0))
    calls = data.api_calls(eng)
    assert len(calls) == 1
    c0 = calls[0]
    assert c0["underlying"] == "IAU" and c0["contracts"] == 2
    assert c0["strike"] == 79.0 and c0["expiration"] == "2026-08-07"
    assert c0["delta"] is None and c0["premium"] is None   # no write row → metadata absent


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
    assert al[0]["severity"] == "info"          # "rebalance ... complete" → info, not a red error


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


def test_api_track_record_monthly_returns_chain_across_months():
    eng = _engine()
    with eng.begin() as c:
        rows = [(datetime(2026, 6, 1, 16), 100_000.0), (datetime(2026, 6, 2, 16), 101_000.0),
                (datetime(2026, 6, 3, 16), 102_000.0),                       # June: 3 trading days
                (datetime(2026, 7, 1, 16), 103_000.0), (datetime(2026, 7, 2, 16), 104_040.0)]  # July: 2
        for ts, nv in rows:
            c.execute(insert(db.snapshots).values(
                ts=ts, nav=nv, cash=0.0, last_equity=100_000.0, weights={}, positions={}, drift=0.0))
    m = data.api_track_record(eng)["monthly"]
    assert len(m) == 2
    jun, jul = m
    # June is the first (partial) month → based off its own first NAV: 102000/100000 − 1 = 2%
    assert jun["year"] == 2026 and jun["month"] == 6 and jun["days"] == 3
    assert abs(jun["ret"] - 0.02) < 1e-9
    # July chains off June's last close (102000): 104040/102000 − 1 = 2%
    assert jul["month"] == 7 and jul["days"] == 2 and abs(jul["ret"] - 0.02) < 1e-9


def test_api_risk_exposes_daily_returns_for_distribution():
    eng = _engine()
    _seed_curve(eng, [100, 110, 105, 120, 90, 100])
    r = data.api_risk(eng)
    assert len(r["returns"]) == r["days"] - 1                      # one return per day-over-day step
    assert abs(r["returns"][0] - 0.10) < 1e-9                      # 100 → 110


def test_api_risk_contributions_reads_latest_rebalance():
    eng = _engine()
    with eng.begin() as c:
        # older row (should be ignored) then the newest with a fuller decomposition
        c.execute(insert(db.rebalance_log).values(
            ts=datetime(2026, 6, 1, 16), trigger_reason="monthly", target_weights={},
            risk_gate_passed=True, risk_gate_reason="ok",
            risk_contributions={"portfolio_vol": 0.11, "contrib": {"AAPL": 0.5},
                                "weight": {"AAPL": 0.5}}))
        c.execute(insert(db.rebalance_log).values(
            ts=datetime(2026, 7, 1, 16), trigger_reason="monthly", target_weights={},
            risk_gate_passed=True, risk_gate_reason="ok",
            risk_contributions={"portfolio_vol": 0.18,
                                "contrib": {"AAPL": 0.3, "MSFT": 0.7},
                                "weight": {"AAPL": 0.5, "MSFT": 0.5}}))
    rc = data.api_risk_contributions(eng)
    assert rc["available"] and rc["portfolio_vol"] == 0.18
    assert [n["symbol"] for n in rc["names"]] == ["MSFT", "AAPL"]     # sorted by risk share desc
    assert rc["names"][0]["rc_pct"] == 0.7 and rc["names"][0]["weight"] == 0.5


def test_api_risk_contributions_empty_when_no_rebalance_has_it():
    eng = _engine()
    with eng.begin() as c:                                            # a rebalance row without the field
        c.execute(insert(db.rebalance_log).values(
            ts=datetime(2026, 7, 1, 16), trigger_reason="monthly", target_weights={},
            risk_gate_passed=True, risk_gate_reason="ok"))
    rc = data.api_risk_contributions(eng)
    assert rc["available"] is False and rc["names"] == []


def _seed_curve(eng, navs):
    with eng.begin() as c:
        for i, nv in enumerate(navs):
            c.execute(insert(db.snapshots).values(
                ts=datetime(2026, 6, 1 + i, 16), nav=float(nv), cash=0.0, last_equity=float(navs[0]),
                weights={}, positions={}, drift=0.0))


def test_api_risk_drawdown_and_var():
    eng = _engine()
    _seed_curve(eng, [100, 110, 105, 120, 90, 100])     # peak 120, trough 90, recovering to 100
    r = data.api_risk(eng)
    assert r["available"] and r["days"] == 6 and r["mature"] is False
    assert abs(r["max_drawdown"] - (-0.25)) < 1e-9 and r["max_drawdown_date"] == r["dates"][4]
    assert abs(r["current_drawdown"] - (100 / 120 - 1)) < 1e-9       # still below the 120 high-water mark
    assert r["days_in_drawdown"] == 2 and r["peak_nav"] == 120
    assert len(r["drawdown"]) == 6 and r["drawdown"][0] == 0.0
    # volatility / VaR are populated even pre-maturity; parametric VaR scales by today's NAV
    assert r["ann_vol"] > 0 and r["daily_vol"] > 0 and r["var95_1d_pct"] > 0
    assert abs(r["var95_1d_usd"] - r["var95_1d_pct"] * r["nav_now"]) < 1e-6
    assert r["hist_var95_1d_pct"] >= 0 and r["cvar95_1d_pct"] >= r["hist_var95_1d_pct"] - 1e-9


def test_api_risk_rolling_vol_alignment_and_mature():
    eng = _engine()
    _seed_curve(eng, [100 + i + (i % 3) for i in range(12)])   # 12 days → mature; some variation
    r = data.api_risk(eng)
    assert r["mature"] is True and r["rolling_window"] == 10
    assert len(r["rolling_vol"]) == r["days"] and r["rolling_vol"][0] is None          # aligned to dates
    assert r["rolling_vol"][1] is None
    vals = [v for v in r["rolling_vol"] if v is not None]
    assert vals and all(v >= 0 for v in vals)                                          # vol is non-negative


def test_api_risk_empty_is_safe():
    r = data.api_risk(_engine())
    assert r["available"] is False and r["days"] == 0


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


def test_fees_from_activities_aggregates_by_type():
    """FEE activities aggregate to a positive total + by-type breakdown; non-fees are ignored."""
    activities = [
        {"activity_type": "FILL", "side": "buy", "symbol": "WU", "price": "7.19"},   # ignored
        {"activity_type": "FEE", "activity_sub_type": "CAT", "net_amount": "-0.01",
         "date": "2026-06-24", "description": "CAT fee for proceed of 2 trades"},
        {"activity_type": "FEE", "activity_sub_type": "TAF", "net_amount": "-0.01",
         "date": "2026-06-24", "description": "TAF fee for proceed of 1 shares"},
        {"activity_type": "FEE", "activity_sub_type": "TAF", "net_amount": "-0.02",
         "date": "2026-06-25", "description": "TAF fee"},
        {"activity_type": "FEE", "activity_sub_type": "REG", "net_amount": "0",          # zero → skip
         "date": "2026-06-25", "description": "no-op"},
    ]
    f = data.fees_from_activities(activities)
    assert f["total_usd"] == 0.04                         # 0.01 + 0.01 + 0.02
    assert f["by_type"] == {"TAF": 0.03, "CAT": 0.01}     # sorted by magnitude desc
    assert f["n"] == 3
    assert f["items"][0]["date"] == "2026-06-25"          # newest first
    assert all(it["amount"] > 0 for it in f["items"])     # magnitudes, not negatives


def test_api_fees_empty_without_broker():
    assert data.api_fees(_engine()) == {"total_usd": 0.0, "by_type": {}, "n": 0, "items": []}


def test_slippage_from_orders_market_uses_arrival_mid():
    """Live path: market orders are priced vs the arrival mid; a missing arrival drops the order."""
    arrivals = {("WU", "t-buy"): 7.20, ("WU", "t-sell"): 7.20}   # NBBO mid at submit
    arrival_mid = lambda sym, ts: arrivals.get((sym, ts))
    orders = [
        # market BUY filled ABOVE the arrival mid → adverse (paid up): (7.21-7.20)/7.20*1e4 ≈ +13.9 bps
        {"symbol": "WU", "side": "buy", "type": "market", "filled_qty": 100,
         "limit_price": None, "filled_avg_price": 7.21, "submitted_at": "t-buy"},
        # market SELL filled BELOW the arrival mid → adverse (got less): (7.20-7.18)/7.20*1e4 ≈ +27.8 bps
        {"symbol": "WU", "side": "sell", "type": "market", "filled_qty": 100,
         "limit_price": None, "filled_avg_price": 7.18, "submitted_at": "t-sell"},
        # limit order → priced vs its limit, no arrival lookup needed
        {"symbol": "AAPL", "side": "buy", "type": "limit", "filled_qty": 10,
         "limit_price": 100.0, "filled_avg_price": 99.90, "submitted_at": "t-x"},
        # market order whose arrival mid can't be resolved → skipped (no reference)
        {"symbol": "XYZ", "side": "buy", "type": "market", "filled_qty": 5,
         "limit_price": None, "filled_avg_price": 50.0, "submitted_at": "t-missing"},
    ]
    sl = data.slippage_from_orders(orders, arrival_mid)
    assert sl["n_fills"] == 3                                                # XYZ (no arrival) dropped
    by = {f["symbol"]: f for f in sl["fills"]}
    assert by["WU"]["basis"] == "arrival" and by["WU"]["type"] == "market"
    assert by["WU"]["slippage_bps"] > 0                                      # paid up on the buy = adverse
    assert by["AAPL"]["basis"] == "limit" and by["AAPL"]["slippage_bps"] == -10.0


def test_arrival_reference_spread_guard():
    # Tight two-sided quote → mid.
    assert data.arrival_reference(99.95, 100.05, 100.10) == 100.0
    # Wide/stale quote (INBX-like: bid 94.73 / ask 108.87, ~14% spread) → trust the trade print.
    assert data.arrival_reference(94.73, 108.87, 95.1) == 95.1
    # One-sided quote → trade.
    assert data.arrival_reference(None, 108.87, 95.1) == 95.1
    # No quote at all → trade; nothing usable → None.
    assert data.arrival_reference(0, 0, 95.1) == 95.1
    assert data.arrival_reference(None, None, None) is None


def test_slippage_prefers_arrival_over_padded_limit():
    # The INBX regression: a marketable-limit BUY whose limit (108.87) was crossed to a stale ask,
    # filled at 95. Measured vs its own limit it's a bogus −$1,470 "gain"; vs the ~95 arrival it's ~0.
    arrival = lambda sym, ts: 95.0 if sym == "INBX" else None
    orders = [{"symbol": "INBX", "side": "buy", "type": "limit", "filled_qty": 106,
               "limit_price": 108.87, "filled_avg_price": 95.0, "submitted_at": "t"}]
    row = data.slippage_from_orders(orders, arrival)["fills"][0]
    assert row["basis"] == "arrival" and row["intended"] == 95.0
    assert abs(row["slippage_usd"]) < 1.0 and abs(row["slippage_bps"]) < 5   # ~0, not −1470 / −1274bps


def test_held_symbols_union_excludes_options():
    eng = _engine()
    _seed(eng)
    syms = set(data.held_symbols(eng))
    # positions (AAPL, MSFT) ∪ targets (AAPL, MSFT) ∪ today's factors (AAPL, OTHER) ∪ call underlyings (AAPL, XYZ)
    assert syms == {"AAPL", "MSFT", "OTHER", "XYZ"}
    assert all(not data._is_option(s) for s in syms)


def test_next_rebalance_estimate_weekend_and_rollover():
    # mid-month → first weekday of the coming month; Aug 1 2026 is a Saturday → rolls to Mon Aug 3
    assert data._next_rebalance_estimate(date(2026, 6, 24)) == date(2026, 7, 1)
    assert data._first_weekday(2026, 8) == date(2026, 8, 3)
    assert data._next_rebalance_estimate(date(2026, 7, 1)) == date(2026, 8, 3)   # on rebalance day → next
    assert data._next_rebalance_estimate(date(2026, 12, 15)) == date(2027, 1, 1)  # year rollover


def test_api_health_live_schedule_and_drift():
    eng = _engine()
    _seed(eng)
    # 'now' 30s after the latest snapshot (2026-07-01 16:00 UTC) → engine heartbeat is live
    h = data.api_health(eng, now=datetime(2026, 7, 1, 16, 0, 30, tzinfo=UTC))
    assert h["engine"]["status"] == "live" and h["engine"]["age_s"] == 30
    assert h["last_rebalance"]["gate_passed"] is True and h["last_rebalance"]["trigger"] == "monthly"
    # next rebalance estimate = first weekday of August (07-01 is past July's first trading day)
    assert h["next_rebalance"] == {"date": "2026-08-03", "source": "estimated",
                                   "days_until": (date(2026, 8, 3) - date(2026, 7, 1)).days}
    # drift: MSFT 0.04 vs target 0.05 → one name off-target by >0.5%, L1 from the snapshot
    assert h["drift"]["l1"] == 0.03 and h["drift"]["n_drifting"] == 1
    assert h["drift"]["max_name"] == "MSFT" and abs(h["drift"]["max_dev"] + 0.01) < 1e-9
    assert h["alerts_24h"]["total"] == 1 and h["alerts_24h"]["worst"] == "ok"
    assert h["freshness"]["factors_date"] == "2026-07-01"
    assert "market" not in h          # market hours are layered on by the route, not here


def test_api_health_heartbeat_thresholds():
    eng = _engine()
    _seed(eng)                                                    # latest snapshot ts = 2026-07-01 16:00
    assert data.api_health(eng, now=datetime(2026, 7, 1, 16, 5, tzinfo=UTC))["engine"]["status"] == "stale"
    assert data.api_health(eng, now=datetime(2026, 7, 1, 16, 20, tzinfo=UTC))["engine"]["status"] == "down"


def test_api_health_alert_severity_and_window():
    # Severity is message-derived now (not the type name), so use realistic message text.
    eng = _engine()
    with eng.begin() as c:
        c.execute(insert(db.alerts).values(ts=datetime(2026, 7, 1, 15, 30), alert_type="risk_gate_block",
                  message="risk gate blocked rebalance: leverage breach", delivered=True))   # error, in-window
        c.execute(insert(db.alerts).values(ts=datetime(2026, 7, 1, 15), alert_type="data_staleness",
                  message="position divergence on 2 name(s)", delivered=True))               # warn, in-window
        c.execute(insert(db.alerts).values(ts=datetime(2026, 6, 1, 15), alert_type="system_error",
                  message="ingest produced stale prices", delivered=True))                   # >24h → excluded
    a = data.api_health(eng, now=datetime(2026, 7, 1, 16, tzinfo=UTC))["alerts_24h"]
    assert a["total"] == 2 and a["errors"] == 1 and a["warnings"] == 1 and a["worst"] == "error"
    assert a["latest"]["severity"] == "error"        # most recent overall (15:30) surfaced for the header
    assert "leverage breach" in a["latest"]["message"]


def test_api_health_empty_is_safe():
    h = data.api_health(_engine(), now=datetime(2026, 7, 1, 12, tzinfo=UTC))
    assert h["engine"]["status"] == "down" and h["engine"]["age_s"] is None
    assert h["last_rebalance"] is None and h["drift"]["l1"] is None
    assert h["next_rebalance"]["date"] and h["alerts_24h"]["total"] == 0
    assert h["freshness"]["factors_date"] is None


def test_empty_db_is_safe():
    eng = _engine()
    s = data.api_state(eng)
    assert s["nav"] is None and s["positions"] == [] and s["premium_collected"] == 0.0
    assert s["leverage"] is None and s["gross_exposure"] is None and s["n_positions"] == 0
    assert data.api_orders(eng) == [] and data.api_calls(eng) == [] and data.api_factors(eng) == []
    assert data.api_nav_history(eng) == []


def test_apply_live_prices_marks_nav_and_positions():
    # AAPL snap price = 20000/100 = $200; a live $210 tick adds 100×$10 = $1,000 to NAV.
    state = {"nav": 200_000.0, "day_pnl": 2_000.0, "day_pnl_pct": 0.01, "leverage": 1.0,
             "gross_exposure": 200_000.0,
             "positions": [{"symbol": "AAPL", "qty": 100, "market_value": 20_000.0, "weight": 0.10},
                           {"symbol": "MSFT", "qty": 50, "market_value": 10_000.0, "weight": 0.05}]}
    out = data.apply_live_prices(state, {"AAPL": 210.0})          # MSFT has no live tick → unchanged
    assert out["prices_live"] is True
    aapl = next(r for r in out["positions"] if r["symbol"] == "AAPL")
    assert aapl["last_price"] == 210.0 and aapl["market_value"] == 21_000.0
    assert out["nav"] == 201_000.0                                # 200k + 100×(210−200)
    assert out["day_pnl"] == 3_000.0                              # basis 198k → 201k − 198k
    assert out["gross_exposure"] == 201_000.0
    msft = next(r for r in out["positions"] if r["symbol"] == "MSFT")
    assert msft.get("last_price") is None and msft["market_value"] == 10_000.0


def test_apply_live_prices_noop_without_prices():
    state = {"nav": 100.0, "day_pnl": 0.0, "positions": [{"symbol": "AAPL", "qty": 1, "market_value": 100.0}]}
    out = data.apply_live_prices(state, {})
    assert out["nav"] == 100.0 and "prices_live" not in out and "last_price" not in out["positions"][0]


def test_apply_live_prices_computes_day_pct_from_prev_close():
    state = {"nav": 100_000.0, "day_pnl": 0.0, "positions": [
        {"symbol": "AAPL", "qty": 100, "market_value": 20_000.0}]}
    out = data.apply_live_prices(state, {"AAPL": 210.0}, {"AAPL": 200.0})   # prior close 200 → +5%
    row = out["positions"][0]
    assert abs(row["day_pct"] - 0.05) < 1e-9


def test_apply_live_prices_ignores_bad_tick():
    # A stray tick >35% off the snapshot price (20000/100 = $200) must be ignored, not 10× the row.
    state = {"nav": 100_000.0, "day_pnl": 0.0,
             "positions": [{"symbol": "AAPL", "qty": 100, "market_value": 20_000.0}]}
    out = data.apply_live_prices(state, {"AAPL": 2000.0}, {"AAPL": 200.0})   # bad 10× print
    row = out["positions"][0]
    assert row["market_value"] == 20_000.0 and "last_price" not in row and "day_pct" not in row
    assert out["nav"] == 100_000.0                                          # NAV unmoved by the bad tick
