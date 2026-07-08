"""Parity test for the first Strategy plugin (ADR-001 A2, wrap-in-place).

The plugin adapts the existing `compute_targets` into a `TargetBook`. These tests pin the mapping
as **lossless** (same objects, no reshaping) and the overlay spec as a faithful read of settings,
so that when `compute_targets` later relocates behind this boundary, the contract is locked. The
target math itself is exercised by the integration suite; here we inject a fake producer.
"""

from __future__ import annotations

from datetime import date
from types import SimpleNamespace

import pandas as pd
import pytest

from engine import strategy as S
from engine import strategies
from engine.strategies.low_beta_overwrite import LowBetaOverwrite


def _settings(mode="index"):
    return SimpleNamespace(
        covered_calls=SimpleNamespace(overlay_mode=mode, overwrite_coverage=1.0,
                                      target_delta=0.30, wing_delta=0.05),
        factors=SimpleNamespace(beta_market="SPY"))


def _fake_plan(weights=None):
    return SimpleNamespace(
        weights=weights if weights is not None else pd.Series({"AAPL": 0.5, "SPG": 0.5}),
        prices={"AAPL": 212.0, "SPG": 150.0},
        adv={"AAPL": 6e7}, spread={"AAPL": 0.0004},
        universe={"AAPL", "SPG"}, sector_map=pd.Series({"AAPL": "Tech", "SPG": "RE"}),
        panel=pd.DataFrame({"AAPL": [1.0, 2.0]}), sigma=pd.DataFrame({"AAPL": [0.1]}))


def _ctx(**kw):
    base = dict(settings=_settings(), db_engine=None)
    base.update(kw)
    return S.StrategyContext(**base)


@pytest.fixture(autouse=True)
def _isolate():
    S.clear()
    yield
    S.clear()


def test_generate_maps_targetplan_losslessly():
    plan = _fake_plan()
    strat = LowBetaOverwrite(targets_fn=lambda *a, **k: plan)
    book = strat.generate(_ctx(), date(2026, 7, 8))
    # Identity, not just equality — the wrap must not copy/reshape and risk divergence.
    assert book.weights is plan.weights
    assert book.inputs.prices is plan.prices and book.inputs.adv is plan.adv
    assert book.inputs.spread is plan.spread and book.inputs.universe is plan.universe
    assert book.inputs.sector_map is plan.sector_map
    assert book.inputs.panel is plan.panel and book.inputs.sigma is plan.sigma
    assert book.metadata["strategy"] == "low_beta_overwrite"


def test_overlay_spec_reflects_settings():
    strat = LowBetaOverwrite(targets_fn=lambda *a, **k: _fake_plan())
    book = strat.generate(_ctx(), date(2026, 7, 8))
    ov = book.overlay
    assert ov is not None and ov.mode == "index" and ov.market == "SPY"
    assert ov.params == {"coverage": 1.0, "target_delta": 0.30, "wing_delta": 0.05}


def test_generate_forwards_context_args_to_producer():
    seen = {}

    def spy(settings, as_of, **kw):
        seen["settings"] = settings
        seen["as_of"] = as_of
        seen["kw"] = kw
        return _fake_plan()

    ctx = _ctx(db_engine="ENG", prices_dir="/p", fundamentals_dir="/f")
    LowBetaOverwrite(targets_fn=spy).generate(ctx, date(2026, 7, 8))
    assert seen["as_of"] == date(2026, 7, 8) and seen["settings"] is ctx.settings
    assert seen["kw"] == {"db_engine": "ENG", "prices_dir": "/p", "fundamentals_dir": "/f"}


def test_unset_context_dirs_are_not_forwarded():
    # Empty dirs must not override compute_targets' own defaults.
    seen = {}
    LowBetaOverwrite(targets_fn=lambda s, d, **kw: seen.update(kw) or _fake_plan()) \
        .generate(_ctx(), date(2026, 7, 8))
    assert seen == {"db_engine": None}      # no prices_dir / fundamentals_dir keys


def test_empty_plan_maps_to_empty_book():
    strat = LowBetaOverwrite(targets_fn=lambda *a, **k: _fake_plan(weights=pd.Series(dtype=float)))
    book = strat.generate(_ctx(), date(2026, 7, 8))
    assert book.is_empty is True


def test_register_all_is_idempotent_and_registers_the_plugin():
    strategies.register_all()
    strategies.register_all()                # no collision on the second call
    assert S.is_registered("low_beta_overwrite")
    assert isinstance(S.get("low_beta_overwrite"), LowBetaOverwrite)


def test_default_targets_fn_is_the_production_compute_targets():
    # The wrap points at the real producer (heavier import; confirms the A2 delegation target).
    from engine.strategies.low_beta_overwrite import _default_targets_fn
    from scripts.run_eod import compute_targets
    assert _default_targets_fn() is compute_targets
