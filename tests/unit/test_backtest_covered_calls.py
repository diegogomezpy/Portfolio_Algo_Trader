"""Unit tests for scripts.backtest_covered_calls helpers (IV estimate, VIX loader).

The walk-forward overlay + ^BXM validation run on real data and are exercised live;
here we pin the pure pieces.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from scripts import backtest_covered_calls as cc


def test_estimate_iv_realized_is_identity():
    rv = pd.Series({"A": 0.25, "B": 0.40})
    out = cc.estimate_iv(rv, vix=0.20, rv_spx=0.15, mode="realized")
    pd.testing.assert_series_equal(out, rv)


def test_estimate_iv_vix_scaled_applies_market_ratio():
    rv = pd.Series({"A": 0.30})
    # ratio = VIX / SPX realized vol = 0.20 / 0.10 = 2.0
    out = cc.estimate_iv(rv, vix=0.20, rv_spx=0.10, mode="vix_scaled")
    assert out["A"] == pytest.approx(0.60)


def test_estimate_iv_vix_scaled_ratio_floored_at_one():
    # When realized index vol exceeds VIX, don't subtract premium (ratio floored at 1).
    rv = pd.Series({"A": 0.30})
    out = cc.estimate_iv(rv, vix=0.10, rv_spx=0.20, mode="vix_scaled")
    assert out["A"] == pytest.approx(0.30)


def test_estimate_iv_unknown_mode_raises():
    with pytest.raises(ValueError):
        cc.estimate_iv(pd.Series({"A": 0.3}), vix=0.2, rv_spx=0.1, mode="bogus")


def test_load_vix_injected_fetch_and_cache(tmp_path):
    idx = pd.to_datetime(["2024-01-02", "2024-01-03"])
    fake = pd.DataFrame({"Close": [18.0, 22.0]}, index=idx)
    cache = tmp_path / "vix.parquet"
    vix = cc.load_vix(cache_path=cache, fetch=lambda: fake)
    assert vix.loc["2024-01-02"] == pytest.approx(0.18)      # percent -> decimal
    assert cache.exists()
    # Second call hits the cache (fetch would raise if used).
    again = cc.load_vix(cache_path=cache, fetch=lambda: (_ for _ in ()).throw(AssertionError("refetched")))
    pd.testing.assert_series_equal(vix, again)
