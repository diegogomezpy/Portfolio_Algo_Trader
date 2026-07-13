"""Smoke test for dashboard.app — wiring (routes present) without an HTTP client.

The data layer is covered by test_dashboard_data; here we just confirm create_app builds
against an injected engine and exposes the documented routes (no httpx/TestClient needed).
"""

from __future__ import annotations

from datetime import datetime, timedelta

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

    # NORMAL needs live quotes → refused when closed; EXPRESS trades any time (orders queue)
    run2 = _route(create_app(eng, client=_Closed([]), live=True), "/api/exec/run")
    out = run2(action="liquidate", pct=10.0, mode="normal", x_exec_token="sekrit")
    assert out["started"] is False and out.get("market_closed") is True
    assert "next_open" in out
    monkeypatch.setattr(app_module, "_spawn_manual",
                        lambda action, mode, params, env, cycle_key: _Proc())
    out = run2(action="liquidate", pct=10.0, mode="express", x_exec_token="sekrit")
    assert out["started"] is True                            # express ignores the clock
    monkeypatch.setitem(app_module._MANUAL, "proc", None)    # don't leak into other tests


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


def test_market_status_session_today_flags_holidays(monkeypatch):
    # July-3rd bug: the session panel drew intraday progress on a full-holiday. The health
    # market block now says whether today has a NYSE session at all (per-day memoized).
    from dashboard import app as app_module

    eng = create_engine("sqlite://")
    db.create_all(eng)
    monkeypatch.setitem(app_module._CAL_TODAY, "date", None)      # bust the per-day memo
    h = _route(create_app(eng, client=_MarketClient([]), live=True), "/api/health")()
    assert h["market"]["session_today"] is True                   # calendar returns sessions

    class _Holiday(_MarketClient):
        def market_calendar(self, start, end):
            return []                                             # no session today

    monkeypatch.setitem(app_module._CAL_TODAY, "date", None)
    h = _route(create_app(eng, client=_Holiday([]), live=True), "/api/health")()
    assert h["market"]["session_today"] is False


def test_attribution_route_reports_todays_premium_and_fees():
    # The waterfall's Premium/Costs bars were hardcoded 0 client-side; now they read real
    # bookings: today's options_lifecycle premium + today's FEE activities.
    eng = create_engine("sqlite://")
    db.create_all(eng)
    now = datetime.utcnow()
    with eng.begin() as c:
        c.execute(insert(db.options_lifecycle).values(
            ts=now, event_type="write", underlying="SPY",
            option_symbol="SPY260731C00764000", contracts=7, premium=1638.0))
        c.execute(insert(db.options_lifecycle).values(
            ts=now - timedelta(days=3), event_type="write", underlying="SPY",
            option_symbol="SPY260703C00750000", contracts=1, premium=999.0))  # not today

    class _Fees(_FakeClient):
        def account_activities(self, activity_types, *, date=None, page_size=100):
            return [{"activity_type": "FEE", "net_amount": -1.23, "activity_sub_type": "CAT"},
                    {"activity_type": "FEE", "net_amount": -0.77, "activity_sub_type": "TAF"}]

    out = _route(create_app(eng, client=_Fees([]), live=True), "/api/attribution")()
    assert out["premium_today"] == 1638.0                         # today's row only
    assert out["costs_today"] == 2.0
    # Postgres-only degrades: premium from the DB, costs zero (no activities surface)
    out = _route(create_app(eng, live=False), "/api/attribution")()
    assert out["premium_today"] == 1638.0 and out["costs_today"] == 0.0


def test_risk_route_postgres_only():
    eng = create_engine("sqlite://")
    db.create_all(eng)
    with eng.begin() as c:
        for i, nv in enumerate([100_000.0, 101_000.0, 99_000.0, 102_000.0]):
            c.execute(insert(db.snapshots).values(
                ts=datetime(2026, 6, 1 + i, 16), nav=nv, cash=0.0, last_equity=100_000.0,
                weights={}, positions={"AAPL": 100}, drift=0.0))
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


def test_last_outcome_surfaces_child_traceback(tmp_path, monkeypatch):
    """Regression (2026-07-06): a rebalance child that CRASHED (traceback, no result line)
    showed as silence in the UI. The outcome parser must surface the exception."""
    from dashboard import app as dapp
    log = tmp_path / "exec.log"
    log.write_text("===== 2026-07-06 rebalance normal {} =====\n"
                   "some ingest noise\n"
                   "Traceback (most recent call last):\n"
                   '  File "scripts/run_eod.py", line 880, in main\n'
                   "StopIteration\n")
    monkeypatch.setattr(dapp, "_EXEC_LOG", str(log))
    out = dapp._last_outcome()
    assert out and "crashed" in out.get("error", "") and "StopIteration" in out["error"]


