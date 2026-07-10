"""Unit tests for engine.scheduler — the autonomous per-account strategy scheduler (FB5).

Covers the cadence math (is_due) and run_scheduled's guardrails: opt-in only, market + rebalance-hour
gate, once-per-period lease, and that a transient failure leaves last_run unset so it retries.
"""

from __future__ import annotations

from datetime import date, datetime
from types import SimpleNamespace

from zoneinfo import ZoneInfo

from engine import config_strategy as CS, db, scheduler, specstore


# ------------------------------ cadence math ------------------------------ #
def test_is_due_daily():
    assert scheduler.is_due("daily", date(2026, 7, 9), None) is True
    assert scheduler.is_due("daily", date(2026, 7, 9), date(2026, 7, 9)) is False   # already today
    assert scheduler.is_due("daily", date(2026, 7, 9), date(2026, 7, 8)) is True


def test_is_due_weekly_once_per_iso_week():
    wed = date(2026, 7, 8)                                    # ISO week 28
    assert scheduler.is_due("weekly", wed, None) is True
    assert scheduler.is_due("weekly", wed, date(2026, 7, 6)) is False   # Mon same week → not again
    assert scheduler.is_due("weekly", wed, date(2026, 6, 30)) is True   # prior week → due


def test_is_due_monthly_once_per_month():
    d = date(2026, 7, 2)
    assert scheduler.is_due("monthly", d, None) is True
    assert scheduler.is_due("monthly", d, date(2026, 7, 1)) is False    # already ran in July
    assert scheduler.is_due("monthly", d, date(2026, 6, 30)) is True    # last ran in June → due


def _settings():
    return SimpleNamespace(execution=SimpleNamespace(rebalance_hour_et=13))


def _eng(tmp_path):
    return db.get_engine(f"sqlite:///{tmp_path}/sched.sqlite")


def _et(hour):
    return datetime(2026, 7, 2, hour, 0, tzinfo=ZoneInfo("America/New_York"))   # first trading day of July


class _OpenClient:
    def market_clock(self): return {"is_open": True}


class _ClosedClient:
    def market_clock(self): return {"is_open": False}


# ------------------------------ run_scheduled ------------------------------ #
def test_run_scheduled_fires_due_enabled_and_marks_run(tmp_path):
    eng = _eng(tmp_path)
    specstore.save_spec(eng, "trend", CS.StrategySpec(name="A", signals={"low_vol": 1.0}),
                        rebalance_frequency="monthly", auto_enabled=True)
    calls = []
    def runner(acct, strat, **kw): calls.append(acct); return {"status": "executed", "account": acct}
    out = scheduler.run_scheduled(eng, _settings(), client=_OpenClient(), runner=runner, now=_et(13))
    assert calls == ["trend"] and out[0]["status"] == "executed"
    assert specstore.get_spec(eng, "trend")["last_run"] == "2026-07-02"     # lease stamped
    # second pass same day → not due again (lease holds)
    calls.clear()
    scheduler.run_scheduled(eng, _settings(), client=_OpenClient(), runner=runner, now=_et(14))
    assert calls == []


def test_run_scheduled_skips_when_not_enabled_or_before_hour_or_closed(tmp_path):
    eng = _eng(tmp_path)
    specstore.save_spec(eng, "trend", CS.StrategySpec(name="A", signals={"low_vol": 1.0}),
                        rebalance_frequency="daily", auto_enabled=False)      # opt-in OFF
    def runner(acct, strat, **kw): raise AssertionError("should not run")
    scheduler.run_scheduled(eng, _settings(), client=_OpenClient(), runner=runner, now=_et(13))
    # enable it, but before the rebalance hour → still skipped
    specstore.set_auto_enabled(eng, "trend", True)
    scheduler.run_scheduled(eng, _settings(), client=_OpenClient(), runner=runner, now=_et(11))
    # market closed → skipped even after the hour
    scheduler.run_scheduled(eng, _settings(), client=_ClosedClient(), runner=runner, now=_et(14))
    assert specstore.get_spec(eng, "trend")["last_run"] is None             # never fired


def test_run_scheduled_leaves_lease_unset_on_failure(tmp_path):
    eng = _eng(tmp_path)
    specstore.save_spec(eng, "trend", CS.StrategySpec(name="A", signals={"low_vol": 1.0}),
                        rebalance_frequency="daily", auto_enabled=True)
    def boom(acct, strat, **kw): raise RuntimeError("alpaca down")
    alerts = []
    scheduler.run_scheduled(eng, _settings(), client=_OpenClient(), runner=boom,
                            alert=alerts.append, now=_et(13))
    assert specstore.get_spec(eng, "trend")["last_run"] is None             # retries next pass
    assert any("FAILED" in m for m in alerts)
