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
import hmac
import json
import logging
import os
import subprocess
import sys
import threading
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from fastapi import FastAPI, Header
from fastapi.responses import HTMLResponse, Response
from fastapi.staticfiles import StaticFiles

from dashboard import data

log = logging.getLogger("dashboard")

_STATIC = Path(__file__).parent / "static"
_INDEX = _STATIC / "index.html"
_BACKTEST = Path(__file__).parent.parent / "reports" / "backtest_dashboard.html"

# ---------------------------------------------------------------------------- #
# Execution console — manual actions spawned out-of-process (rebalance via run_eod,
# everything else via scripts/manual_exec.py). ONE child at a time, module-wide — the
# same guarantee whether the user opens one tab or five.
# ---------------------------------------------------------------------------- #
_ROOT = Path(__file__).resolve().parent.parent
_EXEC_LOG = "/tmp/sepi_manual_exec.log"
_EXEC_ACTIONS = ("rebalance", "liquidate", "trade", "leverage")
_MANUAL: dict = {"proc": None, "action": None, "mode": None, "params": None,
                 "cycle_key": None, "started_at": None}


def _exec_gate(token_header: str | None) -> dict | None:
    """Token check for trade-placing endpoints; ``None`` = authorized.

    The token lives in the service environment (``SEPI_EXEC_TOKEN`` in the git-ignored env
    file) — never in source or YAML. Unset ⇒ the console is disabled with instructions, so
    a fresh deploy can't expose unauthenticated trading endpoints by accident.
    """
    configured = os.environ.get("SEPI_EXEC_TOKEN")
    if not configured:
        return {"error": "execution console disabled — set SEPI_EXEC_TOKEN in the service "
                         "env file and restart the dashboard", "disabled": True}
    if not token_header or not hmac.compare_digest(str(token_header), configured):
        return {"error": "bad or missing X-Exec-Token", "unauthorized": True}
    return None


def _market_gate(client) -> dict | None:
    """Refuse to trade into a closed market; ``None`` = open."""
    try:
        clk = client.market_clock()
    except Exception as exc:  # noqa: BLE001 — no clock, no trading
        return {"error": f"market clock unreadable ({exc}) — refusing to trade blind"}
    if not clk.get("is_open"):
        return {"error": "market closed", "market_closed": True,
                "next_open": clk.get("next_open")}
    return None


def _spawn_manual(action: str, mode: str, params: dict, env: str, cycle_key: str) -> subprocess.Popen:
    if action == "rebalance":
        cmd = [sys.executable, str(_ROOT / "scripts" / "run_eod.py"), "--once", "--env", env]
        if mode == "express":
            cmd.append("--express")
    else:
        cmd = [sys.executable, str(_ROOT / "scripts" / "manual_exec.py"), "--env", env,
               "--action", action, "--mode", mode, "--cycle-key", cycle_key]
        for k, v in (params or {}).items():
            if v is not None:
                cmd += [f"--{k}", str(v)]
    logf = open(_EXEC_LOG, "ab")  # noqa: SIM115 — handed to the child for its lifetime
    logf.write(f"\n===== {datetime.now(timezone.utc).isoformat()} "
               f"{action} {mode} {params} =====\n".encode())
    return subprocess.Popen(cmd, cwd=str(_ROOT), stdout=logf, stderr=subprocess.STDOUT,
                            start_new_session=True)


def _last_outcome() -> dict | None:
    """Best-effort parse of the finished child's tail: the CLI's result JSON, or run_eod's
    ``Cycle <date> → <status>`` line (how 'market closed — skipped' gets surfaced honestly)."""
    try:
        raw = Path(_EXEC_LOG).read_bytes()[-8000:].decode(errors="replace")
    except Exception:  # noqa: BLE001
        return None
    for line in reversed(raw.strip().splitlines()):
        line = line.strip()
        if line.startswith("{") and line.endswith("}"):
            with contextlib.suppress(Exception):
                return json.loads(line)
        if line.startswith("Cycle ") and "→" in line:
            return {"summary": line}
    return None

# The SFI mark — a 3×3 dot grid with the middle column in teal — on a rounded navy tile.
# (See the SFI Design Language: docs/DESIGN_LANGUAGE.md / design_lang/.)
_FAVICON = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 36 36">'
    '<rect width="36" height="36" rx="8" fill="#0e1830"/>'
    '<circle cx="9" cy="9" r="3.1" fill="#34425e"/><circle cx="18" cy="9" r="3.1" fill="#46b8ad"/><circle cx="27" cy="9" r="3.1" fill="#34425e"/>'
    '<circle cx="9" cy="18" r="3.1" fill="#34425e"/><circle cx="18" cy="18" r="3.1" fill="#46b8ad"/><circle cx="27" cy="18" r="3.1" fill="#34425e"/>'
    '<circle cx="9" cy="27" r="3.1" fill="#34425e"/><circle cx="18" cy="27" r="3.1" fill="#46b8ad"/><circle cx="27" cy="27" r="3.1" fill="#34425e"/></svg>'
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
        "max_sector_pct": getattr(pf, "max_sector_pct", 0.30) if pf else 0.30,
        "target_delta": getattr(cc, "target_delta", 0.30) if cc else 0.30,
        # Overlay config for the SPY beta-overwrite panel (see engine.covered_calls.write_index_overwrite).
        "overlay_mode": getattr(cc, "overlay_mode", "index") if cc else "index",
        "overwrite_coverage": getattr(cc, "overwrite_coverage", 1.0) if cc else 1.0,
        "wing_delta": getattr(cc, "wing_delta", 0.15) if cc else 0.15,
        "beta_market": getattr(getattr(settings, "factors", None), "beta_market", "SPY") if settings else "SPY",
        "live_benchmarks": list(getattr(dash, "live_benchmarks", ["SPY", "BXMD", "BXRD"]))
        if dash else ["SPY", "BXMD", "BXRD"],
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


