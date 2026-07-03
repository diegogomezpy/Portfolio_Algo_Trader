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
            "/api/nav_history", "/api/orders", "/api/calls", "/api/overlay", "/api/factors",
            "/api/alerts", "/api/health", "/api/reference", "/api/risk", "/api/execution",
            "/api/exec/status", "/api/exec/preview", "/api/exec/run", "/api/exec/cancel_all",
            "/api/exec/clear_override", "/api/manual_actions"} <= paths
    # the shared theme is mounted so both tabs reference one design system
    assert any(getattr(r, "path", "") == "/static" for r in app.routes)


def test_index_route_serves_html():
    eng = create_engine("sqlite://")
    db.create_all(eng)
    app = create_app(eng, live=False)
    # call the index route handler directly (no server): since the W5 split it is a thin
    # shell — markup + links to the cacheable static bundle (app.js / css / fonts).
    html = _route(app, "/")()
    assert "SEPI" in html                                   # brand lockup markup
    assert "/static/theme.css" in html                      # shared dark design system
    assert "/static/dashboard.css" in html and "/static/app.js" in html
    assert "/static/fonts.css" in html and "googleapis" not in html   # fonts self-hosted (W6)


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


def test_execution_route_rotation_excludes_options_and_flags_active():
    # /api/execution aggregates filled equity orders into the cash-rotation flow (Phase 3): buys
    # = deployed, sells = raised, the SPY option fill excluded. A still-open order flags active.
    eng = create_engine("sqlite://")
    db.create_all(eng)
    with eng.begin() as c:
        c.execute(insert(db.snapshots).values(
            ts=datetime(2026, 7, 2, 16), nav=100_000.0, cash=0.0, last_equity=100_000.0,
            weights={"AAPL": 0.2}, positions={"AAPL": 100}, drift=0.0))
    fake = _FakeClient([
        {"symbol": "AAPL", "side": "buy", "qty": 100, "filled_qty": 100, "type": "limit",
         "status": "filled", "filled_avg_price": 200.0, "submitted_at": "2026-07-02T13:31:00"},
        {"symbol": "XOM", "side": "sell", "qty": 50, "filled_qty": 50, "type": "limit",
         "status": "filled", "filled_avg_price": 100.0, "submitted_at": "2026-07-02T13:31:00"},
        {"symbol": "SPY260731C00764000", "side": "sell", "qty": 7, "filled_qty": 7, "type": "limit",
         "status": "filled", "filled_avg_price": 2.34, "submitted_at": "2026-07-02T13:31:00"},
        {"symbol": "JPM", "side": "buy", "qty": 40, "filled_qty": 0, "type": "limit",
         "status": "new", "filled_avg_price": None, "submitted_at": "2026-07-02T13:31:00"},
    ])
    ex = _route(create_app(eng, client=fake, live=True), "/api/execution")()
    assert ex["active"] is True                              # the open JPM order → a working cycle
    rot = ex["rotation"]
    assert rot["deployed"] == 20000.0 and rot["raised"] == 5000.0   # AAPL 100×200 ; XOM 50×100
    assert [b["symbol"] for b in rot["buys"]] == ["AAPL"]
    assert [s["symbol"] for s in rot["sells"]] == ["XOM"]   # SPY option fill is not equity rotation
    assert ex["chase"] == []                                # no order_events seeded → empty, not error


class _Proc:  # fake child: alive until the test flips _rc
    pid = 4242
    _rc = None

    def poll(self):
        return self._rc

    @property
    def returncode(self):            # real Popen exposes it once poll() is non-None
        return self._rc


def _reset_manual(monkeypatch):
    from dashboard import app as app_module
    for k in ("proc", "action", "mode", "params", "cycle_key", "started_at"):
        monkeypatch.setitem(app_module._MANUAL, k, None)


def test_exec_run_token_gate_and_market_gate(monkeypatch):
    from dashboard import app as app_module

    eng = create_engine("sqlite://")
    db.create_all(eng)
    _reset_manual(monkeypatch)
    app = create_app(eng, client=_MarketClient([]), live=True)
    run = _route(app, "/api/exec/run")

    monkeypatch.delenv("SEPI_EXEC_TOKEN", raising=False)     # unset → console disabled
    out = run(action="liquidate", pct=10.0, mode="express", x_exec_token="whatever")
    assert out["started"] is False and out.get("disabled") is True

    monkeypatch.setenv("SEPI_EXEC_TOKEN", "sekrit")          # wrong token → unauthorized
    out = run(action="liquidate", pct=10.0, mode="express", x_exec_token="nope")
    assert out["started"] is False and out.get("unauthorized") is True

    class _Closed(_MarketClient):                            # right token, closed market
        def market_clock(self):
            return {"is_open": False, "next_open": "2026-07-06T09:30:00-04:00"}

    run2 = _route(create_app(eng, client=_Closed([]), live=True), "/api/exec/run")
    out = run2(action="liquidate", pct=10.0, mode="express", x_exec_token="sekrit")
    assert out["started"] is False and out.get("market_closed") is True
    assert "next_open" in out


