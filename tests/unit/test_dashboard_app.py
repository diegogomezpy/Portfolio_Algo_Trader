"""Smoke test for dashboard.app — wiring (routes present) without an HTTP client.

The data layer is covered by test_dashboard_data; here we just confirm create_app builds
against an injected engine and exposes the documented routes (no httpx/TestClient needed).
"""

from __future__ import annotations

from sqlalchemy import create_engine

from dashboard.app import create_app
from engine import db


def test_create_app_exposes_expected_routes():
    eng = create_engine("sqlite://")
    db.create_all(eng)
    app = create_app(eng)
    paths = {r.path for r in app.routes}
    assert {"/", "/backtest", "/favicon.svg", "/api/meta", "/api/state",
            "/api/nav_history", "/api/orders", "/api/calls", "/api/factors",
            "/api/alerts"} <= paths
    # the shared theme is mounted so both tabs reference one design system
    assert any(getattr(r, "path", "") == "/static" for r in app.routes)


def test_index_route_serves_html():
    eng = create_engine("sqlite://")
    db.create_all(eng)
    app = create_app(eng)
    # call the index route handler directly (no server): it returns the self-contained page
    index = next(r for r in app.routes if getattr(r, "path", None) == "/")
    html = index.endpoint()
    assert "sharpe-engine" in html and "/api/state" in html
    assert "/static/theme.css" in html        # links the shared dark design system


def test_meta_route_is_config_driven():
    eng = create_engine("sqlite://")
    db.create_all(eng)
    app = create_app(eng, env="paper")
    meta = next(r for r in app.routes if getattr(r, "path", None) == "/api/meta").endpoint()
    assert meta["env"] == "paper"
    assert meta["leverage_cap"] >= 1.0 and "target_delta" in meta
