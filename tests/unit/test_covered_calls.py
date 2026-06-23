"""Unit tests for engine.covered_calls — the pure overlay planner (4.1).

Synthetic chains / holdings. Guards contract sizing (the ≥100-share partial-coverage rule),
delta-nearest strike selection within the DTE window, the write plan (sizing + mid premium +
skips), and the buy-to-close plan.
"""

from __future__ import annotations

from datetime import date, timedelta
from types import SimpleNamespace

import numpy as np
import pandas as pd
from sqlalchemy import create_engine, select

from engine import covered_calls as cc
from engine import db, options


def _settings(target_delta=0.30, min_dte=30, max_dte=45, cov_window=60):
    return SimpleNamespace(
        covered_calls=SimpleNamespace(target_delta=target_delta, min_dte_entry=min_dte,
                                      max_dte_entry=max_dte),
        covariance=SimpleNamespace(estimation_window_days=cov_window))


AS_OF = date(2026, 7, 1)
IN_WINDOW = "2026-08-05"      # 35 DTE
OUT_WINDOW = "2026-07-08"     # 7 DTE


def _call(symbol, strike, exp=IN_WINDOW, mid=1.0):
    return {"symbol": symbol, "underlying": "X", "type": "call", "strike": strike,
            "expiration": exp, "bid": mid - 0.05, "ask": mid + 0.05, "mid": mid}


# ---------------------------------------------------------------- sizing ----
def test_contracts_for_floor_and_partial_coverage_rule():
    assert cc.contracts_for(250) == 2
    assert cc.contracts_for(100) == 1
    assert cc.contracts_for(99) == 0          # below one contract → uncoverable
    assert cc.contracts_for(50) == 0


# -------------------------------------------------------- strike select ----
def test_select_strike_picks_delta_nearest_target():
    strikes = [100, 105, 110, 115, 120]
    chain = [_call(f"X_{k}", k) for k in strikes]
    chosen = cc.select_strike(chain, spot=100.0, iv=0.30, target_delta=0.30,
                              as_of=AS_OF, min_dte=30, max_dte=45)
    # Independently compute the expected nearest-delta strike.
    T = (date.fromisoformat(IN_WINDOW) - AS_OF).days / 365.0
    deltas = {k: float(options.bs_call_delta(100.0, k, T, 0.30)) for k in strikes}
    expected = min(strikes, key=lambda k: abs(deltas[k] - 0.30))
    assert chosen["strike"] == expected
    assert chosen["delta"] == deltas[expected]


def test_select_strike_filters_dte_window_and_missing_mid():
    chain = [
        _call("near", 105, exp=OUT_WINDOW),       # too soon (7 DTE) — excluded
        {"symbol": "noq", "strike": 106, "expiration": IN_WINDOW, "mid": None},  # no quote
        _call("good", 107, exp=IN_WINDOW),        # the only eligible one
    ]
    chosen = cc.select_strike(chain, spot=100.0, iv=0.30, target_delta=0.30,
                              as_of=AS_OF, min_dte=30, max_dte=45)
    assert chosen["symbol"] == "good"


def test_select_strike_none_when_nothing_eligible():
    chain = [_call("near", 105, exp=OUT_WINDOW)]   # all out of window
    assert cc.select_strike(chain, spot=100.0, iv=0.30, target_delta=0.30,
                            as_of=AS_OF, min_dte=30, max_dte=45) is None


# ----------------------------------------------------------- write plan ----
def test_build_write_plan_sizes_skips_and_prices_at_mid():
    holdings = {"AAA": 250, "BBB": 99, "CCC": 300}     # BBB < 100 → skipped
    chains = {
        "AAA": [_call("AAA_C", 110, mid=2.0)],
        "CCC": [],                                      # no chain → skipped
    }
    spots = {"AAA": 100.0, "CCC": 100.0}
    ivs = {"AAA": 0.30, "CCC": 0.30}
    writes, skipped = cc.build_write_plan(holdings, chains, spots, ivs,
                                          settings=_settings(), as_of=AS_OF)
    assert len(writes) == 1
    w = writes[0]
    assert w.action == "sell_to_open" and w.underlying == "AAA"
    assert w.contracts == 2 and w.limit_price == 2.0          # 250 sh → 2 contracts, mid 2.0
    assert w.premium == 2 * 100 * 2.0                          # contracts × 100 × mid
    reasons = {s["symbol"]: s["reason"] for s in skipped}
    assert "BBB" in reasons and "100" in reasons["BBB"]
    assert "CCC" in reasons


