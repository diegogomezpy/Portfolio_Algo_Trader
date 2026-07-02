"""Unit tests for engine.factors — synthetic frames, no disk or Postgres.

Covers the Phase 1 gate criteria: robust unit-variance z-scores, the earnings/book
yield value definition (loss-makers must not score as "cheap"), neutral-fill +
stale-flag for missing fundamentals, correct 12-1 momentum offsets, and — most
importantly — **no look-ahead**: a quarter's fundamentals are unusable until
``report_date + report_lag_days`` has passed (report_date is quarter-end, not the
filing date).
"""

from __future__ import annotations

from datetime import date
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

from engine import factors


def make_settings(*, winsor_pct=0.01, beta_w=4, vol_w=4, beta_market="SPY",
                  min_price=5.0, min_adv=1_000_000):
    return SimpleNamespace(
        universe=SimpleNamespace(min_price=min_price, min_adv_usd=min_adv),
        factors=SimpleNamespace(
            winsor_pct=winsor_pct,
            beta_window=beta_w,
            beta_market=beta_market,
            vol_window=vol_w,
            report_lag_days=45,
            weights=SimpleNamespace(quality=0.25, value=0.25, low_beta=0.25, low_vol=0.25),
        ),
    )


def flat_panel(symbols, n=6, price=100.0):
    """Constant-price panel — momentum and vol collapse to neutral, isolating
    the fundamental factors in end-to-end tests."""
    idx = pd.bdate_range("2026-01-01", periods=n)
    return pd.DataFrame({s: [price] * n for s in symbols}, index=idx)


# ---------------------------------------------------------------- zscore
def test_zscore_unit_variance_and_outlier_robust():
    # Bulk near 0..1 with one extreme outlier; σ-clipping would let the outlier
    # crush everyone — percentile winsorization must keep std ≈ 1.
    s = pd.Series([0, 0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 1.0, 500.0])
    z = factors.zscore(s, winsor_pct=0.05)
    assert abs(z.mean()) < 1e-9
    assert 0.9 < z.std(ddof=0) < 1.1
    assert z.max() < 5  # the 500 outlier is winsorized, not left at +30σ


def test_zscore_preserves_nan():
    s = pd.Series([1.0, 2.0, np.nan, 4.0])
    z = factors.zscore(s, winsor_pct=0.01)
    assert z.isna().tolist() == [False, False, True, False]


def test_zscore_constant_is_neutral_not_nan():
    s = pd.Series([3.0, 3.0, 3.0])
    z = factors.zscore(s, winsor_pct=0.01)
    assert (z == 0.0).all()  # degenerate spread → neutral, never div-by-zero


# ---------------------------------------------------------- double-z combine
def test_double_z_neutral_fills_one_missing_metric():
    # AAA missing metric B still gets scored off metric A; not dropped to NaN.
    z_a = pd.Series({"AAA": 2.0, "BBB": -1.0, "CCC": 0.0})
    z_b = pd.Series({"AAA": np.nan, "BBB": -1.0, "CCC": 1.0})
    # combined (neutral-filled): AAA=2, BBB=-2, CCC=1 → AAA ranks above CCC.
    out = factors._double_z(z_a, z_b, winsor_pct=0.01)
    assert out.notna().all()
    assert out["AAA"] > out["CCC"]  # AAA's strong A survives its missing B


# --------------------------------------------------------------- low-beta
def _px_from_rets(rets, p0=100.0):
    p = [p0]
    for r in rets:
        p.append(p[-1] * (1 + r))
    return p


def test_market_beta_matches_ols():
    # AAA returns = 2×SPY (β=2), BBB = −1×SPY (β=−1), the market itself β=1.
    idx = pd.bdate_range("2026-01-01", periods=6)
    spy_r = [0.01, -0.02, 0.03, -0.01, 0.02]
    panel = pd.DataFrame({"SPY": _px_from_rets(spy_r),
                          "AAA": _px_from_rets([2 * r for r in spy_r]),
                          "BBB": _px_from_rets([-r for r in spy_r])}, index=idx)
    b = factors.market_beta(panel, idx[-1].date(), window=5, market="SPY")
    assert b["AAA"] == pytest.approx(2.0, abs=1e-6)
    assert b["BBB"] == pytest.approx(-1.0, abs=1e-6)
    assert b["SPY"] == pytest.approx(1.0, abs=1e-6)