def _held_equities(db_engine) -> list[str]:
    """Equity symbols currently held (from the latest snapshot) — the price feed's subscription."""
    try:
        return [r["symbol"] for r in data.api_state(db_engine).get("positions", [])
                if r.get("qty") and not data._is_option(r["symbol"])]
    except Exception:  # noqa: BLE001
        return []


# Earnings dates for the Events tab come from yfinance (the engine's fetch_earnings_dates) — the one
# external call the dashboard makes. It's slow per-symbol, so it's refreshed in a background thread
# and cached; the route never blocks on it and serves whatever's cached (possibly empty on the very
# first paint, filling in within ~30s). Best-effort: a Yahoo hiccup just leaves earnings empty.
_EARN_CACHE: dict = {"ts": 0.0, "data": {}, "loading": False, "key": ""}
_EARN_TTL = 6 * 3600


def _events_symbols(db_engine) -> list[str]:
    """Held equities + call underlyings — the names an earnings calendar should cover."""
    unders = [c.get("underlying") for c in data.api_calls(db_engine) if c.get("underlying")]
    return sorted({*_held_equities(db_engine), *unders})


def _earnings_map(symbols: list[str]) -> tuple[dict, bool]:
    """Cached ``{symbol: [ISO dates]}`` earnings, refreshed off-thread. Returns (data, loading)."""
    key = ",".join(symbols)
    fresh = _EARN_CACHE["key"] == key and (time.time() - _EARN_CACHE["ts"]) < _EARN_TTL
    if symbols and not fresh and not _EARN_CACHE["loading"]:
        _EARN_CACHE["loading"] = True

        def _load():
            try:
                from engine.covered_calls import fetch_earnings_dates
                m = fetch_earnings_dates(symbols)
                _EARN_CACHE.update(data={k: [str(d) for d in v] for k, v in m.items()},
                                   ts=time.time(), key=key)
            except Exception as exc:  # noqa: BLE001 — earnings are best-effort; never break the tab
                log.warning("events: earnings fetch failed: %s", exc)
            finally:
                _EARN_CACHE["loading"] = False

        threading.Thread(target=_load, name="events-earnings", daemon=True).start()
    return _EARN_CACHE["data"], _EARN_CACHE["loading"]


# Per-symbol quote detail for the ticker hover-card: today's OHLCV, prior close, and 52-week
# range, from ~1y of daily bars. Refreshed off-thread and cached (the day bar / 52w range move
# slowly; the front-end overlays the sub-second live price for the headline number).
_QUOTE_CACHE: dict = {"ts": 0.0, "data": {}, "loading": False, "key": ""}
_QUOTE_TTL = 45


def _quotes_map(client, symbols: list[str]) -> dict:
    """Cached ``{symbol: {last, open, high, low, volume, prev_close, wk52_high, wk52_low}}``."""
    key = ",".join(symbols)
    fresh = _QUOTE_CACHE["key"] == key and (time.time() - _QUOTE_CACHE["ts"]) < _QUOTE_TTL
    if client is not None and symbols and not fresh and not _QUOTE_CACHE["loading"]:
        _QUOTE_CACHE["loading"] = True

        def _load():
            try:
                end = datetime.now(timezone.utc)
                bars = client.bars_multi(symbols, (end - timedelta(days=370)).isoformat(),
                                         end.isoformat(), timeframe="1Day")
                out = {}
                for sym, rows in bars.items():
                    rows = [b for b in rows if b.get("close") is not None]
                    if not rows:
                        continue
                    last, prev = rows[-1], (rows[-2] if len(rows) > 1 else rows[-1])
                    highs = [b["high"] for b in rows if b.get("high") is not None]
                    lows = [b["low"] for b in rows if b.get("low") is not None]
                    out[sym] = {"last": last.get("close"), "open": last.get("open"),
                                "high": last.get("high"), "low": last.get("low"),
                                "volume": last.get("volume"), "prev_close": prev.get("close"),
                                "wk52_high": max(highs) if highs else None,
                                "wk52_low": min(lows) if lows else None}
                _QUOTE_CACHE.update(data=out, ts=time.time(), key=key)
            except Exception as exc:  # noqa: BLE001 — hover detail is best-effort; never break the page
                log.warning("quotes fetch failed: %s", exc)
            finally:
                _QUOTE_CACHE["loading"] = False

        threading.Thread(target=_load, name="quotes", daemon=True).start()
    return _QUOTE_CACHE["data"]


# Trailing correlation matrix for the Risk view: pairwise Pearson correlation of daily returns
# across the top holdings, from ~3 months of daily bars. Refreshed off-thread and cached (daily
# bars move slowly), same pattern as the quote cache.
_CORR_CACHE: dict = {"ts": 0.0, "data": {}, "loading": False, "key": ""}
_CORR_TTL = 600
_CORR_MAX_NAMES = 10       # keep the matrix readable (mock shows ~8); top holdings by market value
_CORR_WINDOW = 60          # trailing trading days of returns


def _correlation_map(client, db_engine) -> dict:
    """Cached trailing-``_CORR_WINDOW`` correlation matrix over the top held equities by weight."""
    state = data.api_state(db_engine)
    held = sorted((r for r in state.get("positions", [])
                   if r.get("qty") and r.get("market_value") is not None
                   and not data._is_option(r["symbol"])),
                  key=lambda r: r["market_value"], reverse=True)
    symbols = [r["symbol"] for r in held][:_CORR_MAX_NAMES]
    key = ",".join(symbols)
    fresh = _CORR_CACHE["key"] == key and (time.time() - _CORR_CACHE["ts"]) < _CORR_TTL
    if client is not None and len(symbols) >= 2 and not fresh and not _CORR_CACHE["loading"]:
        _CORR_CACHE["loading"] = True

        def _load():
            try:
                end = datetime.now(timezone.utc)
                bars = client.bars_multi(symbols, (end - timedelta(days=130)).isoformat(),
                                         end.isoformat(), timeframe="1Day")
                closes = {sym: {str(b["time"])[:10]: b["close"] for b in rows
                                if b.get("time") and b.get("close") is not None}
                          for sym, rows in bars.items()}
                _CORR_CACHE.update(
                    data=data.correlation_matrix(closes, symbols, window=_CORR_WINDOW),
                    ts=time.time(), key=key)
            except Exception as exc:  # noqa: BLE001 — correlation is best-effort; never break the page
                log.warning("correlation fetch failed: %s", exc)
            finally:
                _CORR_CACHE["loading"] = False

        threading.Thread(target=_load, name="correlation", daemon=True).start()
    return _CORR_CACHE["data"] or {"available": False, "symbols": [], "matrix": []}


