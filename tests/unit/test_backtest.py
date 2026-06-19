"""Unit tests for scripts.backtest pure helpers — costs, turnover, metrics.

No data/network: each helper is deterministic. The walk-forward driver itself is
covered by tests/integration/test_backtest_pipeline.py.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from scripts import backtest as bt

_COST = dict(large_thr=50e6, mid_thr=5e6, bps_large=5, bps_mid=10, bps_small=20)


# ------------------------------------------------------------- cost tiers ----
@pytest.mark.parametrize("adv, expected", [
    (100e6, 5), (50e6, 5),          # large
    (10e6, 10), (5e6, 10),          # mid
    (1e6, 20), (0.0, 20),           # small
    (np.nan, 20),                   # unknown ADV -> most conservative tier
])
def test_cost_bps_for_adv_tiers(adv, expected):
    assert bt.cost_bps_for_adv(adv, **_COST) == expected


def test_transaction_cost_sums_tiered_bps_on_traded_notional():
    prev = pd.Series(dtype=float)                      # initial build: trade the whole book
    new = pd.Series({"BIG": 0.05, "SMALL": 0.05})
    adv = pd.Series({"BIG": 100e6, "SMALL": 1e6})      # 5 bps and 20 bps
    cost = bt.transaction_cost(prev, new, adv, **_COST)
    assert cost == pytest.approx(0.05 * 5 / 1e4 + 0.05 * 20 / 1e4)


def test_transaction_cost_only_charges_changes():
    prev = pd.Series({"A": 0.05, "B": 0.05})
    new = pd.Series({"A": 0.05, "C": 0.05})            # A unchanged; sell B, buy C
    adv = pd.Series({"A": 100e6, "B": 100e6, "C": 100e6})
    cost = bt.transaction_cost(prev, new, adv, **_COST)
    assert cost == pytest.approx((0.05 + 0.05) * 5 / 1e4)   # |ΔB|+|ΔC|, A contributes 0


# --------------------------------------------------------------- turnover ----
def test_turnover_is_one_way():
    prev = pd.Series({"A": 0.5})
    new = pd.Series({"A": 0.3, "B": 0.4})
    assert bt.turnover(prev, new) == pytest.approx((0.2 + 0.4) / 2)


def test_turnover_full_build_from_empty():
    new = pd.Series({"A": 0.05, "B": 0.05})
    assert bt.turnover(pd.Series(dtype=float), new) == pytest.approx(0.05)   # ½ × 0.10


# ----------------------------------------------------------------- drift ----
def test_drift_weights_preserves_invested_fraction():
    w = pd.Series({"A": 0.475, "B": 0.475})           # 95% invested
    rets = pd.Series({"A": 1.0, "B": 0.0})            # A doubles
    drifted = bt.drift_weights(w, rets)
    assert drifted.sum() == pytest.approx(0.95)        # invested fraction unchanged
    assert drifted["A"] > drifted["B"]                 # winner grows its share
    assert drifted["A"] == pytest.approx(0.95 * (0.95 / 1.425))


# ------------------------------------------------------------ drawdown ----
def test_max_drawdown():
    curve = pd.Series([1.0, 1.2, 0.9, 1.1])
    assert bt.max_drawdown(curve) == pytest.approx(0.9 / 1.2 - 1.0)   # -25%


def test_max_drawdown_monotonic_up_is_zero():
    assert bt.max_drawdown(pd.Series([1.0, 1.1, 1.2])) == pytest.approx(0.0)


# ------------------------------------------------------------- metrics ----
def test_portfolio_metrics_basic():
    r = pd.Series([0.02, -0.01, 0.03, 0.00, 0.015])
    m = bt.portfolio_metrics(r)
    assert m["n_months"] == 5
    expected_ann = (1 + r).prod() ** (12 / 5) - 1
    assert m["ann_return"] == pytest.approx(expected_ann)
    assert m["max_drawdown"] <= 0
    assert np.isfinite(m["sharpe"])


def test_portfolio_metrics_empty():
    m = bt.portfolio_metrics(pd.Series(dtype=float))
    assert m["n_months"] == 0 and np.isnan(m["sharpe"])


def test_portfolio_metrics_calmar_sign():
    # Net-positive return with a real drawdown => positive Calmar.
    r = pd.Series([0.05, -0.08, 0.06, 0.04])
    m = bt.portfolio_metrics(r)
    assert (m["calmar"] > 0) == (m["ann_return"] > 0)


# -------------------------------------------------------------- sleeves ----
def test_sleeve_return_top_n_equal_weight():
    sub = pd.Series({"A": 3.0, "B": 2.0, "C": 1.0, "D": np.nan})
    fwd = pd.Series({"A": 0.10, "B": 0.20, "C": -0.50})
    # top-2 by sub-score = A, B => mean(0.10, 0.20)
    assert bt.sleeve_return(sub, fwd, top_n=2) == pytest.approx(0.15)


def test_sleeve_return_skips_missing_forward():
    sub = pd.Series({"A": 3.0, "B": 2.0})
    fwd = pd.Series({"A": 0.10})                       # B has no forward return
    assert bt.sleeve_return(sub, fwd, top_n=2) == pytest.approx(0.10)