def test_last_outcome_prefers_cycle_line_over_old_traceback(tmp_path, monkeypatch):
    from dashboard import app as dapp
    log = tmp_path / "exec.log"
    log.write_text("Traceback (most recent call last):\nStopIteration\n"
                   "===== retry =====\nCycle 2026-07-06 → executed\n")
    monkeypatch.setattr(dapp, "_EXEC_LOG", str(log))
    assert dapp._last_outcome() == {"summary": "Cycle 2026-07-06 → executed"}


def test_exec_express_finish_arms_flag_and_status_reports_pending(monkeypatch):
    """The express-finish button: token-gated POST arms the override; status shows it pending;
    the engine-side clear (a stage consuming it) drops it back to False."""
    from engine import execute as _ex, overrides as _ov
    eng = create_engine("sqlite://")
    db.create_all(eng)
    _reset_manual(monkeypatch)
    app = create_app(eng, client=_MarketClient([]), live=True)
    xf = _route(app, "/api/exec/express_finish")
    status = _route(app, "/api/exec/status")

    monkeypatch.delenv("SEPI_EXEC_TOKEN", raising=False)      # unset → console disabled
    assert xf(x_exec_token="whatever").get("armed") is not True

    monkeypatch.setenv("SEPI_EXEC_TOKEN", "sekrit")
    assert xf(x_exec_token="nope").get("armed") is not True   # wrong token
    out = xf(x_exec_token="sekrit")
    assert out == {"armed": True}
    assert status()["express_finish_pending"] is True
    assert _ov.get(eng, "express_finish") is not None

    _ex.clear_express_finish(eng)                             # a stage consumed it
    assert status()["express_finish_pending"] is False


# ====================================================================== #
# Accounts — dashboard-managed encrypted credential store (ADR-001 Phase C)
# ====================================================================== #
def test_accounts_add_is_token_gated_then_stores_masked(monkeypatch):
    from cryptography.fernet import Fernet
    monkeypatch.setenv("SEPI_CRED_KEK", Fernet.generate_key().decode())
    eng = create_engine("sqlite://")
    db.create_all(eng)
    app = create_app(eng, client=_MarketClient([]), live=True)
    add = _route(app, "/api/accounts/add")
    lst = _route(app, "/api/accounts")

    monkeypatch.delenv("SEPI_EXEC_TOKEN", raising=False)          # console disabled
    assert add(body={"slug": "trend", "api_key": "PKX1", "api_secret": "s"},
               x_exec_token="whatever").get("disabled") is True

    monkeypatch.setenv("SEPI_EXEC_TOKEN", "sekrit")
    assert add(body={"slug": "trend", "api_key": "PKX1", "api_secret": "s"},
               x_exec_token="nope").get("unauthorized") is True   # wrong token

    out = add(body={"slug": "trend", "api_key": "PKLIVE9999ZZZZ", "api_secret": "s3cr3t",
                    "label": "Trend", "capital": 250000, "leverage": 1.0}, x_exec_token="sekrit")
    assert out["added"] is True and out["key_fingerprint"] == "PKL…ZZZZ"
    assert "api_secret" not in out and "api_key" not in out       # no secret echoed

    assert lst(x_exec_token="nope")[0].get("unauthorized") is True    # roster is token-gated too
    accts = lst(x_exec_token="sekrit")
    assert len(accts) == 1 and accts[0]["slug"] == "trend"
    assert accts[0]["label"] == "Trend" and accts[0]["capital"] == 250000
    assert "api_secret" not in accts[0] and "api_key" not in accts[0]


def test_accounts_add_validates_and_remove(monkeypatch):
    from cryptography.fernet import Fernet
    monkeypatch.setenv("SEPI_CRED_KEK", Fernet.generate_key().decode())
    monkeypatch.setenv("SEPI_EXEC_TOKEN", "sekrit")
    eng = create_engine("sqlite://")
    db.create_all(eng)
    app = create_app(eng, client=_MarketClient([]), live=True)
    add = _route(app, "/api/accounts/add")
    rm = _route(app, "/api/accounts/remove")

    assert "error" in add(body={"slug": "", "api_key": "k", "api_secret": "s"}, x_exec_token="sekrit")
    add(body={"slug": "a", "api_key": "PKAAAA1111", "api_secret": "s"}, x_exec_token="sekrit")
    assert rm(body={"slug": "a"}, x_exec_token="sekrit") == {"removed": True}
    assert _route(app, "/api/accounts")(x_exec_token="sekrit") == []