def test_market_beta_thin_history_is_nan():
    idx = pd.bdate_range("2026-01-01", periods=3)
    panel = pd.DataFrame({"SPY": [100.0, 101, 102], "AAA": [10.0, 11, 12]}, index=idx)
    b = factors.market_beta(panel, idx[-1].date(), window=10, market="SPY")  # 2 obs < 80% of 10
    assert b["AAA"] != b["AAA"]  # NaN


def test_market_beta_no_market_column_is_nan():
    idx = pd.bdate_range("2026-01-01", periods=6)
    panel = pd.DataFrame({"AAA": [1.0, 2, 3, 4, 5, 6]}, index=idx)
    b = factors.market_beta(panel, idx[-1].date(), window=5, market="SPY")
    assert b.isna().all()  # no market series → nothing to regress against


# ----------------------------------------------------- point-in-time / look-ahead
def _three_quarters():
    return pd.DataFrame({
        "symbol": ["AAA", "AAA", "AAA"],
        "pe_ratio": [10.0, 11.0, 12.0],
        "pb_ratio": [1.0, 1.0, 1.0],
        "roe": [0.1, 0.1, 0.1],
        "gross_margin": [0.3, 0.3, 0.3],
        "report_date": pd.to_datetime(["2025-12-31", "2026-03-31", "2026-06-30"]),
    })


def test_point_in_time_excludes_unreported_quarter():
    allf = _three_quarters()
    # 2026-03-01: Q4'25 (report+45d = 2026-02-14) is public; Q1'26 (2026-05-15) is NOT.
    pit = factors.point_in_time_fundamentals(allf, date(2026, 3, 1), lag_days=45)
    assert pit.loc["AAA", "pe_ratio"] == 10.0


def test_point_in_time_picks_latest_eligible_quarter():
    allf = _three_quarters()
    # 2026-05-20: Q1'26 now public (>= 2026-05-15), Q2'26 not yet.
    pit = factors.point_in_time_fundamentals(allf, date(2026, 5, 20), lag_days=45)
    assert pit.loc["AAA", "pe_ratio"] == 11.0


def test_point_in_time_empty_before_first_report():
    allf = _three_quarters()
    pit = factors.point_in_time_fundamentals(allf, date(2026, 1, 1), lag_days=45)
    assert pit.empty


# ------------------------------------------------------------- value semantics
def test_value_loss_maker_scores_low_not_cheap():
    """The inversion trap: a negative-P/E loss-maker must rank at the bottom of
    Value (E/P < 0), never as 'cheap'. A genuinely cheap low-P/E name ranks top."""
    symbols = ["CHEAP", "MID", "RICH", "LOSS", "LOSS2"]
    snap = pd.DataFrame(
        {"close": [100.0] * 5, "adv_20d": [5_000_000] * 5}, index=symbols
    )
    fpit = pd.DataFrame({
        "pe_ratio": [5.0, 15.0, 40.0, -10.0, -3.0],
        "pb_ratio": [1.0, 2.0, 5.0, 3.0, 4.0],
        "roe": [0.1, 0.1, 0.1, 0.1, 0.1],
        "gross_margin": [0.3, 0.3, 0.3, 0.3, 0.3],
    }, index=symbols)
    panel = flat_panel(symbols)
    scores = factors.compute_factor_scores(
        panel.index[-1].date(), settings=make_settings(),
        price_panel=panel, fundamentals_pit=fpit, universe_snapshot=snap,
    ).set_index("symbol")
    assert scores.loc["CHEAP", "value_score"] == scores["value_score"].max()
    assert scores.loc["LOSS", "value_score"] < scores.loc["RICH", "value_score"]