def test_exec_run_starts_guards_double_start_and_reports_status(monkeypatch):
    from dashboard import app as app_module

    eng = create_engine("sqlite://")
    db.create_all(eng)
    _reset_manual(monkeypatch)
    monkeypatch.setenv("SEPI_EXEC_TOKEN", "sekrit")
    monkeypatch.setattr(app_module, "_spawn_manual",
                        lambda action, mode, params, env, cycle_key: _Proc())
    app = create_app(eng, client=_MarketClient([]), live=True)
    run, status = _route(app, "/api/exec/run"), _route(app, "/api/exec/status")

    out = run(action="liquidate", pct=25.0, mode="express", x_exec_token="sekrit")
    assert out["started"] is True and out["cycle_key"].startswith("manual-liquidate-")
    out2 = run(action="leverage", target=1.5, mode="normal", x_exec_token="sekrit")
    assert out2["started"] is False and "already running" in out2["error"]

    st = status()
    assert st["running"] is True and st["action"] == "liquidate" and st["mode"] == "express"
    assert st["params"] == {"pct": 25.0} and st["token_configured"] is True

    app_module._MANUAL["proc"]._rc = 0                        # child exits cleanly
    st = status()
    assert st["running"] is False and st["returncode"] == 0


def test_exec_run_refuses_unknown_action_and_missing_client(monkeypatch):
    eng = create_engine("sqlite://")
    db.create_all(eng)
    _reset_manual(monkeypatch)
    monkeypatch.setenv("SEPI_EXEC_TOKEN", "sekrit")
    run = _route(create_app(eng, client=_MarketClient([]), live=True), "/api/exec/run")
    assert "unknown action" in run(action="yolo", x_exec_token="sekrit")["error"]
    run_pg = _route(create_app(eng, live=False), "/api/exec/run")
    assert "Postgres-only" in run_pg(action="liquidate", pct=10.0, x_exec_token="sekrit")["error"]


def test_exec_preview_plans_in_process_without_trading():
    eng = create_engine("sqlite://")
    db.create_all(eng)

    class _PosClient(_MarketClient):
        def all_positions(self):
            return [{"symbol": "AAPL", "qty": 400, "asset_class": "us_equity"}]

        def latest_trade(self, sym):
            return 212.0

    prev = _route(create_app(eng, client=_PosClient([]), live=True), "/api/exec/preview")
    plan = prev(action="liquidate", pct=25.0)
    assert plan["orders"] == [{"symbol": "AAPL", "side": "sell", "qty": 100,
                               "est_price": 212.0, "est_notional": 21200.0}]
    assert prev(action="rebalance")["orders"] is None         # engine-computed at run time
    assert "error" in prev(action="liquidate", pct=0)         # planner ValueError surfaced
    assert "error" in prev(action="nope")


def test_exec_cancel_all_and_clear_override_are_token_gated(monkeypatch):
    eng = create_engine("sqlite://")
    db.create_all(eng)
    monkeypatch.delenv("SEPI_EXEC_TOKEN", raising=False)
    app = create_app(eng, client=_MarketClient([]), live=True)
    assert _route(app, "/api/exec/cancel_all")(x_exec_token="x").get("disabled") is True
    assert _route(app, "/api/exec/clear_override")(x_exec_token="x").get("disabled") is True

    monkeypatch.setenv("SEPI_EXEC_TOKEN", "sekrit")           # authorized clear works
    from engine import overrides
    overrides.set(eng, "target_leverage", 1.5)
    out = _route(app, "/api/exec/clear_override")(x_exec_token="sekrit")
    assert out == {"cleared": True} and overrides.get(eng, "target_leverage") is None


def test_execution_target_leverage_is_override_aware():
    # The visualizer's "→ target×" must reflect a console-set sticky override immediately,
    # not the startup constant (a leverage rebalance changes the standing parameter).
    from engine import overrides

    eng = create_engine("sqlite://")
    db.create_all(eng)
    exe = _route(create_app(eng, client=_FakeClient([]), live=True), "/api/execution")
    assert exe()["target_leverage"] > 0                      # settings-derived baseline
    overrides.set(eng, "target_leverage", 1.25)
    assert exe()["target_leverage"] == 1.25                  # override wins, per request
    st = _route(create_app(eng, client=_FakeClient([]), live=True), "/api/exec/status")()
    assert st["leverage_override"] == 1.25 and st["leverage_override_since"]
    assert st["settings_target_leverage"] is not None


def test_manual_actions_route_empty_and_execution_carries_manual(monkeypatch):
    from dashboard import app as app_module

    eng = create_engine("sqlite://")
    db.create_all(eng)
    _reset_manual(monkeypatch)
    app = create_app(eng, client=_FakeClient([]), live=True)
    assert _route(app, "/api/manual_actions")() == []
    ex = _route(app, "/api/execution")()
    assert ex["manual"] is None and ex["active"] is False

    monkeypatch.setitem(app_module._MANUAL, "proc", _Proc())   # a manual run in flight
    monkeypatch.setitem(app_module._MANUAL, "action", "liquidate")
    monkeypatch.setitem(app_module._MANUAL, "mode", "normal")
    monkeypatch.setitem(app_module._MANUAL, "params", {"pct": 25.0})
    monkeypatch.setitem(app_module._MANUAL, "cycle_key", "manual-liquidate-x")
    ex = _route(app, "/api/execution")()
    assert ex["active"] is True                                # panel surfaces immediately
    assert ex["manual"]["action"] == "liquidate" and ex["manual"]["params"] == {"pct": 25.0}


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
