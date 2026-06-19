"""Black-Scholes option math for the covered-call overlay.

Used by the Phase 2b backtest (`scripts/backtest_covered_calls.py`) to model premium
income and the upside cap, and reusable by the Phase 4 live overlay (`engine/
covered_calls.py`). European-call pricing, delta, the strike that yields a target
delta, and the covered-call payoff in return space. Pure and vectorized (numpy);
each function accepts scalars or arrays.

Rates default to ``r = 0``: at 30-45 DTE the carry term is a second-order effect on
this overlay, and the backtest already works in excess-of-cash terms. The volatility
input is the **assumption** that matters (DECISIONS D27) — single-name historical IV
is not freely available, so the caller supplies an IV estimate and sensitivity-tests it.
"""

from __future__ import annotations

import numpy as np
from scipy.stats import norm


def _d1_d2(S, K, T, sigma, r):
    T = np.maximum(T, 1e-12)
    sigma = np.maximum(sigma, 1e-12)
    vol = sigma * np.sqrt(T)
    d1 = (np.log(S / K) + (r + 0.5 * sigma ** 2) * T) / vol
    return d1, d1 - vol


def bs_call_price(S, K, T, sigma, r=0.0):
    """European call price. Degenerate (T≤0 or σ≤0) returns intrinsic value."""
    d1, d2 = _d1_d2(S, K, T, sigma, r)
    price = S * norm.cdf(d1) - K * np.exp(-r * T) * norm.cdf(d2)
    degenerate = (np.asarray(T) <= 0) | (np.asarray(sigma) <= 0)
    return np.where(degenerate, np.maximum(np.asarray(S) - np.asarray(K), 0.0), price)


def bs_call_delta(S, K, T, sigma, r=0.0):
    """Call delta N(d1) ∈ (0, 1). Degenerate returns the 1{S>K} step."""
    d1, _ = _d1_d2(S, K, T, sigma, r)
    degenerate = (np.asarray(T) <= 0) | (np.asarray(sigma) <= 0)
    return np.where(degenerate, (np.asarray(S) > np.asarray(K)).astype(float), norm.cdf(d1))


def strike_for_delta(S, T, sigma, target_delta, r=0.0):
    """Strike K of the call whose delta equals ``target_delta`` (closed form).

    From δ = N(d1): K = S · exp((r + ½σ²)T − Φ⁻¹(δ)·σ√T). For an OTM call
    (δ < 0.5) this returns a strike above S.
    """
    T = np.maximum(T, 1e-12)
    sigma = np.maximum(sigma, 1e-12)
    return S * np.exp((r + 0.5 * sigma ** 2) * T - norm.ppf(target_delta) * sigma * np.sqrt(T))


def covered_call_return(equity_return, strike_return, premium_yield):
    """Covered-call return (in return space) over one holding period.

    Long stock + short call: upside is capped at the strike, plus the premium kept.

        cc_return = min(equity_return, strike_return) + premium_yield

    where ``strike_return = strike/spot − 1`` and ``premium_yield = premium/spot``.
    """
    return np.minimum(equity_return, strike_return) + premium_yield