def _latest_snapshot_age_s(db_engine) -> float | None:
    """Seconds since the newest ``snapshots`` row (any writer), or ``None`` when empty."""
    from sqlalchemy import desc, select
    from engine.db import snapshots
    with db_engine.connect() as conn:
        row = conn.execute(select(snapshots.c.ts).order_by(desc(snapshots.c.ts)).limit(1)).first()
    ts = data._as_utc(row[0]) if row and row[0] is not None else None
    return (datetime.now(timezone.utc) - ts).total_seconds() if ts else None


async def _monitor_loop(client, db_engine, interval: int, price_feed=None) -> None:
    """Every ``interval`` seconds: read Alpaca → write a snapshot (the Alpaca→Postgres bridge),
    and keep the live price feed subscribed to the current held book.

    The engine process (sharpe-eod) runs its own 60s monitor; when its snapshots are fresh
    this loop skips the duplicate write (audit E5 — two writers doubled snapshot growth to
    ~1M rows/yr) and only does the price-feed upkeep. If the engine goes down, snapshots age
    past the gate and this loop transparently takes over — the resilience both loops existed
    for, without the double writes.

    Runs the synchronous monitor in a worker thread so the event loop stays responsive; a
    failed pass is logged and the loop continues (alerting/monitoring must never crash the UI).
    """
    from engine import monitor
    log.info("dashboard monitor loop started (every %ss)", interval)
    while True:
        try:
            age = await asyncio.to_thread(_latest_snapshot_age_s, db_engine)
            if age is None or age >= interval * 0.75:      # engine's monitor quiet → we write
                tw = await asyncio.to_thread(monitor.last_target_weights, db_engine)
                await asyncio.to_thread(monitor.monitor_once, client, db_engine, target_weights=tw)
            if price_feed is not None:
                price_feed.set_symbols(await asyncio.to_thread(_held_equities, db_engine))
                pos = await asyncio.to_thread(client.all_positions)
                price_feed.set_prev_close({p.get("symbol"): p.get("lastday_px") for p in pos})
                # Prime last prices from Alpaca position marks so prices + day% show even when the
                # trade stream is quiet (after hours / sparse IEX). Live trades overwrite these.
                price_feed.prime({p["symbol"]: (p["market_value"] / p["qty"])
                                  for p in pos if p.get("qty") and p.get("market_value") is not None
                                  and str(p.get("asset_class") or "us_equity").endswith("equity")})
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


# Order statuses that mean "still working" (in the market, not terminal).
_OPEN_ORDER_STATES = {"new", "accepted", "partially_filled", "pending_new",
                      "accepted_for_bidding", "pending_replace", "replaced", "held"}

# Shared TTL cache for the execution panel's Alpaca order read (audit W3): the front-end polls
# /api/execution every 2.5s per tab, and the old code hit Alpaca's REST get_orders on EVERY
# poll, 24/7 (~35k upstream calls/day/tab with nothing working). N tabs now share one upstream
# read per TTL; outside plausible session hours (the panel can't change) the TTL stretches to
# a minute. Keyed on the client instance so tests / reloads never see a stale fake's orders.
_ORDERS_TTL_S = 2.0
_ORDERS_IDLE_TTL_S = 60.0
_ORDERS_CACHE: dict = {"ts": 0.0, "orders": [], "client": None}


def _likely_market_hours(now=None) -> bool:
    """Cheap LOCAL session test (no API call): Mon–Fri, 04:00–20:10 ET — the pre→post window
    in which orders can plausibly be working. A guess for cache-TTL purposes only."""
    from zoneinfo import ZoneInfo
    et = (now or datetime.now(timezone.utc)).astimezone(ZoneInfo("America/New_York"))
    return et.weekday() < 5 and (4, 0) <= (et.hour, et.minute) <= (20, 10)


def _cached_orders(client) -> list[dict]:
    """Recent Alpaca orders through the shared TTL cache; a failed read serves the stale copy."""
    now = time.time()
    ttl = _ORDERS_TTL_S if _likely_market_hours() else _ORDERS_IDLE_TTL_S
    if _ORDERS_CACHE["client"] is client and now - _ORDERS_CACHE["ts"] < ttl:
        return _ORDERS_CACHE["orders"]
    try:
        orders = client.get_orders(status="all", limit=200)
    except Exception as exc:  # noqa: BLE001 — a read hiccup serves the last known list
        log.warning("execution status: order read failed: %s", exc)
        return _ORDERS_CACHE["orders"] if _ORDERS_CACHE["client"] is client else []
    _ORDERS_CACHE.update(ts=now, orders=orders, client=client)
    return orders


