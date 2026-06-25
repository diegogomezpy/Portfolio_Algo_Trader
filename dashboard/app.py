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
from datetime import date, timedelta
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
    dash = getattr(settings, "dashboard", None)
    return {
        "env": env,
        "leverage_cap": getattr(pf, "max_leverage", 2.0) if pf else 2.0,
        "target_leverage": getattr(pf, "target_leverage", 2.0) if pf else 2.0,
        "max_single_name_pct": getattr(pf, "max_single_name_pct", 0.05) if pf else 0.05,
        "target_delta": getattr(cc, "target_delta", 0.30) if cc else 0.30,
        "live_benchmarks": list(getattr(dash, "live_benchmarks", ["SPY", "XYLD", "JEPI"]))
        if dash else ["SPY", "XYLD", "JEPI"],
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


def _align(closes: dict, dates: list[str]) -> list[float]:
    """Reindex a {date: close} map onto ``dates``, forward-filling the most recent prior close."""
    import bisect
    items = sorted(closes.items())
    keys, vals = [k for k, _ in items], [v for _, v in items]
    out = []
    for d in dates:
        i = bisect.bisect_right(keys, d) - 1
        out.append(vals[i] if i >= 0 else (vals[0] if vals else None))
    return out


def _benchmark_curves(symbols: list[str], start: str, strat_dates: list[str]) -> dict:
    """Per-benchmark normalized curve (aligned to ``strat_dates``, start=1.0) + stats + label.

    Sources come from :mod:`engine.benchmarks` (SPY via yfinance, BXMD/BXRD from CBOE's CDN),
    cached + best-effort, so this never breaks the page if a feed is down.
    """
    if not symbols or not strat_dates:
        return {}
    from engine import benchmarks
    raw = benchmarks.fetch_closes(symbols, start)
    out = {}
    for sym in symbols:
        series = _align(raw.get(sym, {}), strat_dates)
        if not series or not series[0]:
            continue
        stats = data.series_stats(series)
        out[sym] = {"norm": [v / series[0] for v in series],
                    **benchmarks.describe(sym),
                    **{k: stats[k] for k in ("total_return", "ann_return", "ann_vol",
                                             "sharpe", "max_drawdown")}}
    return out


def _market_status(client) -> dict:
    """Live market open/closed + next session from Alpaca's clock (operational tile)."""
    c = client.market_clock()
    return {"is_open": bool(c.get("is_open")), "next_open": c.get("next_open"),
            "next_close": c.get("next_close"), "timestamp": c.get("timestamp")}


def _confirm_next_rebalance(client, est_date: str) -> str | None:
    """Holiday-correct first NYSE trading day of ``est_date``'s month, via the live calendar.

    The data layer estimates the first *weekday*; the real first *trading* day can slip when a
    holiday lands on it (e.g. Jul 4). We read the month's opening sessions and take the
    earliest — which is exactly that holiday correction. Returns ``None`` if the feed is down.
    """
    d = date.fromisoformat(est_date)
    month_start = d.replace(day=1)
    cal = client.market_calendar(month_start.isoformat(), (month_start + timedelta(days=8)).isoformat())
    days = sorted(str(x["date"])[:10] for x in cal if x.get("date"))
    return days[0] if days else None


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

    @app.get("/api/track_record")
    def track_record() -> dict:
        """Realized paper performance since inception + SPY/covered-call-ETF benchmarks."""
        tr = data.api_track_record(db_engine)
        if not tr.get("available"):
            return {**tr, "benchmarks": {}}
        benchmarks = _benchmark_curves(meta.get("live_benchmarks", []), tr["inception"], tr["dates"])
        return {**tr, "benchmarks": benchmarks}

    @app.get("/api/slippage")
    def slippage() -> dict:
        return data.api_slippage(db_engine)

    @app.get("/api/reference")
    def reference() -> dict:
        """Static instrument labels {symbol: {name, sector, industry}} for the held names.

        Best-effort reference data (SEC company names + the cached SIC→GICS sector map), so the
        front-end can show company name + sector beside each ticker. A missing cache / offline
        SEC degrades to an empty map — the UI falls back to bare tickers. Read once on page boot.
        """
        try:
            from engine import instruments
            return instruments.reference_map(data.held_symbols(db_engine))
        except Exception as exc:  # noqa: BLE001 — labels are cosmetic; never break the page
            log.warning("reference build failed: %s", exc)
            return {}

    @app.get("/api/health")
    def health() -> dict:
        """Operational health: engine heartbeat, rebalance schedule, drift, alerts, market hours.

        Postgres-only base (``data.api_health``) refined with the live Alpaca clock (market
        open/closed) and the real NYSE calendar (holiday-correct next-rebalance date) when a
        client is up. Both refinements are best-effort — a feed hiccup leaves the base intact.
        """
        h = data.api_health(db_engine)
        if client is not None:
            try:
                h["market"] = _market_status(client)
            except Exception as exc:  # noqa: BLE001 — never let a feed hiccup break the panel
                log.warning("health: market clock read failed: %s", exc)
            try:
                confirmed = _confirm_next_rebalance(client, h["next_rebalance"]["date"])
                if confirmed:
                    today = date.fromisoformat(h["now"][:10])
                    h["next_rebalance"] = {"date": confirmed, "source": "confirmed",
                                           "days_until": (date.fromisoformat(confirmed) - today).days}
            except Exception as exc:  # noqa: BLE001
                log.warning("health: calendar refine failed: %s", exc)
        return h

    return app
