"""Unit tests for engine.signals — the composable signal library (ADR-001 north star).

The load-bearing test is PARITY: composing the four built-in signals with the live factor weights
reproduces ``factors.compute_factor_scores`` byte-for-byte. That's the guarantee that the current
book can later route through this library WITHOUT changing its targets. Plus registry mechanics.
"""

from __future__ import annotations

from datetime import date
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

from engine import factors, signals as S


def _settings(winsor_pct=0.01, beta_w=4, vol_w=4):
    return SimpleNamespace(
        universe=SimpleNamespace(min_price=5.0, min_adv_usd=1_000_000),
        factors=SimpleNamespace(
            winsor_pct=winsor_pct, beta_window=beta_w, beta_market="SPY", vol_window=vol_w,
            report_lag_days=45,
            weights=SimpleNamespace(quality=0.25, value=0.25, low_beta=0.25, low_vol=0.25)))


def _varied_panel(symbols, n=6):
    """Distinct price paths per name (+ a SPY market col) so beta/vol actually vary cross-sectionally."""
    idx = pd.bdate_range("2026-01-01", periods=n)
    rng = np.random.default_rng(7)
    data = {"SPY": 100 * np.cumprod(1 + rng.normal(0.001, 0.01, n))}
    for i, s in enumerate(symbols):
        data[s] = 100 * np.cumprod(1 + rng.normal(0.001 * (i + 1), 0.01 + 0.004 * i, n))
    return pd.DataFrame(data, index=idx)


@pytest.fixture(autouse=True)
def _iso():
    S.clear(); yield; S.clear()


def _fixtures():
    syms = ["AAA", "BBB", "CCC", "DDD"]
    snap = pd.DataFrame({"close": [100.0] * 4, "adv_20d": [5_000_000] * 4}, index=syms)
    fpit = pd.DataFrame({
        "pe_ratio": [8.0, 15.0, 30.0, -6.0],
        "pb_ratio": [1.0, 2.5, 4.0, 3.0],
        "roe": [0.22, 0.10, 0.05, 0.14],
        "gross_margin": [0.55, 0.35, 0.25, 0.42],
    }, index=syms)
    panel = _varied_panel(syms)
    return syms, snap, fpit, panel


def test_compose_matches_compute_factor_scores_exactly():
    """PARITY: pick all four signals at the live weights → identical composite + sub-scores."""
    st = _settings()
    syms, snap, fpit, panel = _fixtures()
    ref = factors.compute_factor_scores(
        panel.index[-1].date(), settings=st, price_panel=panel,
        fundamentals_pit=fpit, universe_snapshot=snap).set_index("symbol")

    ctx = S.SignalContext(as_of=panel.index[-1].date(), settings=st,
                          price_panel=panel, fundamentals_pit=fpit)
    order = list(ref.index)                                   # compute over the exact same cross-section
    S.register_builtins()
    scores = {n: S.get(n).compute(ctx, order) for n in ("quality", "value", "low_beta", "low_vol")}

    # each signal == its live sub-score column
    pd.testing.assert_series_equal(scores["quality"].reindex(order), ref["quality_score"].reindex(order),
                                   check_names=False)
    pd.testing.assert_series_equal(scores["value"].reindex(order), ref["value_score"].reindex(order),
                                   check_names=False)
    pd.testing.assert_series_equal(scores["low_beta"].reindex(order), ref["beta_score"].reindex(order),
                                   check_names=False)
    pd.testing.assert_series_equal(scores["low_vol"].reindex(order), ref["lowvol_score"].reindex(order),
                                   check_names=False)

    # composite == live composite_score
    w = {"quality": 0.25, "value": 0.25, "low_beta": 0.25, "low_vol": 0.25}
    composite = S.compose(scores, w)
    pd.testing.assert_series_equal(composite.reindex(order), ref["composite_score"].reindex(order),
                                   check_names=False)


def test_compose_neutral_fills_missing_signal_scores():
    # A signal with NaNs contributes 0 there (matches compute_factor_scores' neutral fill).
    a = pd.Series({"X": 1.0, "Y": np.nan, "Z": -1.0})
    b = pd.Series({"X": 2.0, "Y": 2.0, "Z": 2.0})
    out = S.compose({"a": a, "b": b}, {"a": 0.5, "b": 0.5})
    assert out["Y"] == pytest.approx(1.0)                    # 0.5*0 + 0.5*2
    assert out["X"] == pytest.approx(1.5)                    # 0.5*1 + 0.5*2