def _execution_status(client, db_engine, target_leverage: float) -> dict:
    """Live trading-session progress for the auto-surfacing execution visualizer (Phase 1).

    Poll-only, from Alpaca live orders + the snapshot state — no engine changes. ``active`` is
    True whenever any order is still working (a rebalance / call-write / roll is happening). Reports
    the phase (equity chase vs writing calls vs settling), per-name progress vs target weight, the
    leverage ramp, a working-order blotter, and recent fills.
    """
    state = data.api_state(db_engine)
    orders = _cached_orders(client) if client is not None else []
    is_opt = data._is_option
    openo = [o for o in orders if str(o.get("status") or "").lower() in _OPEN_ORDER_STATES]
    open_by_sym: dict = {}
    for o in openo:
        open_by_sym.setdefault(str(o.get("symbol")), []).append(o)
    open_eq = [o for o in openo if not is_opt(o.get("symbol"))]
    open_opt = [o for o in openo if is_opt(o.get("symbol"))]

    # A running manual action surfaces the panel immediately (before its first order posts)
    # and stamps the header so the visualizer narrates the right thing (console addition).
    manual = None
    mproc = _MANUAL["proc"]
    if mproc is not None and mproc.poll() is None:
        manual = {k: _MANUAL[k] for k in ("action", "mode", "params", "cycle_key", "started_at")}

    active = bool(openo) or manual is not None
    phase = ("writing_calls" if open_opt and not open_eq
             else "equity_chase" if open_eq
             else "idle")

    # Per-name equity progress vs the last rebalance's target weights (carried on each state row).
    names, n_filled, n_target = [], 0, 0
    for r in state.get("positions", []):
        sym = r.get("symbol")
        if not sym or is_opt(sym):
            continue
        tw, hw = r.get("target_weight"), r.get("weight")
        if not tw and not hw:
            continue
        oo = open_by_sym.get(sym, [])
        if oo:
            status = "working"
        elif tw and hw and hw >= tw * 0.9:
            status = "filled"
        elif hw:
            status = "held"
        else:
            status = "pending"
        if tw:                                    # progress is measured against the target book
            n_target += 1
            if status in ("filled", "held"):
                n_filled += 1
        fill_pct = (min(1.0, hw / tw) if (tw and hw) else (1.0 if hw else 0.0))
        names.append({"symbol": sym, "target_weight": tw, "held_weight": hw,
                      "status": status, "fill_pct": fill_pct, "open_orders": len(oo)})
    names.sort(key=lambda d: -((d["target_weight"] or d["held_weight"]) or 0))

    blotter = [{"symbol": o.get("symbol"), "side": o.get("side"), "qty": o.get("qty"),
                "filled_qty": o.get("filled_qty"), "type": o.get("type"),
                "limit_price": o.get("limit_price"), "status": o.get("status"),
                "is_option": is_opt(o.get("symbol"))} for o in openo]
    filled = [o for o in orders if str(o.get("status") or "").lower() == "filled"
              and o.get("filled_avg_price")]
    filled.sort(key=lambda o: str(o.get("filled_at") or o.get("submitted_at") or ""), reverse=True)
    recent = [{"symbol": o.get("symbol"), "side": o.get("side"),
               "qty": o.get("filled_qty") or o.get("qty"),
               "price": o.get("filled_avg_price"), "is_option": is_opt(o.get("symbol"))}
              for o in filled[:12]]

    # Chase board (Phase 2): the per-order limit walk for the live cycle. Only while active, so a
    # finished cycle's telemetry doesn't linger once the panel would otherwise hide. A manual run
    # pins the board to its own cycle_key (it would be the latest anyway; this removes the race).
    chase = (data.api_chase(db_engine, cycle_key=manual["cycle_key"] if manual else None)
             .get("orders", []) if active else [])

    # Rotation flow (Phase 3): equity sells → cash → buys for this cycle, by filled notional — the
    # capital rotation the rebalance performs. Options (the SPY overlay) are excluded; this is the
    # equity-cash story. Aggregated from filled orders (proceeds fund purchases).
    sells: dict = {}
    buys: dict = {}
    for o in filled:
        sym = o.get("symbol")
        if not sym or is_opt(sym):
            continue
        fq = float(o.get("filled_qty") or 0)
        px = o.get("filled_avg_price")
        if not fq or px is None:
            continue
        bucket = sells if str(o.get("side")).lower() == "sell" else buys
        bucket[sym] = bucket.get(sym, 0.0) + fq * float(px)
    _flow = lambda d: [{"symbol": s, "notional": round(v, 2)}
                       for s, v in sorted(d.items(), key=lambda kv: -kv[1])]
    rotation = {"sells": _flow(sells), "buys": _flow(buys),
                "raised": round(sum(sells.values()), 2), "deployed": round(sum(buys.values()), 2)}

    return {"active": active, "phase": phase, "n_filled": n_filled, "n_target": n_target,
            "n_working": len(openo), "leverage": state.get("leverage"),
            "target_leverage": target_leverage, "gross_exposure": state.get("gross_exposure"),
            "nav": state.get("nav"), "names": names, "blotter": blotter, "recent_fills": recent,
            "chase": chase, "rotation": rotation, "manual": manual}


# Arrival-mid lookups are immutable once an order has filled, so cache them per (symbol, submit
# time) — a panel refresh shouldn't re-hit the data API for the same fills.
_ARRIVAL_MID_CACHE: dict = {}


def _arrival_mid(client, symbol, submitted_at):
    """The arrival price prevailing at order submission — what a fill's shortfall is judged against.

    A **spread-guarded** reference (see :func:`data.arrival_reference`): the NBBO mid when the
    quote at/after ``submitted_at`` is two-sided and tight, else the last trade near then. Guarding
    on the trade matters for thin names whose quote has a stale/phantom side (INBX's $108.87 ask
    while it traded ~$95) — the raw mid would still be badly off. ``None`` if nothing resolves.
    Cached so repeated slippage-panel refreshes don't re-query the data API."""
    if not symbol or not submitted_at:
        return None
    key = (symbol, str(submitted_at))
    if key in _ARRIVAL_MID_CACHE:
        return _ARRIVAL_MID_CACHE[key]
    ref = None
    try:
        start = _as_aware(datetime.fromisoformat(str(submitted_at)))
        quotes = client.historical_quotes(symbol, start=start.isoformat(),
                                           end=(start + timedelta(seconds=60)).isoformat(), limit=1)
        bid = quotes[0].get("bid_px") if quotes else None
        ask = quotes[0].get("ask_px") if quotes else None
        trade = _closest_trade(client, symbol, start)   # thin names may not print within 60s
        ref = data.arrival_reference(bid, ask, trade)
    except Exception as exc:  # noqa: BLE001 — a quote miss just drops that order from slippage
        log.warning("arrival-mid lookup failed for %s @ %s: %s", symbol, submitted_at, exc)
    _ARRIVAL_MID_CACHE[key] = ref
    return ref


