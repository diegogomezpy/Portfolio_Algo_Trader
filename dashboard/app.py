"""FastAPI app for the live dashboard (Phase 5.2).

A thin wrapper over :mod:`dashboard.data` — every route just returns a Postgres read, and
``/`` serves the self-contained ``static/index.html`` which polls the ``/api/*`` endpoints.
``create_app`` takes an injectable ``db_engine`` (so it builds against in-memory sqlite in
tests); in production it builds from ``DATABASE_URL``. No Alpaca access — Postgres only.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import HTMLResponse

from dashboard import data

_INDEX = Path(__file__).parent / "static" / "index.html"


def create_app(db_engine=None) -> FastAPI:
    if db_engine is None:
        from engine import db
        db_engine = db.get_engine()

    app = FastAPI(title="sharpe-engine dashboard")

    @app.get("/", response_class=HTMLResponse)
    def index() -> str:
        return _INDEX.read_text() if _INDEX.exists() else "<h1>sharpe-engine dashboard</h1>"

    @app.get("/api/state")
    def state() -> dict:
        return data.api_state(db_engine)

    @app.get("/api/orders")
    def orders(limit: int = 50) -> list:
        return data.api_orders(db_engine, limit)

    @app.get("/api/calls")
    def calls() -> list:
        return data.api_calls(db_engine)

    @app.get("/api/factors")
    def factors() -> list:
        return data.api_factors(db_engine)

    @app.get("/api/alerts")
    def alerts(limit: int = 50) -> list:
        return data.api_alerts(db_engine, limit)

    return app