# --------------------------------------------------------- end-to-end compute
def test_compute_applies_universe_filter_and_stale_flag():
    symbols = ["KEEP", "CHEAPPRICE", "THINADV", "NOFUND"]
    snap = pd.DataFrame({
        "close":  [100.0, 2.0, 100.0, 100.0],     # CHEAPPRICE below min_price
        "adv_20d": [5e6, 5e6, 1e5, 5e6],           # THINADV below min_adv
    }, index=symbols)
    fpit = pd.DataFrame({
        "pe_ratio": [10.0, 10.0, 10.0],
        "pb_ratio": [1.0, 1.0, 1.0],
        "roe": [0.1, 0.1, 0.1],
        "gross_margin": [0.3, 0.3, 0.3],
    }, index=["KEEP", "CHEAPPRICE", "THINADV"])  # NOFUND has no fundamentals row
    panel = flat_panel(symbols)
    scores = factors.compute_factor_scores(
        panel.index[-1].date(), settings=make_settings(),
        price_panel=panel, fundamentals_pit=fpit, universe_snapshot=snap,
    ).set_index("symbol")

    assert set(scores.index) == {"KEEP", "NOFUND"}        # filter dropped 2
    assert bool(scores.loc["NOFUND", "stale"]) is True     # missing fundamentals
    assert bool(scores.loc["KEEP", "stale"]) is False
    assert scores["composite_score"].notna().all()         # neutral-filled, never NaN


def test_compute_restricts_to_eligible_symbols():
    # D28: only US-GAAP filers tradable; the ADR is excluded even though it's liquid.
    symbols = ["USGAAP", "ADR"]
    snap = pd.DataFrame({"close": [100.0, 100.0], "adv_20d": [5e6, 5e6]}, index=symbols)
    fpit = pd.DataFrame({"pe_ratio": [10.0, 0.02], "pb_ratio": [1.0, 0.01],
                         "roe": [0.1, 0.02], "gross_margin": [0.3, 0.6]}, index=symbols)
    panel = flat_panel(symbols)
    scores = factors.compute_factor_scores(
        panel.index[-1].date(), settings=make_settings(),
        price_panel=panel, fundamentals_pit=fpit, universe_snapshot=snap,
        eligible_symbols={"USGAAP"},
    ).set_index("symbol")
    assert set(scores.index) == {"USGAAP"}                 # ADR excluded from the universe


def test_load_all_fundamentals_filters_by_source(tmp_path):
    df = pd.DataFrame({
        "pe_ratio": [10.0, 0.02], "pb_ratio": [1.0, 0.01], "roe": [0.1, 0.02],
        "gross_margin": [0.3, 0.6], "report_date": ["2024-01-15", "2024-01-15"],
        "source": ["sec_edgar", "yfinance"],
    }, index=pd.Index(["USGAAP", "ADR"], name="symbol"))
    df.to_parquet(tmp_path / "2024-Q1.parquet")
    edgar = factors.load_all_fundamentals(tmp_path, source="sec_edgar")
    assert list(edgar["symbol"]) == ["USGAAP"]             # yfinance row dropped
    assert set(factors.load_all_fundamentals(tmp_path)["symbol"]) == {"USGAAP", "ADR"}


def test_loaders_skip_macos_dotfiles(tmp_path):
    """A macOS '._' AppleDouble shadow (or .DS_Store) copied in alongside the real Parquet — e.g.
    after `scp`-ing data/ up from a Mac — must be ignored, not parsed as a date or read as Parquet.
    Otherwise one junk file crashes the whole rebalance (the go-live '._2020-07-27' bug)."""
    eq = tmp_path / "equities"; eq.mkdir()
    pd.DataFrame({"close": [101.0]}, index=pd.Index(["AAPL"], name="symbol")).to_parquet(eq / "2026-06-25.parquet")
    (eq / "._2026-06-25.parquet").write_bytes(b"\x00\x05\x16\x07Mac OS X")   # AppleDouble junk, not Parquet
    (eq / ".DS_Store").write_bytes(b"junk")
    panel = factors.load_close_panel(eq, end=date(2026, 6, 25), lookback=10)
    assert list(panel.columns) == ["AAPL"] and len(panel) == 1               # real file loaded, junk skipped

    fund = tmp_path / "fundamentals"; fund.mkdir()
    pd.DataFrame({"pe_ratio": [15.0], "pb_ratio": [2.0], "roe": [0.2], "gross_margin": [0.4],
                  "report_date": ["2020-03-31"]}, index=pd.Index(["AAPL"], name="symbol")).to_parquet(fund / "2020-Q1.parquet")
    (fund / "._2020-Q1.parquet").write_bytes(b"\x00\x05\x16\x07Mac OS X")
    allf = factors.load_all_fundamentals(fund)
    assert set(allf["symbol"]) == {"AAPL"} and len(allf) == 1                # shadow skipped, real file read