def _as_aware(dt: datetime) -> datetime:
    """Coerce a datetime to UTC-aware (Alpaca timestamps are aware; stored ones may be naive)."""
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt


def _closest_trade(client, symbol, at: datetime, *, window_s: int = 180):
    """The trade price nearest ``at`` within ±``window_s`` — the prevailing print at submit.

    A wider window than the quote so thin names (which may not trade for minutes) still yield a
    real executed price for the spread guard to fall back on. ``None`` if none resolve."""
    try:
        trades = client.historical_trades(
            symbol, start=(at - timedelta(seconds=window_s)).isoformat(),
            end=(at + timedelta(seconds=window_s)).isoformat(), limit=500)
    except Exception as exc:  # noqa: BLE001 — trades are the fallback; a miss just leaves the mid
        log.warning("arrival-trade lookup failed for %s @ %s: %s", symbol, at, exc)
        return None
    best, best_dt = None, None
    for t in trades or []:
        px, tm = t.get("price"), t.get("time")
        if not px or not tm:
            continue
        try:
            gap = abs((_as_aware(datetime.fromisoformat(str(tm))) - at).total_seconds())
        except ValueError:
            continue
        if best_dt is None or gap < best_dt:
            best, best_dt = px, gap
    return best


def _live_slippage(client, limit: int = 200) -> dict:
    """Execution quality from live Alpaca fills. Includes market orders and trades placed directly
    on Alpaca (priced vs the arrival NBBO mid) — not just the engine's limit orders."""
    orders = client.get_orders(status="all", limit=limit)
    filled = [o for o in orders if str(o.get("status")).lower() == "filled"]
    return data.slippage_from_orders(filled, lambda s, t: _arrival_mid(client, s, t))


def _live_fees(client, page_size: int = 100) -> dict:
    """Regulatory / broker fees (CAT, TAF, SEC, …) from live Alpaca activities. 100 = Alpaca's
    per-page max; the recent window is plenty for the dashboard's cost view."""
    return data.fees_from_activities(client.account_activities("FEE", page_size=page_size))


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


# Whether today (ET) has a NYSE session at all — memoized per day. This is what lets the
# session panel say "market holiday" instead of drawing intraday progress on July 3rd.
_CAL_TODAY: dict = {"date": None, "has_session": None}


def _session_today(client) -> bool | None:
    try:
        from zoneinfo import ZoneInfo
        d = datetime.now(ZoneInfo("America/New_York")).date().isoformat()
        if _CAL_TODAY["date"] != d:
            _CAL_TODAY.update(date=d, has_session=bool(client.market_calendar(d, d)))
        return _CAL_TODAY["has_session"]
    except Exception:  # noqa: BLE001 — calendar read failure → unknown, panel falls back
        return None