# ----------------------------------------------------------- close plan ----
def test_build_close_plan_buys_to_close_each_short_call():
    positions = [
        {"symbol": "AAPL260821C00215000", "qty": -2, "mid": 3.10},   # short 2 calls
        {"symbol": "MSFT260821C00500000", "qty": -1},                # no quote attached
        {"symbol": "ZERO260821C00100000", "qty": 0},                 # nothing to close
    ]
    closes = cc.build_close_plan(positions)
    assert len(closes) == 2
    by_u = {o.underlying: o for o in closes}
    assert by_u["AAPL"].action == "buy_to_close" and by_u["AAPL"].contracts == 2
    assert by_u["AAPL"].limit_price == 3.10
    assert by_u["MSFT"].contracts == 1 and by_u["MSFT"].limit_price is None  # filled by I/O later


# ====================================================================== #
# I/O (4.3) — fake client/broker + in-memory sqlite
# ====================================================================== #
class _FakeClient:
    def __init__(self, positions=None, chains=None):
        self._positions = positions or []
        self._chains = chains or {}

    def all_positions(self):
        return list(self._positions)

    def option_chain(self, underlying, *, option_type="call", expiration_gte=None,
                     expiration_lte=None, **kw):
        val = self._chains.get(underlying, [])
        if isinstance(val, Exception):
            raise val
        return val


class _FakeBroker:
    def __init__(self, reject=()):
        self.reject = set(reject)
        self.option_orders = []

    def submit_option_order(self, option_symbol, contracts, side, *, position_intent,
                            order_type="limit", limit_price=None, client_order_id=None):
        if option_symbol in self.reject:
            from engine.alpaca_client import AlpacaAPIError
            raise AlpacaAPIError(option_symbol, "submit_option_order", "422")
        rec = dict(option_symbol=option_symbol, contracts=contracts, side=side,
                   position_intent=position_intent, order_type=order_type,
                   limit_price=limit_price, client_order_id=client_order_id,
                   id=f"oid-{len(self.option_orders) + 1}", status="accepted")
        self.option_orders.append(rec)
        return rec


def _engine():
    eng = create_engine("sqlite://")
    db.create_all(eng)
    return eng


def _panel(symbols, n=70, seed=0):
    rng = np.random.default_rng(seed)
    idx = pd.bdate_range("2026-03-02", periods=n)
    data = {s: 100 * np.cumprod(1 + rng.normal(0, 0.02, n)) for s in symbols}
    return pd.DataFrame(data, index=idx)


def _lifecycle(eng):
    with eng.connect() as c:
        return [dict(r) for r in c.execute(select(db.options_lifecycle)).mappings()]


def test_estimate_ivs_annualizes_realized_vol():
    panel = _panel(["AAA"])
    as_of = panel.index[-1].date()
    ivs = cc.estimate_ivs(panel, ["AAA"], as_of, window=60)
    assert "AAA" in ivs and ivs["AAA"] > 0


def test_fetch_chains_degrades_on_error():
    as_of = date(2026, 7, 1)
    client = _FakeClient(chains={"AAA": [_call("AAA_C", 105)], "BAD": ValueError("boom")})
    chains = cc.fetch_chains(client, ["AAA", "BAD"], as_of, min_dte=30, max_dte=45)
    assert len(chains["AAA"]) == 1 and chains["BAD"] == []


def test_open_call_positions_filters_short_calls_and_estimates_mid():
    positions = [
        {"symbol": "AAA260821C00105000", "qty": -2, "market_value": -620.0, "asset_class": "us_option"},
        {"symbol": "AAPL", "qty": 100, "market_value": 20000.0, "asset_class": "us_equity"},
        {"symbol": "BBB260821P00090000", "qty": -1, "market_value": -300.0, "asset_class": "us_option"},  # put
        {"symbol": "CCC260821C00100000", "qty": 1, "market_value": 200.0, "asset_class": "us_option"},     # long call
    ]
    out = cc.open_call_positions(_FakeClient(positions=positions))
    assert len(out) == 1
    assert out[0]["underlying"] == "AAA" and out[0]["mid"] == 3.10        # 620 / (2 × 100)


def test_write_calls_submits_sell_to_open_and_logs_lifecycle():
    eng = _engine()
    panel = _panel(["AAA"])
    as_of = panel.index[-1].date()
    exp = (as_of + timedelta(days=35)).isoformat()
    chains = {"AAA": [{"symbol": "AAA_C105", "strike": 105.0, "expiration": exp, "mid": 2.0}]}
    broker = _FakeBroker()
    submitted, skipped = cc.write_calls(
        _FakeClient(chains=chains), broker, eng, {"AAA": 250},
        settings=_settings(), as_of=as_of, price_panel=panel)
    assert len(submitted) == 1 and skipped == []
    o = broker.option_orders[0]
    assert o["side"] == "sell" and o["position_intent"] == "sell_to_open"
    assert o["contracts"] == 2 and o["limit_price"] == 2.0
    rows = _lifecycle(eng)
    assert len(rows) == 1 and rows[0]["event_type"] == "write"
    assert rows[0]["premium"] == 400.0 and rows[0]["contracts"] == 2     # 2 × 100 × 2.0