def test_accounts_routes_registered():
    eng = create_engine("sqlite://")
    db.create_all(eng)
    paths = {r.path for r in create_app(eng, live=False).routes}
    assert {"/api/accounts", "/api/accounts/add", "/api/accounts/remove",
            "/api/accounts/{slug}/state"} <= paths


def test_monitor_secondary_accounts_snapshots_each_enabled_account(monkeypatch):
    """The per-account monitor sweep (tracked sleeves): each enabled credstore account gets a
    snapshot tagged with its slug, built from its own creds. A bad account is skipped, not fatal."""
    from cryptography.fernet import Fernet
    from dashboard import app as dapp, data
    from engine import credstore, alpaca_client
    monkeypatch.setenv("SEPI_CRED_KEK", Fernet.generate_key().decode())
    eng = create_engine("sqlite://")
    db.create_all(eng)
    credstore.add_account(eng, slug="trend", api_key="PKGOOD1", api_secret="s")
    credstore.add_account(eng, slug="bad", api_key="PKBAD1", api_secret="s")

    class _FakeClient:
        def __init__(self, api_key, secret_key, **kw):
            if api_key == "PKBAD1":
                raise RuntimeError("unauthorized")          # bad creds blow up on construction/use
            self._eq = 250_000.0
        def account(self):
            return {"equity": self._eq, "cash": 10_000.0, "last_equity": self._eq}
        def all_positions(self):
            return [{"symbol": "IWM", "qty": 100, "market_value": 240_000.0}]

    monkeypatch.setattr(alpaca_client, "AlpacaClient", _FakeClient)
    dapp._monitor_secondary_accounts(eng, 60)

    from sqlalchemy import select
    accts = {r[0] for r in eng.connect().execute(select(db.snapshots.c.account)).all()}
    assert "trend" in accts and "bad" not in accts          # good written, bad skipped
    assert data.api_state(eng, account="trend")["nav"] == 250_000.0


def test_signals_endpoint_lists_the_builtin_palette():
    eng = create_engine("sqlite://"); db.create_all(eng)
    app = create_app(eng, live=False)
    out = _route(app, "/api/signals")()
    names = {s["name"] for s in out}
    # The four live factors plus the finer single-metric building blocks (FB2).
    assert {"quality", "value", "low_beta", "low_vol"} <= names
    assert {"roe", "gross_margin", "earnings_yield", "book_yield", "momentum"} <= names
    assert all("needs" in s and "label" in s and "category" in s for s in out)


def test_strategy_preview_token_gated_and_wires_spec(monkeypatch):
    import asyncio
    from engine import account_runner
    eng = create_engine("sqlite://"); db.create_all(eng)
    _reset_manual(monkeypatch)
    app = create_app(eng, client=_MarketClient([]), live=True)
    preview = _route(app, "/api/strategy/preview")

    monkeypatch.delenv("SEPI_EXEC_TOKEN", raising=False)                 # unset → disabled
    out = asyncio.run(preview(body={"account": "trend", "signals": {"value": 1.0}}, x_exec_token="x"))
    assert out.get("disabled") is True

    monkeypatch.setenv("SEPI_EXEC_TOKEN", "sekrit")
    seen = {}
    def fake_run(account, strat, *, db_engine, settings, dry_run):
        seen["account"] = account
        seen["dry_run"] = dry_run
        seen["signals"] = dict(strat.spec.signals)
        seen["construction"] = strat.spec.construction
        return {"status": "dry_run", "account": account, "n_orders": 2, "orders": []}
    monkeypatch.setattr(account_runner, "run_strategy_on_account", fake_run)

    out = asyncio.run(preview(body={"account": "trend", "signals": {"quality": 0.5, "value": 0.5},
                                    "construction": "optimizer", "leverage": 1.0}, x_exec_token="sekrit"))
    assert out["status"] == "dry_run" and seen["account"] == "trend" and seen["dry_run"] is True
    assert seen["signals"] == {"quality": 0.5, "value": 0.5} and seen["construction"] == "optimizer"


def test_strategy_preview_requires_a_signal(monkeypatch):
    import asyncio
    eng = create_engine("sqlite://"); db.create_all(eng)
    _reset_manual(monkeypatch)
    monkeypatch.setenv("SEPI_EXEC_TOKEN", "sekrit")
    app = create_app(eng, client=_MarketClient([]), live=True)
    out = asyncio.run(_route(app, "/api/strategy/preview")(
        body={"account": "trend", "signals": {}}, x_exec_token="sekrit"))
    assert "error" in out and "signal" in out["error"]
