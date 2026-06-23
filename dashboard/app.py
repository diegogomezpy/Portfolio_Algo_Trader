"""FastAPI app for the live dashboard (Phase 5.2).

A thin wrapper over :mod:`dashboard.data` — every route just returns a Postgres read, and
``/`` serves the self-contained ``static/index.html`` which polls the ``/api/*`` endpoints.
``create_app`` takes an injectable ``db_engine`` (so it builds against in-memory sqlite in
tests); in production it builds from ``DATABASE_URL``. No Alpaca access — Postgres only.

The ``static/`` directory (shared ``theme.css`` + assets) is mounted at ``/static`` so both
the live page and the generated backtest page reference one design system. ``/api/meta``
surfaces config (environment, leverage cap, target delta) so the UI is config-driven, not
hardcoded — read once from ``settings.yaml`` at startup.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import HTMLResponse, Response
from fastapi.staticfiles import StaticFiles

from dashboard import data

_STATIC = Path(__file__).parent / "static"
_INDEX = _STATIC / "index.html"
_BACKTEST = Path(__file__).parent.parent / "reports" / "backtest_dashboard.html"

# A tiny inline SVG mark used as the favicon and the header logo (generic — no branding).
_FAVICON = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32">'
    '<rect width="32" height="32" rx="7" fill="#121925"/>'
    '<path d="M5 21 L13 13 L18 18 L27 7" fill="none" stroke="#3ddc97" '
    'stroke-width="2.6" stroke-linecap="round" stroke-linejoin="round"/>'
    '<circle cx="27" cy="7" r="2.4" fill="#3ddc97"/></svg>'
)


def _load_meta(env: str, settings) -> dict:
    """Config context for the UI. Best-effort: falls back to sane defaults if YAML absent."""
    if settings is None:
        try:
            from engine import config
            settings = config.load_settings()
        except Exception:
            settings = None
    pf = getattr(settings, "portfolio", None)
    cc = getattr(settings, "covered_calls", None)
    return {
        "env": env,
        "leverage_cap": getattr(pf, "max_leverage", 2.0) if pf else 2.0,
        "target_leverage": getattr(pf, "target_leverage", 2.0) if pf else 2.0,
        "max_single_name_pct": getattr(pf, "max_single_name_pct", 0.05) if pf else 0.05,
        "target_delta": getattr(cc, "target_delta", 0.30) if cc else 0.30,
    }


def create_app(db_engine=None, *, env: str = "paper", settings=None) -> FastAPI:
    if db_engine is None:
        from engine import db
        db_engine = db.get_engine()

    meta = _load_meta(env, settings)
    app = FastAPI(title="sharpe-engine dashboard")
    if _STATIC.exists():
        app.mount("/static", StaticFiles(directory=str(_STATIC)), name="static")

    @app.get("/", response_class=HTMLResponse)
    def index() -> str:
        return _INDEX.read_text() if _INDEX.exists() else "<h1>sharpe-engine dashboard</h1>"

    @app.get("/favicon.svg")
    def favicon() -> Response:
        return Response(content=_FAVICON, media_type="image/svg+xml")

    @app.get("/backtest", response_class=HTMLResponse)
    def backtest() -> str:
        """Serve the static backtest-analytics dashboard (the 'Backtest' tab's iframe)."""
        if _BACKTEST.exists():
            return _BACKTEST.read_text()
        return ("<div style='font:14px sans-serif;color:#7c8a9e;padding:40px'>"
                "No backtest dashboard yet — run <code>python scripts/build_dashboard.py</code>.</div>")

    @app.get("/api/meta")
    def api_meta() -> dict:
        return meta

    @app.get("/api/state")
    def state() -> dict:
        return data.api_state(db_engine)

    @app.get("/api/nav_history")
    def nav_history(limit: int = 120) -> list:
        return data.api_nav_history(db_engine, limit)

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