def test_close_calls_submits_buy_to_close_and_logs_lifecycle():
    eng = _engine()
    positions = [{"symbol": "AAA260821C00105000", "qty": -2, "market_value": -620.0,
                  "asset_class": "us_option"}]
    broker = _FakeBroker()
    submitted = cc.close_calls(_FakeClient(positions=positions), broker, eng, as_of=date(2026, 7, 1))
    assert len(submitted) == 1
    o = broker.option_orders[0]
    assert o["side"] == "buy" and o["position_intent"] == "buy_to_close" and o["contracts"] == 2
    rows = _lifecycle(eng)
    assert rows[0]["event_type"] == "close" and rows[0]["premium"] == -620.0   # -(3.10 × 2 × 100)


# ====================================================================== #
# Daily safety checks (4.5)
# ====================================================================== #
def test_occ_expiration_parsing():
    assert cc._occ_expiration("AAPL260821C00215000") == date(2026, 8, 21)
    assert cc._occ_expiration("not-occ") is None


def test_needs_earnings_close():
    exp = date(2026, 8, 21)
    assert cc.needs_earnings_close(exp, date(2026, 8, 10), date(2026, 7, 1)) is True   # within life
    assert cc.needs_earnings_close(exp, date(2026, 9, 1), date(2026, 7, 1)) is False   # after expiry
    assert cc.needs_earnings_close(exp, date(2026, 6, 15), date(2026, 7, 1)) is False  # already past
    assert cc.needs_earnings_close(exp, None, date(2026, 7, 1)) is False


def test_is_expiring():
    assert cc.is_expiring(date(2026, 7, 1), date(2026, 7, 1)) is True            # DTE 0
    assert cc.is_expiring(date(2026, 8, 21), date(2026, 7, 1)) is False
    assert cc.is_expiring(date(2026, 7, 15), date(2026, 7, 1), within_days=30) is True


def test_next_and_last_earnings():
    dates = [date(2026, 1, 10), date(2026, 6, 28), date(2026, 9, 5)]
    assert cc.next_earnings(dates, date(2026, 7, 1)) == date(2026, 9, 5)
    assert cc.last_earnings(dates, date(2026, 7, 1)) == date(2026, 6, 28)


def test_earnings_and_expiry_close_plans():
    open_calls = [
        {"symbol": "AAA260821C00100000", "qty": -1, "underlying": "AAA", "mid": 2.0},  # earnings
        {"symbol": "BBB260701C00100000", "qty": -1, "underlying": "BBB", "mid": 1.0},  # expiring
        {"symbol": "CCC260821C00100000", "qty": -1, "underlying": "CCC", "mid": 1.0},  # neither
    ]
    as_of = date(2026, 7, 1)
    earn = cc.earnings_close_plan(open_calls, {"AAA": date(2026, 8, 10)}, as_of)
    assert {o.underlying for o in earn} == {"AAA"}
    exp = cc.expiry_close_plan(open_calls, as_of)
    assert {o.underlying for o in exp} == {"BBB"}


def test_options_daily_check_closes_into_earnings_and_rewrites():
    eng = _engine()
    as_of = date(2026, 7, 1)
    exp = (as_of + timedelta(days=35)).isoformat()
    panel = _panel(["MSFT"])
    # AAPL: open short call reporting within its life → earnings-close.
    # MSFT: held equity, just reported (within 5d), uncovered → rewrite.
    client = _FakeClient(
        positions=[
            {"symbol": "AAPL260821C00215000", "qty": -1, "market_value": -300.0, "asset_class": "us_option"},
            {"symbol": "MSFT", "qty": 100, "market_value": 20000.0, "asset_class": "us_equity"},
        ],
        chains={"MSFT": [{"symbol": "MSFT_C", "strike": 105.0, "expiration": exp, "mid": 2.0}]})
    earn = {"AAPL": [date(2026, 7, 10)], "MSFT": [date(2026, 6, 28)]}
    broker = _FakeBroker()
    out = cc.options_daily_check(client, broker, eng, settings=_settings(), as_of=as_of,
                                 price_panel=panel, earnings_fetch=lambda s: earn.get(s, []))
    assert out == {"expiry_closed": 0, "earnings_closed": 1, "rewritten": 1}
    sides = {(o["side"], o["position_intent"]) for o in broker.option_orders}
    assert ("buy", "buy_to_close") in sides and ("sell", "sell_to_open") in sides
    events = {r["event_type"] for r in _lifecycle(eng)}
    assert events == {"earnings_close", "write"}