def test_compose_skips_weights_for_absent_signals():
    a = pd.Series({"X": 1.0, "Y": -1.0})
    out = S.compose({"a": a}, {"a": 1.0, "notpresent": 0.5})   # absent signal ignored
    pd.testing.assert_series_equal(out, a, check_names=False)


def test_signal_declares_data_needs():
    assert S.ValueSignal().needs == ("fundamentals",)
    assert S.LowBetaSignal().needs == ("prices",)


def test_registry_register_get_all_and_builtins():
    S.register_builtins()
    assert set(S.all_signals()) == {"quality", "value", "low_beta", "low_vol",
                                    "roe", "gross_margin", "earnings_yield", "book_yield", "momentum"}
    assert isinstance(S.get("value"), S.ValueSignal)
    n = len(S.all_signals())
    S.register_builtins()                                    # idempotent
    assert len(S.all_signals()) == n


def test_single_metric_signals_are_zscored_and_oriented():
    """The finer building blocks (roe/gross_margin/earnings_yield/book_yield/momentum) return a
    z-scored, higher = better series over the cross-section."""
    st = _settings()
    syms, snap, fpit, panel = _fixtures()
    ctx = S.SignalContext(as_of=panel.index[-1].date(), settings=st,
                          price_panel=panel, fundamentals_pit=fpit)
    S.register_builtins()
    for name in ("roe", "gross_margin", "earnings_yield", "book_yield", "momentum"):
        out = S.get(name).compute(ctx, syms)
        assert list(out.index) == syms and out.notna().any()
        assert abs(float(out.mean())) < 1e-6                 # z-scored → ~zero mean
    # ROE orientation: AAA has the highest ROE (0.22) → highest z; CCC the lowest (0.05) → lowest.
    roe = S.get("roe").compute(ctx, syms)
    assert roe["AAA"] == roe.max() and roe["CCC"] == roe.min()


def test_momentum_helper_ranks_by_trailing_return():
    idx = pd.bdate_range("2026-01-01", periods=8)
    panel = pd.DataFrame({"UP": np.linspace(100, 130, 8),      # strong up-trend
                          "FLAT": [100.0] * 8,                  # no move
                          "DN": np.linspace(100, 80, 8)}, index=idx)
    mom = S._momentum(panel, idx[-1].date(), lookback=6, skip=1)
    assert mom["UP"] > mom["FLAT"] > mom["DN"]


def test_raw_fields_returns_native_values_by_source():
    st = _settings()
    syms, snap, fpit, panel = _fixtures()
    ctx = S.SignalContext(as_of=panel.index[-1].date(), settings=st,
                          price_panel=panel, fundamentals_pit=fpit)
    rf = S.raw_fields(ctx, syms)
    assert set(rf) == set(S.FIELD_META)                       # all documented fields present
    assert rf["raw_roe"]["AAA"] == pytest.approx(0.22)        # raw, un-z-scored
    assert rf["raw_ep"]["AAA"] == pytest.approx(1.0 / 8.0)    # E/P = 1/PE
    # fundamentals-only context omits price fields
    rf2 = S.raw_fields(S.SignalContext(as_of=panel.index[-1].date(), settings=st,
                                       price_panel=None, fundamentals_pit=fpit), syms)
    assert "raw_beta" not in rf2 and "raw_roe" in rf2


def test_signal_and_field_specs_carry_metadata():
    specs = {s["name"]: s for s in S.signal_specs()}
    assert specs["quality"]["label"] == "Quality" and specs["quality"]["category"]
    assert specs["momentum"]["needs"] == ["prices"]
    fields = {f["name"]: f for f in S.field_specs()}
    assert fields["raw_pe"]["needs"] == ["fundamentals"] and fields["raw_pe"]["desc"]


def test_registry_rejects_junk_and_collisions():
    with pytest.raises(TypeError):
        S.register(object())
    S.register(S.ValueSignal())
    with pytest.raises(ValueError):
        S.register(SimpleNamespace(name="value", compute=lambda *a: None))  # same name, diff object


def test_runtime_checkable_protocol():
    assert isinstance(S.ValueSignal(), S.Signal)
    assert not isinstance(object(), S.Signal)
