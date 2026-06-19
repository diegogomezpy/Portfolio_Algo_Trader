"""Integration test for the Phase 2 walk-forward backtest.

Runs the real pipeline (factors → covariance → optimizer → return accounting) over
a short window on the actual data store, asserting structural invariants and that
the gate summary computes. Skipped if the data store or reference caches are absent
(e.g. CI without a backfill).
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pandas as pd
import pytest

from engine.config import load_settings
from scripts import backtest as bt

_REQUIRED = [
    Path("data/raw/equities"), Path("data/raw/fundamentals"),
    Path("data/ref/ff5_daily.parquet"), Path("data/ref/sectors.parquet"),
]


def _data_available() -> bool:
    for p in _REQUIRED:
        if p.is_dir() and not any(p.glob("*.parquet")):
            return False
        if not p.exists():
            return False
    return True


pytestmark = pytest.mark.skipif(not _data_available(),
                                reason="backtest data store / reference caches not present")


@pytest.fixture(scope="module")
def results() -> pd.DataFrame:
    # Short window: a handful of rebalances after the momentum burn-in.
    return bt.run_walkforward(date(2024, 1, 1), date(2024, 6, 1), settings=load_settings())


def test_produces_rows_with_expected_schema(results):
    assert len(results) >= 2
    for col in ["date", "n_names", "status", "gross_return", "cost", "net_return",
                "turnover", "benchmark_return", "quality", "value", "momentum", "lowvol"]:
        assert col in results.columns


def test_return_accounting_is_consistent(results):
    # net = gross − cost, costs non-negative, turnover a valid one-way fraction.
    assert ((results["net_return"] - (results["gross_return"] - results["cost"])).abs() < 1e-12).all()
    assert (results["cost"] >= 0).all()
    assert results["turnover"].between(0, 1).all()
    assert results["net_return"].notna().all()


def test_book_is_sized_and_feasible(results):
    settings = load_settings()
    assert (results["n_names"] > 0).all()
    assert (results["n_names"] <= settings.optimizer.preselect_top_k).all()
    assert results["status"].isin({"optimal", "relaxed", "held"}).all()


def test_summary_and_gate_compute(results):
    summary = bt.summarize(results)
    assert set(summary["sleeves"]) == {"quality", "value", "momentum", "lowvol"}
    assert isinstance(summary["passed"], bool)
    assert summary["n_months"] == len(results)