def _market_status(client) -> dict:
    """Live market open/closed + next session from Alpaca's clock (operational tile)."""
    c = client.market_clock()
    return {"is_open": bool(c.get("is_open")), "next_open": c.get("next_open"),
            "next_close": c.get("next_close"), "timestamp": c.get("timestamp"),
            "session_today": _session_today(client)}


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
    if settings is None:  # the exec console's preview needs the real settings object
        with contextlib.suppress(Exception):
            from engine import config as _config
            settings = _config.load_settings()
    # The live layer (background monitor + live Alpaca orders) is on by default; pass live=False
    # for the original Postgres-only behaviour. A client may be injected (tests); else built.
    if live and client is None:
        client = _build_client()
    if not live:
        client = None

    # Live market-data feed → sub-second headline metrics (NAV/prices), overlaid on /api/state.
    # If the stream can't start it self-reconnects and /api/state simply serves the snapshot.
    price_feed = None
    if client is not None:
        try:
            from engine import config
            from engine.price_feed import LivePriceFeed
            price_feed = LivePriceFeed(stream_factory=config.get_stock_data_stream,
                                       symbols=_held_equities(db_engine))
        except Exception as exc:  # noqa: BLE001
            log.warning("live price feed unavailable; headline metrics fall back to the snapshot: %s", exc)

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI):
        task = None
        if price_feed is not None:
            price_feed.start()
        if client is not None:
            task = asyncio.create_task(_monitor_loop(client, db_engine, monitor_interval, price_feed))
        try:
            yield
        finally:
            if task is not None:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
            if price_feed is not None:
                price_feed.stop()

    app = FastAPI(title="SEPI · Systematic Equity Premium Income", lifespan=lifespan)
    # Compress responses ≥1KB (audit W4): the index page alone shipped 185KB raw — the app
    # served no compression and neither did Caddy (now it does too; belt and braces for
    # tunnel/local access that bypasses Caddy).
    from fastapi.middleware.gzip import GZipMiddleware
    app.add_middleware(GZipMiddleware, minimum_size=1024)
    if _STATIC.exists():
        app.mount("/static", StaticFiles(directory=str(_STATIC)), name="static")

    @app.get("/", response_class=HTMLResponse)
    def index() -> str:
        return _INDEX.read_text() if _INDEX.exists() else "<h1>SEPI · Systematic Equity Premium Income</h1>"

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
        s = data.api_state(db_engine)
        if price_feed is not None:                         # sub-second overlay of live trade prices + day %
            s = data.apply_live_prices(s, price_feed.snapshot(), price_feed.prev_close())
        return s

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

    def _target_leverage_now() -> float:
        """The book's standing gross target — override-aware, read per request so a leverage
        action from the console shows up immediately (the old startup constant lied)."""
        fallback = float(meta.get("target_leverage", meta.get("leverage_cap", 2.0)))
        try:
            from engine import overrides as _ov
            if settings is not None:
                return float(_ov.effective_target_leverage(db_engine, settings, default=fallback))
        except Exception:  # noqa: BLE001
            pass
        return fallback

    @app.get("/api/execution")
    def execution() -> dict:
        """Live trading-session progress for the auto-surfacing execution visualizer.

        ``active`` is False (panel hidden) in Postgres-only mode or when nothing is working.
        """
        if client is None:
            return {"active": False}
        return _execution_status(client, db_engine, _target_leverage_now())

    # ------------------------------------------------------------------ #
    # Execution console (POSTs are token-gated; see _exec_gate)
    # ------------------------------------------------------------------ #
    def _exec_status() -> dict:
        proc = _MANUAL["proc"]
        running = proc is not None and proc.poll() is None
        out = {"running": running, "action": _MANUAL["action"], "mode": _MANUAL["mode"],
               "params": _MANUAL["params"], "cycle_key": _MANUAL["cycle_key"],
               "started_at": _MANUAL["started_at"], "log": _EXEC_LOG,
               "returncode": None if (proc is None or running) else proc.returncode,
               "token_configured": bool(os.environ.get("SEPI_EXEC_TOKEN"))}
        try:
            from engine import overrides as _ov
            ov = _ov.info(db_engine, "target_leverage")
            out["leverage_override"] = ov["value"] if ov else None
            out["leverage_override_since"] = ov["updated_at"] if ov else None
            out["settings_target_leverage"] = float(getattr(
                getattr(settings, "portfolio", None), "target_leverage", 2.0)) if settings else None
        except Exception:  # noqa: BLE001
            out["leverage_override"] = out["leverage_override_since"] = None
            out["settings_target_leverage"] = None
        if client is not None:  # margin headroom for the console (spread collateral competes)
            try:
                acct = client.account()
                for k in ("buying_power", "maintenance_margin", "cash"):
                    v = acct.get(k)
                    out[k] = float(v) if v is not None else None
            except Exception:  # noqa: BLE001
                pass
        if not running and proc is not None:
            out["outcome"] = _last_outcome()
        return out

    @app.get("/api/exec/status")
    def exec_status() -> dict:
        return _exec_status()

    @app.get("/api/exec/preview")
    def exec_preview(action: str, pct: float | None = None, symbol: str | None = None,
                     side: str | None = None, usd: float | None = None,
                     target: float | None = None) -> dict:
        """The confirm modal's order plan — computed in-process, nothing is sent."""
        if client is None:
            return {"error": "dashboard is Postgres-only (no broker client)"}
        if action == "rebalance":
            return {"action": "rebalance", "orders": None, "warnings": [
                "targets are computed by the engine at run time (ingest → optimize) — "
                "no pre-trade preview; the run panel shows the plan as it executes"]}
        if action not in _EXEC_ACTIONS:
            return {"error": f"unknown action {action!r}"}
        try:
            from engine import manual_exec
            params = {k: v for k, v in (("pct", pct), ("symbol", symbol), ("side", side),
                                        ("usd", usd), ("target", target)) if v is not None}
            return manual_exec.build_plan(action, client, db_engine, settings, **params)
        except (ValueError, RuntimeError, KeyError) as exc:
            return {"error": str(exc)}

    @app.post("/api/exec/run")
    def exec_run(action: str, mode: str = "normal", pct: float | None = None,
                 symbol: str | None = None, side: str | None = None, usd: float | None = None,
                 target: float | None = None,
                 x_exec_token: str | None = Header(default=None)) -> dict:
        """Start one manual action (or the rebalance) out-of-process. Token-gated."""
        err = _exec_gate(x_exec_token)
        if err:
            return {"started": False, **err}
        if action not in _EXEC_ACTIONS:
            return {"started": False, "error": f"unknown action {action!r}"}
        if mode not in ("normal", "express"):
            return {"started": False, "error": f"unknown mode {mode!r}"}
        if client is None:
            return {"started": False, "error": "dashboard is Postgres-only (no broker client)"}
        if mode == "normal":                # express trades regardless — off-hours orders queue
            err = _market_gate(client)
            if err:
                return {"started": False, **err}
        proc = _MANUAL["proc"]
        if proc is not None and proc.poll() is None:
            return {"started": False, "error": f"a manual {_MANUAL['action']} is already running"}
        try:  # refuse while an engine cycle is already working orders (theirs, not ours)
            working = any(str(o.get("status") or "").lower() in _OPEN_ORDER_STATES
                          for o in _cached_orders(client))
        except Exception:  # noqa: BLE001
            working = False
        if working:
            return {"started": False, "error": "orders are already working — wait for the "
                                               "current cycle or cancel-all first"}
        params = {k: v for k, v in (("pct", pct), ("symbol", symbol), ("side", side),
                                    ("usd", usd), ("target", target)) if v is not None}
        cycle_key = f"manual-{action}-{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}"
        _MANUAL.update(proc=_spawn_manual(action, mode, params, env, cycle_key),
                       action=action, mode=mode, params=params, cycle_key=cycle_key,
                       started_at=datetime.now(timezone.utc).isoformat())
        log.warning("manual %s (%s) started from the dashboard (pid=%s, params=%s)",
                    action, mode, _MANUAL["proc"].pid, params)
        return {"started": True, "action": action, "mode": mode, "cycle_key": cycle_key,
                "pid": _MANUAL["proc"].pid}

    @app.post("/api/exec/cancel_all")
    def exec_cancel_all(x_exec_token: str | None = Header(default=None)) -> dict:
        """Cancel every working order (the chase-misbehaving big red button). Token-gated."""
        err = _exec_gate(x_exec_token)
        if err:
            return err
        try:
            from engine.broker import Broker
            from engine.config import require_env
            n = Broker(require_env("ALPACA_API_KEY"),
                       require_env("ALPACA_SECRET_KEY")).cancel_all_orders()
            log.warning("cancel-all from the dashboard: %s order(s)", n)
            return {"cancelled": n}
        except Exception as exc:  # noqa: BLE001
            return {"error": str(exc)}

    @app.post("/api/exec/clear_override")
    def exec_clear_override(x_exec_token: str | None = Header(default=None)) -> dict:
        """Clear the sticky leverage override (rebalances return to settings.yaml). Token-gated."""
        err = _exec_gate(x_exec_token)
        if err:
            return err
        from engine import overrides as _ov
        _ov.clear(db_engine, "target_leverage")
        return {"cleared": True}

    @app.get("/api/manual_actions")
    def manual_actions_history(limit: int = 20) -> list:
        return data.api_manual_actions(db_engine, limit)

    @app.get("/api/premium_ledger")
    def premium_ledger() -> dict:
        return data.api_premium_ledger(db_engine)

    @app.get("/api/tca")
    def tca() -> dict:
        return data.api_tca(db_engine)

    @app.get("/api/config")
    def config_snapshot() -> dict:
        """Read-only live parameter snapshot — which wing/delta/coverage/leverage the engine
        runs RIGHT NOW, visible without SSH. Values only; no secrets live in settings."""
        if settings is None:
            return {"available": False}

        def grab(section: str, keys: list[str]) -> dict:
            s = getattr(settings, section, None)
            return {k: getattr(s, k) for k in keys if getattr(s, k, None) is not None}

        return {"available": True,
                "portfolio": grab("portfolio", ["target_leverage", "max_leverage",
                                                "min_position_pct", "max_sector_pct",
                                                "max_single_name_pct"]),
                "covered_calls": grab("covered_calls", ["overlay_mode", "overwrite_coverage",
                                                        "target_delta", "wing_delta",
                                                        "min_dte_entry", "max_dte_entry",
                                                        "min_bid_frac", "iv_window"]),
                "execution": grab("execution", ["min_trade_usd", "marketable_limit_bps",
                                                "equity_repeg_s", "close_buffer_s",
                                                "ladder_steps", "rebalance_hour_et"])}

    # Today's booked FEE activities, cached 5 min (they land next-morning; no need to hammer).
    _ATTR_FEES = {"ts": 0.0, "total": 0.0}

    @app.get("/api/attribution")
    def attribution() -> dict:
        """The waterfall's real inputs: option premium and fees actually booked today (ET).

        Price P&L is derived client-side as the residual (day P&L − premium − costs), so the
        three bars always sum to the day move. Fees are booked by Alpaca the morning after
        the trade, so Costs is usually yesterday's trading being paid for.
        """
        premium = data.api_premium_today(db_engine)
        if client is not None and time.time() - _ATTR_FEES["ts"] > 300:
            try:
                from zoneinfo import ZoneInfo
                today_et = datetime.now(ZoneInfo("America/New_York")).date().isoformat()
                fees = data.fees_from_activities(
                    client.account_activities("FEE", date=today_et))
                _ATTR_FEES.update(ts=time.time(), total=float(fees.get("total_usd") or 0.0))
            except Exception as exc:  # noqa: BLE001 — decoration; degrade to zero
                log.warning("attribution fees read failed", extra={"error": str(exc)})
                _ATTR_FEES.update(ts=time.time(), total=0.0)
        return {"premium_today": premium, "costs_today": round(_ATTR_FEES["total"], 2)}

    @app.get("/api/calls")
    def calls() -> list:
        return data.api_calls(db_engine)

    @app.get("/api/overlay")
    def overlay() -> dict:
        """Live state of the SPY beta-overwrite spread overlay for the Overview panel.

        DB layer supplies the contract terms; here we add the live SPY spot, the book's gross
        equity, and the derived ``beta_overwritten`` = short-leg notional ÷ gross equity — which,
        by the beta-sizing (N ≈ coverage·β_p·gross / 100·spot), reads back the fraction of the
        book's market beta the overlay is currently selling against.
        """
        market = meta.get("beta_market", "SPY")
        ov = data.api_overlay(db_engine, market=market)
        ov["mode"] = meta.get("overlay_mode", "index")
        ov["coverage_target"] = meta.get("overwrite_coverage", 1.0)
        if not ov.get("active"):
            return ov
        gross = data.api_state(db_engine).get("gross_exposure")
        ov["gross_equity"] = gross
        spot = None
        if client is not None:
            try:
                spot = client.latest_trade(market)
            except Exception:  # noqa: BLE001 — spot is enrichment; the panel degrades gracefully
                try:
                    spot = client.latest_quote(market)
                except Exception:  # noqa: BLE001
                    spot = None
        ov["spot"] = round(spot, 2) if spot else None
        if spot and gross and ov.get("contracts"):
            ov["beta_overwritten"] = round(ov["contracts"] * 100 * spot / gross, 3)
        return ov

    @app.get("/api/factors")
    def factors() -> list:
        return data.api_factors(db_engine)

    @app.get("/api/alerts")
    def alerts(limit: int = 300) -> list:                  # a browsable record, not just the last few
        return data.api_alerts(db_engine, limit)

    @app.get("/api/track_record")
    def track_record(start: str | None = None) -> dict:
        """Realized paper performance + SPY/covered-call-ETF benchmarks.

        ``start`` (ISO date) windows the curve and re-bases everything — including the
        benchmarks, which align to the (now windowed) ``dates`` and base at ``inception``.
        """
        tr = data.api_track_record(db_engine, start=start)
        if not tr.get("available"):
            return {**tr, "benchmarks": {}}
        benchmarks = _benchmark_curves(meta.get("live_benchmarks", []), tr["inception"], tr["dates"])
        return {**tr, "benchmarks": benchmarks}

    @app.get("/api/slippage")
    def slippage() -> dict:
        # Live Alpaca fills (incl. market + manually-placed trades, priced vs the arrival mid)
        # when a broker client is available; fall back to the engine's Postgres limit orders.
        if client is not None:
            try:
                return _live_slippage(client)
            except Exception as exc:  # noqa: BLE001 — degrade to Postgres, never break the panel
                log.warning("live slippage read failed, falling back to Postgres: %s", exc)
        return data.api_slippage(db_engine)

    @app.get("/api/fees")
    def fees() -> dict:
        # Regulatory / broker fees from live Alpaca activities (CAT, TAF, SEC, …). No Postgres
        # source, so the no-client fallback is simply empty.
        if client is not None:
            try:
                return _live_fees(client)
            except Exception as exc:  # noqa: BLE001 — never break the panel on a fees read
                log.warning("live fees read failed: %s", exc)
        return data.api_fees(db_engine)

    @app.get("/api/quotes")
    def quotes() -> dict:
        """Per-symbol quote detail (day OHLCV, prior close, 52-week range) for the ticker hover-card.
        Live-only + cached; empty when no client. The front-end overlays the live price."""
        if client is None:
            return {"quotes": {}}
        return {"quotes": _quotes_map(client, _events_symbols(db_engine))}

    @app.get("/api/price_history")
    def price_history() -> dict:
        """Recent intraday price path per held name + call underlying, to seed the row sparklines.

        Live-only (Alpaca bars). The sparklines otherwise start empty and grow one point per live
        tick, so on the sparse IEX feed many rows never reach two points and read as "dead". Seeding
        with real bars gives every row a proper window that live ticks then extend. No client (or a
        read failure) → empty, and the front-end just keeps its tick-built buffer.
        """
        if client is None:
            return {"available": False, "history": {}}
        try:
            underlyings = [c.get("underlying") for c in data.api_calls(db_engine) if c.get("underlying")]
            syms = sorted({*_held_equities(db_engine), *underlyings})
            if not syms:
                return {"available": True, "history": {}, "timeframe": "1Hour"}
            end = datetime.now(timezone.utc)
            start = end - timedelta(days=8)                       # ~7 sessions of hourly bars
            bars = client.bars_multi(syms, start.isoformat(), end.isoformat(), timeframe="1Hour")
            hist = {sym: [b["close"] for b in rows if b.get("close") is not None][-80:]
                    for sym, rows in bars.items()}
            return {"available": True, "history": hist, "timeframe": "1Hour"}
        except Exception as exc:  # noqa: BLE001 — sparklines are cosmetic; never break the page
            log.warning("price history read failed: %s", exc)
            return {"available": False, "history": {}}

    @app.get("/api/events")
    def events() -> dict:
        """Upcoming calendar for the book: option expiries, the next rebalance, and (best-effort,
        cached) earnings dates for held names. Sorted soonest-first, windowed to ~120 days."""
        today = date.today()

        def _iso(v):
            try:
                return date.fromisoformat(str(v)[:10])
            except (ValueError, TypeError):
                return None

        out: list[dict] = []
        for c in data.api_calls(db_engine):                       # option expiries
            d = _iso(c.get("expiration"))
            if d:
                n = int(c.get("contracts") or 0)
                out.append({"date": str(d), "days_until": (d - today).days, "type": "expiry",
                            "symbol": c.get("underlying"),
                            "detail": f"{n} call{'' if n == 1 else 's'} @ {c.get('strike')}"})
        nr = (data.api_health(db_engine) or {}).get("next_rebalance") or {}   # next rebalance
        d = _iso(nr.get("date"))
        if d:
            out.append({"date": str(d), "days_until": (d - today).days, "type": "rebalance",
                        "symbol": None, "detail": f"monthly rebalance ({nr.get('source', 'estimated')})"})
        earn, loading = _earnings_map(_events_symbols(db_engine))             # earnings (nearest upcoming per name)
        for sym, dates in earn.items():
            ups = sorted(x for x in (_iso(ds) for ds in dates) if x and x >= today)
            if ups:
                d0 = ups[0]
                out.append({"date": str(d0), "days_until": (d0 - today).days, "type": "earnings",
                            "symbol": sym, "detail": "earnings report"})
        out = [e for e in out if e["days_until"] is not None and -1 <= e["days_until"] <= 120]
        out.sort(key=lambda e: (e["date"], e["type"]))
        return {"events": out, "earnings_loading": loading, "as_of": str(today)}

    @app.get("/api/risk")
    def risk(start: str | None = None) -> dict:
        """Drawdown / volatility / VaR analytics from the equity curve (Postgres-only).

        ``start`` (ISO date) windows the curve to match the Performance start-date picker.
        Rolling realized β vs SPY is layered on when there's enough curve (needs the
        benchmark closes — cached; failure just omits the series).
        """
        r = data.api_risk(db_engine, start=start)
        if r.get("available") and r.get("days", 0) >= 12:
            try:
                from engine import benchmarks as _bm
                spy = _bm.align(_bm.closes("SPY", r["dates"][0]), r["dates"])
                r["rolling_beta"] = data.rolling_beta(r["nav"], spy)
            except Exception as exc:  # noqa: BLE001
                log.warning("rolling beta unavailable: %s", exc)
        return r

    @app.get("/api/risk_contrib")
    def risk_contrib() -> dict:
        """Per-name risk decomposition from the latest rebalance (engine-persisted, Postgres-only)."""
        return data.api_risk_contributions(db_engine)

    @app.get("/api/correlation")
    def correlation() -> dict:
        """Trailing-60d return correlation across the top holdings (from daily bars, cached).

        Needs the market-data client for bars; without it (Postgres-only mode) the matrix is
        unavailable and the Risk view shows a dashed placeholder.
        """
        if client is None:
            return {"available": False, "symbols": [], "matrix": []}
        return _correlation_map(client, db_engine)

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
