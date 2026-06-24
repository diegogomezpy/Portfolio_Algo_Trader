"""Black-Scholes option math for the covered-call overlay.

Used by the Phase 2b backtest (`scripts/backtest_covered_calls.py`) to model premium
income and the upside cap, and reusable by the Phase 4 live overlay (`engine/
covered_calls.py`). European call **and put** pricing, delta, the strike that yields a
target delta, and the covered-call / cash-secured-put payoffs in return space (the put
side feeds the optional put-wheel overlay). Pure and vectorized (numpy); each function
accepts scalars or arrays.

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


def bs_put_price(S, K, T, sigma, r=0.0):
    """European put price. Degenerate (T≤0 or σ≤0) returns intrinsic value."""
    d1, d2 = _d1_d2(S, K, T, sigma, r)
    price = K * np.exp(-r * T) * norm.cdf(-d2) - S * norm.cdf(-d1)
    degenerate = (np.asarray(T) <= 0) | (np.asarray(sigma) <= 0)
    return np.where(degenerate, np.maximum(np.asarray(K) - np.asarray(S), 0.0), price)


def bs_call_delta(S, K, T, sigma, r=0.0):
    """Call delta N(d1) ∈ (0, 1). Degenerate returns the 1{S>K} step."""
    d1, _ = _d1_d2(S, K, T, sigma, r)
    degenerate = (np.asarray(T) <= 0) | (np.asarray(sigma) <= 0)
    return np.where(degenerate, (np.asarray(S) > np.asarray(K)).astype(float), norm.cdf(d1))


def bs_put_delta(S, K, T, sigma, r=0.0):
    """Put delta N(d1) − 1 ∈ (−1, 0). Degenerate returns the −1{S<K} step."""
    d1, _ = _d1_d2(S, K, T, sigma, r)
    degenerate = (np.asarray(T) <= 0) | (np.asarray(sigma) <= 0)
    return np.where(degenerate, -(np.asarray(S) < np.asarray(K)).astype(float), norm.cdf(d1) - 1.0)


def strike_for_delta(S, T, sigma, target_delta, r=0.0):
    """Strike K of the call whose delta equals ``target_delta`` (closed form).

    From δ = N(d1): K = S · exp((r + ½σ²)T − Φ⁻¹(δ)·σ√T). For an OTM call
    (δ < 0.5) this returns a strike above S.
    """
    T = np.maximum(T, 1e-12)
    sigma = np.maximum(sigma, 1e-12)
    return S * np.exp((r + 0.5 * sigma ** 2) * T - norm.ppf(target_delta) * sigma * np.sqrt(T))


def strike_for_put_delta(S, T, sigma, target_delta, r=0.0):
    """Strike K of the **put** whose delta magnitude equals ``target_delta`` (closed form).

    Put delta is N(d1) − 1; |Δ_put| = target_delta ⇒ N(d1) = 1 − target_delta, so
    K = S · exp((r + ½σ²)T − Φ⁻¹(1 − target_delta)·σ√T) — equivalently the strike of the
    (1 − target_delta) call. For an OTM put (target_delta < 0.5) this returns a strike below S.
    """
    return strike_for_delta(S, T, sigma, 1.0 - target_delta, r)


def covered_call_return(equity_return, strike_return, premium_yield):
    """Covered-call return (in return space) over one holding period.

    Long stock + short call: upside is capped at the strike, plus the premium kept.

        cc_return = min(equity_return, strike_return) + premium_yield

    where ``strike_return = strike/spot − 1`` and ``premium_yield = premium/spot``.
    """
    return np.minimum(equity_return, strike_return) + premium_yield


def cash_secured_put_return(equity_return, strike_return, premium_yield):
    """Short cash-secured put return (in return space) over one holding period.

    Sell an OTM put: keep the premium; below the strike you are assigned and bear the
    shortfall as if long from the strike.

        csp_return = premium_yield + min(equity_return − strike_return, 0)

    where ``strike_return = strike/spot − 1`` (< 0 for an OTM put) and
    ``premium_yield = premium/spot``. Above the strike the min term is 0 (keep premium);
    below it you lose ``strike_return − equity_return`` (the assigned-stock drawdown).
    """
    return premium_yield + np.minimum(equity_return - strike_return, 0.0)
