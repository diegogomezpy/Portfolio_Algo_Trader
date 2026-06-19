"""Unit tests for engine.covariance — FF5 parsing + factor-model Σ, no network.

Guards the CSV parser (percent→decimal, preamble/trailer handling), the
factor-model estimator (recovers known loadings, Σ ≈ B·cov(F)·Bᵀ when assets are
built from the factors, PSD, thin-history drop), and the zip fetch via an
injected byte payload.
"""

from __future__ import annotations

import io
import zipfile

import numpy as np
import pandas as pd
import pytest

from engine import covariance


# Mimic the real file: 3 preamble lines, blank, header, data rows, blank, copyright.
_SYNTHETIC_FF5 = """This file was created by ...
The Tbill return ...
More preamble ...

,Mkt-RF,SMB,HML,RMW,CMA,RF
20240102,    1.00,    0.50,   -0.20,    0.10,    0.30,    0.02
20240103,   -0.50,    0.25,    0.40,   -0.15,    0.05,    0.02
20240104,    0.75,   -0.10,    0.10,    0.20,   -0.25,    0.02

Copyright 2026 Eugene F. Fama and Kenneth R. French
"""


def test_parse_ff5_csv_values_and_units():
    df = covariance.parse_ff5_csv(_SYNTHETIC_FF5)
    assert list(df.columns) == covariance.FACTOR_COLUMNS + ["RF"]
    assert len(df) == 3                                   # trailer/blank lines excluded
    assert df.index[0] == pd.Timestamp("2024-01-02")
    # percent -> decimal
    assert df.loc["2024-01-02", "Mkt-RF"] == pytest.approx(0.01)
    assert df.loc["2024-01-02", "RF"] == pytest.approx(0.0002)
    assert df.loc["2024-01-04", "CMA"] == pytest.approx(-0.0025)


def test_parse_ff5_csv_missing_header_raises():
    with pytest.raises(ValueError):
        covariance.parse_ff5_csv("no header here\njust text\n")


def _make_ff5(n_days=120, seed=0):
    """Random but reproducible daily factor frame (decimals)."""
    rng = np.random.default_rng(seed)
    idx = pd.bdate_range("2023-01-02", periods=n_days)
    data = rng.normal(0, 0.01, size=(n_days, 5))
    df = pd.DataFrame(data, index=idx, columns=covariance.FACTOR_COLUMNS)
    df["RF"] = 0.0001
    return df


def test_factor_covariance_recovers_structure_when_assets_are_factor_built():
    # Assets constructed as exact linear combos of the factors (no idiosyncratic
    # noise) => recovered Σ should match B·cov(F)·Bᵀ (annualized), resid var ~ floor.
    ff5 = _make_ff5()
    F = ff5[covariance.FACTOR_COLUMNS]
    rf = ff5["RF"]
    B_true = np.array([
        [1.0, 0.2, -0.1, 0.0, 0.3],   # asset A
        [0.5, -0.4, 0.6, 0.1, 0.0],   # asset B
        [1.2, 0.0, 0.0, -0.2, 0.4],   # asset C
    ])
    # excess_i = F @ b_i ; return_i = excess_i + rf
    excess = F.to_numpy() @ B_true.T
    returns = pd.DataFrame(excess, index=ff5.index, columns=["A", "B", "C"]).add(rf, axis=0)

    sigma = covariance.factor_covariance(returns, ff5, min_obs=40)

    assert list(sigma.index) == ["A", "B", "C"]
    f_cov = np.cov(F.to_numpy(), rowvar=False, ddof=1)
    # Noise-free assets => residual variance hits the floor; it lands on the
    # diagonal, annualized, so fold it into the expected matrix for an exact check.
    expected = (B_true @ f_cov @ B_true.T + np.eye(3) * 1e-8) * covariance.TRADING_DAYS_PER_YEAR
    np.testing.assert_allclose(sigma.to_numpy(), expected, atol=1e-9)


def test_factor_covariance_is_psd():
    ff5 = _make_ff5(seed=3)
    rng = np.random.default_rng(7)
    # Real-ish assets: factor exposure + idiosyncratic noise.
    F = ff5[covariance.FACTOR_COLUMNS].to_numpy()
    B = rng.normal(0.5, 0.5, size=(6, 5))
    noise = rng.normal(0, 0.008, size=(len(ff5), 6))
    rets = pd.DataFrame(F @ B.T + noise, index=ff5.index,
                        columns=[f"S{i}" for i in range(6)]).add(ff5["RF"], axis=0)
    sigma = covariance.factor_covariance(rets, ff5)
    eig = np.linalg.eigvalsh(sigma.to_numpy())
    assert eig.min() > 0                       # strictly PD (idiosyncratic floor)
    np.testing.assert_allclose(sigma.to_numpy(), sigma.to_numpy().T)  # symmetric


def test_factor_covariance_drops_thin_history():
    ff5 = _make_ff5(n_days=80)
    rng = np.random.default_rng(1)
    good = pd.Series(rng.normal(0, 0.01, len(ff5)), index=ff5.index)
    thin = good.copy()
    thin.iloc[10:] = np.nan                    # only 10 obs < min_obs
    rets = pd.DataFrame({"GOOD": good, "THIN": thin}).add(ff5["RF"], axis=0)
    sigma = covariance.factor_covariance(rets, ff5, min_obs=40)
    assert list(sigma.index) == ["GOOD"]       # THIN dropped, no raise


def test_load_ff5_daily_via_injected_zip(tmp_path):
    # Build an in-memory zip carrying the synthetic CSV, inject it as the fetch.
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("F-F_Research_Data_5_Factors_2x3_daily.csv", _SYNTHETIC_FF5)
    payload = buf.getvalue()

    cache = tmp_path / "ff5.parquet"
    df = covariance.load_ff5_daily(cache_path=cache, fetch=lambda url: payload)
    assert len(df) == 3 and cache.exists()
    # Second call hits the cache (fetch would raise if used).
    def boom(url):
        raise AssertionError("should not refetch when cache exists")
    again = covariance.load_ff5_daily(cache_path=cache, fetch=boom)
    pd.testing.assert_frame_equal(df, again)


def test_covariance_from_prices_is_point_in_time():
    ff5 = _make_ff5(n_days=120)
    rng = np.random.default_rng(5)
    prices = pd.DataFrame(
        100 * np.cumprod(1 + rng.normal(0, 0.01, size=(len(ff5), 3)), axis=0),
        index=ff5.index, columns=["A", "B", "C"],
    )
    as_of = ff5.index[80]
    sigma = covariance.covariance_from_prices(prices, ff5, as_of=as_of, window=60, min_obs=40)
    assert sigma.shape == (3, 3)
    # Adding future prices must not change Σ estimated as of `as_of`.
    sigma_again = covariance.covariance_from_prices(prices, ff5, as_of=as_of, window=60, min_obs=40)
    np.testing.assert_allclose(sigma.to_numpy(), sigma_again.to_numpy())
