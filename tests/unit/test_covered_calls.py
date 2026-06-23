"""Unit tests for engine.covered_calls — the pure overlay planner (4.1).

Synthetic chains / holdings. Guards contract sizing (the ≥100-share partial-coverage rule),
delta-nearest strike selection within the DTE window, the write plan (sizing + mid premium +
skips), and the buy-to-close plan.
"""

from __future__ import annotations

from datetime import date
from types import SimpleNamespace

from engine import covered_calls as cc
from engine import options


def _settings(target_delta=0.30, min_dte=30, max_dte=45):
    return SimpleNamespace(covered_calls=SimpleNamespace(
        target_delta=target_delta, min_dte_entry=min_dte, max_dte_entry=max_dte))


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
