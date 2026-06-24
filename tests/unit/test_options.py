"""Unit tests for engine.options — Black-Scholes pricing, delta, strike, payoff."""

from __future__ import annotations

import numpy as np
import pytest

from engine import options


def test_bs_call_price_atm_known_value():
    # ATM, r=0: price ≈ 0.4·S·σ·√T (Brenner-Subrahmanyam); exact BS ≈ 7.97.
    price = float(options.bs_call_price(100, 100, 1.0, 0.20, r=0.0))
    assert price == pytest.approx(7.966, abs=0.01)


def test_bs_call_price_intrinsic_at_expiry():
    assert float(options.bs_call_price(110, 100, 0.0, 0.20)) == pytest.approx(10.0)
    assert float(options.bs_call_price(90, 100, 0.0, 0.20)) == pytest.approx(0.0)


def test_bs_call_delta_atm_and_bounds():
    d = float(options.bs_call_delta(100, 100, 1.0, 0.20, r=0.0))
    assert d == pytest.approx(0.54, abs=0.01)          # slightly > 0.5 (d1 = ½σ√T > 0)
    deep_itm = float(options.bs_call_delta(200, 100, 0.1, 0.20))
    assert deep_itm > 0.99
    deep_otm = float(options.bs_call_delta(50, 100, 0.1, 0.20))
    assert deep_otm < 0.01


def test_strike_for_delta_roundtrips():
    S, T, sigma, target = 100.0, 35 / 365, 0.30, 0.25
    K = float(options.strike_for_delta(S, T, sigma, target))
    assert K > S                                        # 0.25-delta call is OTM
    assert float(options.bs_call_delta(S, K, T, sigma)) == pytest.approx(target, abs=1e-6)


def test_strike_for_delta_vectorized():
    S = np.array([100.0, 50.0])
    K = options.strike_for_delta(S, 35 / 365, np.array([0.3, 0.5]), 0.25)
    deltas = options.bs_call_delta(S, K, 35 / 365, np.array([0.3, 0.5]))
    np.testing.assert_allclose(deltas, 0.25, atol=1e-6)


@pytest.mark.parametrize("eq, strike, prem, expected", [
    (0.20, 0.10, 0.02, 0.12),     # rally beyond strike: capped at strike + premium
    (-0.05, 0.10, 0.02, -0.03),   # down month: full equity loss + premium cushion
    (0.08, 0.10, 0.02, 0.10),     # up but below strike: keep full gain + premium
])
def test_covered_call_return(eq, strike, prem, expected):
    assert options.covered_call_return(eq, strike, prem) == pytest.approx(expected)


def test_covered_call_return_vectorized():
    eq = np.array([0.20, -0.05, 0.08])
    out = options.covered_call_return(eq, 0.10, 0.02)
    np.testing.assert_allclose(out, [0.12, -0.03, 0.10])


# ----------------------------------------------------------------- puts ----
def test_bs_put_price_parity_and_intrinsic():
    # Put-call parity at r=0: C − P = S − K.
    S, K, T, sig = 100.0, 100.0, 1.0, 0.20
    c = float(options.bs_call_price(S, K, T, sig))
    p = float(options.bs_put_price(S, K, T, sig))
    assert (c - p) == pytest.approx(S - K, abs=1e-6)          # ATM ⇒ C == P
    assert float(options.bs_put_price(90, 100, 0.0, sig)) == pytest.approx(10.0)   # intrinsic
    assert float(options.bs_put_price(110, 100, 0.0, sig)) == pytest.approx(0.0)


def test_bs_put_delta_bounds_and_parity():
    d = float(options.bs_put_delta(100, 100, 1.0, 0.20))
    assert -1.0 < d < 0.0 and d == pytest.approx(-0.46, abs=0.01)   # = call delta − 1
    assert float(options.bs_put_delta(50, 100, 0.1, 0.20)) < -0.99  # deep ITM put → −1
    assert float(options.bs_put_delta(200, 100, 0.1, 0.20)) > -0.01  # deep OTM put → 0


def test_strike_for_put_delta_roundtrips_below_spot():
    S, T, sig, target = 100.0, 35 / 365, 0.30, 0.30
    K = float(options.strike_for_put_delta(S, T, sig, target))
    assert K < S                                                    # 0.30-delta put is OTM
    assert abs(float(options.bs_put_delta(S, K, T, sig))) == pytest.approx(target, abs=1e-6)


@pytest.mark.parametrize("eq, strike_ret, prem, expected", [
    (0.20, -0.08, 0.02, 0.02),     # rally: put expires worthless, keep premium
    (-0.03, -0.08, 0.02, 0.02),    # down but above strike: not assigned, keep premium
    (-0.15, -0.08, 0.02, -0.05),   # assigned (below strike): premium − (0.15−0.08)
])
def test_cash_secured_put_return(eq, strike_ret, prem, expected):
    assert options.cash_secured_put_return(eq, strike_ret, prem) == pytest.approx(expected)


def test_cash_secured_put_return_vectorized():
    eq = np.array([0.20, -0.03, -0.15])
    out = options.cash_secured_put_return(eq, -0.08, 0.02)
    np.testing.assert_allclose(out, [0.02, 0.02, -0.05])
