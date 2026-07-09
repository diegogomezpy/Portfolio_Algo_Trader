"""Unit tests for engine.account_runner — running a strategy on ONE non-primary account (CS-2).

Tests the runner's orchestration + isolation (its own client/broker from the account's creds,
account-tagged snapshot, primary refused), stubbing the already-tested execution engine.
"""

from __future__ import annotations

from datetime import date
from types import SimpleNamespace

import pandas as pd
import pytest
from cryptography.fernet import Fernet
from sqlalchemy import create_engine

from engine import account_runner as AR
from engine import db, credstore, risk
from engine.strategy import PlanInputs, TargetBook


@pytest.fixture(autouse=True)
def _kek(monkeypatch):
    monkeypatch.setenv("SEPI_CRED_KEK", Fernet.generate_key().decode())


def _eng():
    e = create_engine("sqlite://"); db.create_all(e); return e


def _settings():
    return SimpleNamespace(
        execution=SimpleNamespace(min_trade_usd=100, large_cap_adv_threshold=1e9,
                                  mid_cap_adv_threshold=5e6, spread_threshold=0.001,
                                  marketable_limit_bps=10, max_spread_bps=150, ladder_steps=3,
                                  child_adv_pct=0.1, stale_trade_max_s=900),
        portfolio=SimpleNamespace(max_leverage=2.0))


class _FakeClient:
    def __init__(self, creds=None, equity=1_000_000.0, positions=None):
        self.creds = creds
        self._eq = equity
        self._pos = positions or []
    def account(self): return {"equity": self._eq, "cash": self._eq, "last_equity": self._eq}
    def all_positions(self): return list(self._pos)
    def market_clock(self): return {"next_close": None, "is_open": True}


class _FakeBroker:
    def __init__(self, creds=None): self.creds = creds


class _Strat:
    name = "cfg_test"
    spec = SimpleNamespace(leverage=1.0)
    def __init__(self, weights=None):
        self._w = weights if weights is not None else pd.Series({"AAA": 0.5, "BBB": 0.5})
    def generate(self, ctx, as_of):
        return TargetBook(weights=self._w,
                          inputs=PlanInputs(prices={"AAA": 100.0, "BBB": 200.0},
                                            universe={"AAA", "BBB"}))


def _patch_engine(monkeypatch, *, approved=True):
    """Stub the (separately-tested) execution + risk + snapshot so we test the runner's wiring."""
    calls = {}
    monkeypatch.setattr(risk, "check_pretrade",
                        lambda *a, **k: SimpleNamespace(approved=approved, reason="capped" if not approved else ""))
    def fake_submit(orders, *, broker, db_engine, cycle_key, **kw):
        calls["submit"] = {"broker": broker, "cycle_key": cycle_key, "n": len(orders)}
        return SimpleNamespace(submitted=len(orders), filled=len(orders), partial=0,
                               rejected=0, deferred=0, lines=[])
    monkeypatch.setattr(AR, "submit_and_track", fake_submit)
    def fake_monitor(client, db_engine, *, account="primary", **kw):
        calls["monitor_account"] = account
    monkeypatch.setattr(AR.monitor, "monitor_once", fake_monitor)
    return calls


def test_refuses_primary_account(monkeypatch):
    with pytest.raises(ValueError, match="NON-primary"):
        AR.run_strategy_on_account("primary", _Strat(), db_engine=_eng(), settings=_settings())


def test_unknown_account_raises(monkeypatch):
    with pytest.raises(KeyError):
        AR.run_strategy_on_account("ghost", _Strat(), db_engine=_eng(), settings=_settings())


def test_runs_isolated_on_the_accounts_own_broker(monkeypatch):
    calls = _patch_engine(monkeypatch)
    eng = _eng()
    credstore.add_account(eng, slug="trend", api_key="PKTREND", api_secret="s")
    seen = {}
    def mk_client(creds): seen["client_creds"] = creds; return _FakeClient(creds)
    def mk_broker(creds): seen["broker_creds"] = creds; return _FakeBroker(creds)

    res = AR.run_strategy_on_account("trend", _Strat(), db_engine=eng, settings=_settings(),
                                     as_of=date(2026, 7, 9), make_client=mk_client, make_broker=mk_broker)
    assert res["status"] == "executed" and res["account"] == "trend"
    # Isolation: client + broker were built from the TREND account's creds, not env/primary.
    assert seen["client_creds"]["api_key"] == "PKTREND"
    assert seen["broker_creds"]["api_key"] == "PKTREND"
    # Orders went to that account's broker, under an account-namespaced cycle key.
    assert isinstance(calls["submit"]["broker"], _FakeBroker)
    assert calls["submit"]["cycle_key"] == "acct:trend:2026-07-09"
    # Snapshot tagged with the sleeve.
    assert calls["monitor_account"] == "trend"


def test_risk_block_places_no_orders(monkeypatch):
    calls = _patch_engine(monkeypatch, approved=False)
    eng = _eng()
    credstore.add_account(eng, slug="trend", api_key="PKTREND", api_secret="s")
    res = AR.run_strategy_on_account("trend", _Strat(), db_engine=eng, settings=_settings(),
                                     make_client=lambda c: _FakeClient(c), make_broker=lambda c: _FakeBroker(c))
    assert res["status"] == "blocked_risk" and "submit" not in calls


def test_no_targets_short_circuits(monkeypatch):
    _patch_engine(monkeypatch)
    eng = _eng()
    credstore.add_account(eng, slug="trend", api_key="PKTREND", api_secret="s")
    res = AR.run_strategy_on_account("trend", _Strat(weights=pd.Series(dtype=float)),
                                     db_engine=eng, settings=_settings(),
                                     make_client=lambda c: _FakeClient(c), make_broker=lambda c: _FakeBroker(c))
    assert res["status"] == "no_targets"


def test_dry_run_previews_without_trading(monkeypatch):
    calls = _patch_engine(monkeypatch)
    eng = _eng()
    credstore.add_account(eng, slug="trend", api_key="PKTREND", api_secret="s")
    res = AR.run_strategy_on_account("trend", _Strat(), db_engine=eng, settings=_settings(),
                                     dry_run=True, make_client=lambda c: _FakeClient(c),
                                     make_broker=lambda c: _FakeBroker(c))
    assert res["status"] == "dry_run" and res["n_orders"] >= 1
    assert "submit" not in calls and "monitor_account" not in calls   # nothing traded/snapshotted
    assert all({"symbol", "side", "qty", "notional"} <= set(o) for o in res["orders"])
