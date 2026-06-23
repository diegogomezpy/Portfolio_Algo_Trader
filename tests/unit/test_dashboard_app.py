"""Smoke test for dashboard.app — wiring (routes present) without an HTTP client.

The data layer is covered by test_dashboard_data; here we just confirm create_app builds
against an injected engine and exposes the documented routes (no httpx/TestClient needed).
"""

from __future__ import annotations

from sqlalchemy import create_engine

from dashboard.app import create_app
from engine import db


class _FakeClient:
    """Minimal stand-in for the Alpaca read client (just the orders surface)."""

    def __init__(self, orders):
        self._orders = orders

    def get_orders(self, status="all", limit=50):
        return self._orders


def _route(app, path):
    return next(r for r in app.routes if getattr(r, "path", None) == path).endpoint


def test_create_app_exposes_expected_routes():
    eng = create_engine("sqlite://")
    db.create_all(eng)
    app = create_app(eng, live=False)
    paths = {r.path for r in app.routes}
    assert {"/", "/backtest", "/favicon.svg", "/api/meta", "/api/state",
            "/api/nav_history", "/api/orders", "/api/calls", "/api/factors",
            "/api/alerts"} <= paths
    # the shared theme is mounted so both tabs reference one design system
    assert any(getattr(r, "path", "") == "/static" for r in app.routes)


def test_index_route_serves_html():
    eng = create_engine("sqlite://")
    db.create_all(eng)
    app = create_app(eng, live=False)
    # call the index route handler directly (no server): it returns the self-contained page
    html = _route(app, "/")()
    assert "sharpe-engine" in html and "/api/state" in html
    assert "/static/theme.css" in html        # links the shared dark design system


def test_meta_route_is_config_driven():
    eng = create_engine("sqlite://")
    db.create_all(eng)
    app = create_app(eng, env="paper", live=False)
    meta = _route(app, "/api/meta")()
    assert meta["env"] == "paper"
    assert meta["leverage_cap"] >= 1.0 and "target_delta" in meta
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


def test_orders_route_postgres_only_when_live_false():
    eng = create_engine("sqlite://")
    db.create_all(eng)
    # a client is passed but live=False must ignore it and read Postgres (empty here)
    fake = _FakeClient([{"symbol": "AAPL", "side": "buy", "qty": 10.0, "type": "market",
                         "status": "new", "filled_qty": 0.0, "filled_avg_price": None,
                         "submitted_at": None}])
    app = create_app(eng, client=fake, live=False)
    assert _route(app, "/api/orders")() == []
