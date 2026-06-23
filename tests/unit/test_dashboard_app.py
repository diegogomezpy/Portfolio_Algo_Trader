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
    assert {"/", "/backtest", "/api/state", "/api/orders", "/api/calls",
            "/api/factors", "/api/alerts"} <= paths


def test_index_route_serves_html():
    eng = create_engine("sqlite://")
    db.create_all(eng)
    app = create_app(eng)
    # call the index route handler directly (no server): it returns the self-contained page
    index = next(r for r in app.routes if getattr(r, "path", None) == "/")
    html = index.endpoint()
    assert "sharpe-engine" in html and "/api/state" in html
