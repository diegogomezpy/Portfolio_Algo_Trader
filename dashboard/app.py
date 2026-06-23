"""FastAPI app for the live dashboard (Phase 5.2 / 5.5).

Routes are thin wrappers over :mod:`dashboard.data` (Postgres reads) plus, when an Alpaca
client is available, a **self-updating** layer so the dashboard stays live on its own —
without ``run_eod`` running:

* a background **monitor loop** (default every 60s) reads the Alpaca account + positions and
  writes a fresh snapshot to Postgres, so NAV / cash / positions / leverage keep updating and
  the NAV sparkline keeps accumulating history; and
* ``/api/orders`` reads **live** Alpaca orders (open + recent), so an order placed directly on
  Alpaca — even a still-pending one — shows up immediately, not just orders the engine placed.

If no Alpaca credentials are present (or ``live=False``), the app degrades cleanly to the
original Postgres-only behaviour. ``create_app`` takes an injectable ``db_engine`` and
``client`` so it builds against in-memory sqlite + fakes in tests. ``/api/meta`` surfaces
config (env, leverage cap, target delta) read once from ``settings.yaml`` at startup.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import HTMLResponse, Response
from fastapi.staticfiles import StaticFiles

from dashboard import data

log = logging.getLogger("dashboard")

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


def _build_client():
    """Build the read-only Alpaca client from the loaded environment, or None if unavailable.

    Credentials are already loaded into the environment by ``run_dashboard`` (``load_env``);
    this never raises — a missing key just disables the live layer (Postgres-only fallback).
    """
    try:
        from engine import config
        return config.get_alpaca_client()
    except Exception as exc:  # noqa: BLE001 — any creds/SDK issue → degrade, don't crash
        log.warning("live layer disabled (no Alpaca client): %s", exc)
        return None


async def _monitor_loop(client, db_engine, interval: int) -> None:
    """Every ``interval`` seconds: read Alpaca → write a snapshot (the Alpaca→Postgres bridge).

    Runs the synchronous monitor in a worker thread so the event loop stays responsive; a
    failed pass is logged and the loop continues (alerting/monitoring must never crash the UI).
    """
    from engine import monitor
    log.info("dashboard monitor loop started (every %ss)", interval)
    while True:
        try:
            tw = await asyncio.to_thread(monitor.last_target_weights, db_engine)
            await asyncio.to_thread(monitor.monitor_once, client, db_engine, target_weights=tw)
        except Exception as exc:  # noqa: BLE001
            log.warning("dashboard monitor pass failed: %s", exc)
        await asyncio.sleep(interval)


def _live_orders(client, limit: int) -> list[dict]:
    """Live Alpaca orders (open + recent), shaped like :func:`dashboard.data.api_orders`."""
    orders = client.get_orders(status="all", limit=limit)
    return [{"symbol": o.get("symbol"), "side": o.get("side"), "qty": o.get("qty"),
             "type": o.get("type"), "status": o.get("status"), "filled_qty": o.get("filled_qty"),
             "filled_avg_price": o.get("filled_avg_price"),
             "submitted_at": o.get("submitted_at")} for o in orders]


def create_app(db_engine=None, *, env: str = "paper", settings=None, client=None,
               live: bool = True, monitor_interval: int = 60) -> FastAPI:
    if db_engine is None:
        from engine import db
        db_engine = db.get_engine()

    meta = _load_meta(env, settings)
    # The live layer (background monitor + live Alpaca orders) is on by default; pass live=False
    # for the original Postgres-only behaviour. A client may be injected (tests); else built.
    if live and client is None:
        client = _build_client()
    if not live:
        client = None

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI):
        task = None
        if client is not None:
            task = asyncio.create_task(_monitor_loop(client, db_engine, monitor_interval))
        try:
            yield
        finally:
            if task is not None:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task

    app = FastAPI(title="sharpe-engine dashboard", lifespan=lifespan)
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
        return {**meta, "live": client is not None, "monitor_interval": monitor_interval}

    @app.get("/api/state")
    def state() -> dict:
        return data.api_state(db_engine)

    @app.get("/api/nav_history")
    def nav_history(limit: int = 120) -> list:
        return data.api_nav_history(db_engine, limit)

    @app.get("/api/orders")
    def orders(limit: int = 50) -> list:
        # Live Alpaca orders when available (reflects orders placed directly on Alpaca,
        # incl. still-pending ones); fall back to the engine's Postgres orders otherwise.
        if client is not None:
            try:
                return _live_orders(client, limit)
            except Exception as exc:  # noqa: BLE001
                log.warning("live orders read failed, falling back to Postgres: %s", exc)
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
