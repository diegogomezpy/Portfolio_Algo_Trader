"""Unit tests for engine.config_strategy — the declarative on-the-fly strategy (ADR-001 north star).

Synthetic data injected via load_data; asserts the spec drives signal selection, construction
(top-N / caps / leverage), overlay passthrough, and that it satisfies the Strategy protocol.
"""

from __future__ import annotations

from datetime import date
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

from engine import config_strategy as CS
from engine import signals, strategy as S


@pytest.fixture(autouse=True)
def _iso():
    signals.clear(); yield; signals.clear()


def _settings():
    return SimpleNamespace(factors=SimpleNamespace(
        winsor_pct=0.01, beta_window=4, beta_market="SPY", vol_window=4, report_lag_days=45))


def _panel(syms, n=6):
    idx = pd.bdate_range("2026-01-01", periods=n)
    rng = np.random.default_rng(3)
    data = {"SPY": 100 * np.cumprod(1 + rng.normal(0.001, 0.01, n))}
    for i, s in enumerate(syms):
        data[s] = 100 * np.cumprod(1 + rng.normal(0.001, 0.008 + 0.004 * i, n))
    return pd.DataFrame(data, index=idx)


def _loader(panel, universe, fpit=None):
    return lambda ctx, as_of, needs: (panel, fpit, universe)


def test_construct_weights_topn_caps_and_normalizes():
    comp = pd.Series({"A": 3.0, "B": 2.0, "C": 1.0, "D": 0.5, "E": -1.0})
    spec = CS.StrategySpec(name="x", signals={"low_vol": 1.0}, max_names=3, max_weight=0.5)
    w = CS.construct_weights(comp, spec)
    assert set(w.index) == {"A", "B", "C"}                   # top 3 by score, E (negative) excluded
    assert w.max() <= 0.5 + 1e-9                             # per-name cap (fraction of base)
    assert w.sum() == pytest.approx(1.0)                     # feasible cap here → fully invested
    assert w["A"] > w["C"]                                   # score-weighted


def test_construct_weights_respects_cap_and_holds_cash_when_binding():
    # Too few names for the cap: 3 names @ 5% can't reach 100% — never breach the cap; hold cash.
    comp = pd.Series({"A": 3.0, "B": 2.0, "C": 1.0})
    spec = CS.StrategySpec(name="x", signals={"low_vol": 1.0}, max_names=3, max_weight=0.05)
    w = CS.construct_weights(comp, spec)
    assert (w <= 0.05 + 1e-9).all()                          # cap never breached (the risk-gate bug)
    assert w.sum() == pytest.approx(0.15)                    # 3 × 5% → 15% invested, 85% cash


def test_construct_weights_empty_when_nothing_qualifies():
    comp = pd.Series({"A": -1.0, "B": -2.0})
    spec = CS.StrategySpec(name="x", signals={"low_vol": 1.0}, min_score=0.0)
    assert CS.construct_weights(comp, spec).empty


def test_generate_uses_selected_signals_only():
    syms = ["AAA", "BBB", "CCC", "DDD"]
    panel = _panel(syms)
    spec = CS.StrategySpec(name="trend_test", signals={"low_vol": 0.6, "low_beta": 0.4},
                           max_names=2, max_weight=0.5, leverage=1.0)   # 2 @ 50% → feasible, fully invested
    strat = CS.ConfigStrategy(spec, load_data=_loader(panel, syms))
    book = strat.generate(S.StrategyContext(settings=_settings()), panel.index[-1].date())
    assert isinstance(book, S.TargetBook) and not book.is_empty
    assert len(book.weights) <= 2 and book.weights.sum() == pytest.approx(1.0)
    assert set(book.weights.index) <= set(syms)
    assert all(s in book.inputs.prices for s in book.weights.index)     # prices from the panel
    assert book.metadata["kind"] == "config" and book.metadata["signals"] == {"low_vol": 0.6, "low_beta": 0.4}


def test_generate_carries_overlay_spec():
    syms = ["AAA", "BBB"]
    ov = S.OverlaySpec(mode="index", market="SPY")
    spec = CS.StrategySpec(name="ov_test", signals={"low_vol": 1.0}, overlay=ov)
    strat = CS.ConfigStrategy(spec, load_data=_loader(_panel(syms), syms))
    book = strat.generate(S.StrategyContext(settings=_settings()), date(2026, 1, 8))
    assert book.overlay is ov


def test_config_strategy_satisfies_the_strategy_protocol():
    spec = CS.StrategySpec(name="proto", signals={"low_vol": 1.0})
    strat = CS.from_spec(spec, load_data=_loader(_panel(["AAA"]), ["AAA"]))
    assert isinstance(strat, S.Strategy) and strat.name == "proto"


def test_fundamentals_signal_triggers_fundamental_load():
    seen = {}
    def loader(ctx, as_of, needs):
        seen["needs"] = needs
        return _panel(["AAA", "BBB"]), pd.DataFrame(
            {"pe_ratio": [10.0, 20.0], "pb_ratio": [1.0, 2.0], "roe": [0.2, 0.1],
             "gross_margin": [0.4, 0.3]}, index=["AAA", "BBB"]), ["AAA", "BBB"]
    spec = CS.StrategySpec(name="q", signals={"quality": 0.5, "value": 0.5})
    CS.ConfigStrategy(spec, load_data=loader).generate(S.StrategyContext(settings=_settings()),
                                                       date(2026, 1, 8))
    assert seen["needs"] == {"fundamentals"}                 # only fundamental signals selected
