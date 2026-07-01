"""Smoke test for dashboard.app — wiring (routes present) without an HTTP client.

The data layer is covered by test_dashboard_data; here we just confirm create_app builds
against an injected engine and exposes the documented routes (no httpx/TestClient needed).
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import create_engine, insert

from dashboard.app import create_app
from engine import db


class _FakeClient:
    """Minimal stand-in for the Alpaca read client (just the orders surface)."""

    def __init__(self, orders):
        self._orders = orders

    def get_orders(self, status="all", limit=50):
        return self._orders


class _MarketClient(_FakeClient):
    """FakeClient plus the clock + calendar surfaces the health route refines with."""

    def market_clock(self):
        return {"timestamp": "2026-06-24T12:00:00-04:00", "is_open": True,
                "next_open": "2026-06-25T09:30:00-04:00", "next_close": "2026-06-24T16:00:00-04:00"}

    def market_calendar(self, start, end):
        # Always reports July 1–2 as the month's opening sessions (holiday-correct first day = Jul 1).
        return [{"date": "2026-07-01", "open": "2026-07-01T09:30:00-04:00", "close": "2026-07-01T16:00:00-04:00"},
                {"date": "2026-07-02", "open": "2026-07-02T09:30:00-04:00", "close": "2026-07-02T16:00:00-04:00"}]


class _BarsClient(_FakeClient):
    """FakeClient plus the bars surface the sparkline-seeding route reads."""

    def bars_multi(self, symbols, start, end, timeframe="1Day"):
        return {s: [{"close": 100.0 + i} for i in range(5)] for s in symbols}


def _route(app, path):
    return next(r for r in app.routes if getattr(r, "path", None) == path).endpoint


def test_create_app_exposes_expected_routes():
    eng = create_engine("sqlite://")
    db.create_all(eng)
    app = create_app(eng, live=False)
    paths = {r.path for r in app.routes}
    assert {"/", "/backtest", "/favicon.svg", "/api/meta", "/api/state",
            "/api/nav_history", "/api/orders", "/api/calls", "/api/factors",
            "/api/alerts", "/api/health", "/api/reference", "/api/risk"} <= paths
    # the shared theme is mounted so both tabs reference one design system
    assert any(getattr(r, "path", "") == "/static" for r in app.routes)


def test_index_route_serves_html():
    eng = create_engine("sqlite://")
    db.create_all(eng)
    app = create_app(eng, live=False)
    # call the index route handler directly (no server): it returns the self-contained page
    html = _route(app, "/")()
    assert "SFI" in html and "/api/state" in html          # SFI brand lockup
    assert "/static/theme.css" in html        # links the shared dark design system


def test_meta_route_is_config_driven():
    eng = create_engine("sqlite://")
    db.create_all(eng)
    app = create_app(eng, env="paper", live=False)
    meta = _route(app, "/api/meta")()
    assert meta["env"] == "paper"
    assert meta["leverage_cap"] >= 1.0 and "target_delta" in meta
    assert meta["max_sector_pct"] > 0 and meta["max_single_name_pct"] > 0   # concentration caps for the UI
    assert meta["live"] is False                # no client → Postgres-only


def test_orders_route_uses_live_alpaca_when_client_present():
    eng = create_engine("sqlite://")
    db.create_all(eng)
    fake = _FakeClient([{"symbol": "AAPL", "side": "buy", "qty": 10.0, "type": "market",
                         "status": "new", "filled_qty": 0.0, "filled_avg_price": None,
                         "submitted_at": "2026-06-23T16:00:00"}])
    app = create_app(eng, client=fake, live=True)
    out = _route(app, "/api/orders")()
    # comes from Alpaca (a pending 'new' order the engine never wrote to Postgres)
    assert len(out) == 1 and out[0]["symbol"] == "AAPL" and out[0]["status"] == "new"
    assert _route(app, "/api/meta")()["live"] is True


def test_price_history_route_seeds_from_bars():
    eng = create_engine("sqlite://")
    db.create_all(eng)
    with eng.begin() as c:
        c.execute(insert(db.snapshots).values(
            ts=datetime(2026, 7, 1, 16), nav=100_000.0, cash=0.0, last_equity=100_000.0,
            weights={"AAPL": 0.5}, positions={"AAPL": 100}, drift=0.0))
    app = create_app(eng, client=_BarsClient([]), live=True)
    out = _route(app, "/api/price_history")()
    assert out["available"] is True
    assert out["history"]["AAPL"] == [100.0, 101.0, 102.0, 103.0, 104.0]   # closes only, chronological


def test_price_history_route_empty_without_client():
    eng = create_engine("sqlite://")
    db.create_all(eng)
    out = _route(create_app(eng, live=False), "/api/price_history")()
    assert out["available"] is False and out["history"] == {}


def test_events_route_merges_expiries_rebalance_and_cached_earnings():
    import time as _t
    from datetime import date as _date, timedelta as _td
    from dashboard import app as app_module

    eng = create_engine("sqlite://")
    db.create_all(eng)
    today = _date.today()
    exp = today + _td(days=20)
    with eng.begin() as c:
        c.execute(insert(db.snapshots).values(
            ts=datetime(2026, 7, 1, 16), nav=100_000.0, cash=0.0, last_equity=100_000.0,
            weights={"AAPL": 0.5, "AAPL260717C00210000": 0.0},
            positions={"AAPL": 100, "AAPL260717C00210000": -1}, drift=0.0))
        c.execute(insert(db.options_lifecycle).values(
            ts=datetime(2026, 7, 1), event_type="write", underlying="AAPL",
            option_symbol="AAPL260717C00210000", strike=210.0, expiration=exp,
            contracts=1, premium=100.0))
    # pre-seed the earnings cache as fresh so the route uses it (no yfinance / network in tests)
    app_module._EARN_CACHE.update(ts=_t.time(), key="AAPL", loading=False,
                                  data={"AAPL": [str(today + _td(days=30))]})
    out = _route(create_app(eng, live=False), "/api/events")()
    by_type = {e["type"]: e for e in out["events"]}
    assert "expiry" in by_type and by_type["expiry"]["symbol"] == "AAPL"
    assert "earnings" in by_type and by_type["earnings"]["days_until"] == 30
    assert "rebalance" in by_type                                   # estimated next monthly
    assert out["events"] == sorted(out["events"], key=lambda e: (e["date"], e["type"]))


def test_health_route_postgres_only_omits_market():
    eng = create_engine("sqlite://")
    db.create_all(eng)
    app = create_app(eng, live=False)
    h = _route(app, "/api/health")()
    # Postgres-only: heartbeat + schedule present, but no live market hours and an estimated date
    assert "engine" in h and "drift" in h and "alerts_24h" in h
    assert h["next_rebalance"]["source"] == "estimated" and "market" not in h


def test_health_route_refines_with_live_client():
    eng = create_engine("sqlite://")
    db.create_all(eng)
    app = create_app(eng, client=_MarketClient([]), live=True)
    h = _route(app, "/api/health")()
    # the live clock fills the market tile; the calendar confirms the holiday-correct rebalance day
    assert h["market"]["is_open"] is True and h["market"]["next_close"]
    assert h["next_rebalance"]["source"] == "confirmed" and h["next_rebalance"]["date"] == "2026-07-01"


def test_risk_route_postgres_only():
    eng = create_engine("sqlite://")
    db.create_all(eng)
    with eng.begin() as c:
        for i, nv in enumerate([100_000.0, 101_000.0, 99_000.0, 102_000.0]):
            c.execute(insert(db.snapshots).values(
                ts=datetime(2026, 6, 1 + i, 16), nav=nv, cash=0.0, last_equity=100_000.0,
                weights={}, positions={}, drift=0.0))
    r = _route(create_app(eng, live=False), "/api/risk")()
    assert r["available"] and r["days"] == 4 and "drawdown" in r
    assert r["max_drawdown"] < 0 and r["var95_1d_pct"] > 0


def test_orders_route_postgres_only_when_live_false():
    eng = create_engine("sqlite://")
    db.create_all(eng)
    # a client is passed but live=False must ignore it and read Postgres (empty here)
    fake = _FakeClient([{"symbol": "AAPL", "side": "buy", "qty": 10.0, "type": "market",
                         "status": "new", "filled_qty": 0.0, "filled_avg_price": None,
                         "submitted_at": None}])
    app = create_app(eng, client=fake, live=False)
    assert _route(app, "/api/orders")() == []
