"""Unit tests for engine.strategy — the plugin interface + registry (ADR-001 / D37, increment A1).

Pure interface, no I/O: the boundary types (TargetBook / PlanInputs / OverlaySpec /
StrategyContext) and the name-keyed registry. Nothing here touches run_cycle yet.
"""

from __future__ import annotations

from datetime import date
from types import SimpleNamespace

import pandas as pd
import pytest

from engine import strategy as S


class _Dummy:
    """A minimal conforming strategy: constant weights, optional overlay."""
    def __init__(self, name="dummy", weights=None, overlay=None):
        self.name = name
        self._weights = weights if weights is not None else pd.Series({"AAPL": 0.5, "MSFT": 0.5})
        self._overlay = overlay

    def generate(self, ctx, as_of):
        return S.TargetBook(weights=self._weights,
                            inputs=S.PlanInputs(prices={"AAPL": 100.0, "MSFT": 200.0}),
                            overlay=self._overlay, metadata={"as_of": str(as_of)})


@pytest.fixture(autouse=True)
def _isolate_registry():
    S.clear()
    yield
    S.clear()


# ---- boundary types --------------------------------------------------------- #
def test_targetbook_defaults_and_is_empty():
    empty = S.TargetBook(weights=pd.Series(dtype=float))
    assert empty.is_empty is True
    assert empty.overlay is None and empty.metadata == {}
    assert isinstance(empty.inputs, S.PlanInputs) and empty.inputs.panel is None

    book = S.TargetBook(weights=pd.Series({"AAPL": 1.0}))
    assert book.is_empty is False


def test_plan_inputs_mirror_targetplan_fields():
    # The fields the platform needs downstream — same payload as the legacy run_eod.TargetPlan,
    # so the current strategy pours in without reshaping (A2).
    pi = S.PlanInputs()
    for f in ("prices", "adv", "spread", "universe", "sector_map", "panel", "sigma"):
        assert hasattr(pi, f)
    assert pi.prices == {} and pi.universe == set()
    assert isinstance(pi.sector_map, pd.Series)


def test_overlay_spec_is_frozen_declarative():
    ov = S.OverlaySpec(mode="index", market="SPY", params={"coverage": 1.0, "wing_delta": 0.05})
    assert ov.mode == "index" and ov.market == "SPY" and ov.params["wing_delta"] == 0.05
    with pytest.raises(Exception):        # frozen — specs don't mutate after a strategy emits one
        ov.mode = "per_name"              # type: ignore[misc]


def test_strategy_context_carries_read_surfaces_only():
    ctx = S.StrategyContext(settings=SimpleNamespace(x=1), db_engine=None,
                            prices_dir="/p", fundamentals_dir="/f", capital_base=2_000_000.0)
    assert ctx.capital_base == 2_000_000.0 and ctx.positions == {}
    assert not hasattr(ctx, "broker")     # strategies decide, they don't trade


# ---- registry --------------------------------------------------------------- #
def test_register_get_and_all():
    d = _Dummy()
    assert S.register(d) is d              # returns the object (decorator-friendly)
    assert S.get("dummy") is d
    assert S.is_registered("dummy") and list(S.all_strategies()) == ["dummy"]
    assert S.all_strategies() is not S._REGISTRY   # a copy, not the live dict


def test_register_same_object_twice_is_idempotent():
    d = _Dummy()
    S.register(d)
    S.register(d)                         # no raise
    assert len(S.all_strategies()) == 1


def test_register_name_collision_raises():
    S.register(_Dummy(name="clash"))
    with pytest.raises(ValueError, match="already registered"):
        S.register(_Dummy(name="clash"))  # different object, same name


def test_register_rejects_non_strategy():
    with pytest.raises(TypeError):
        S.register(object())              # no name / generate
    with pytest.raises(TypeError):
        S.register(SimpleNamespace(name="x"))   # name but no generate


def test_get_unknown_lists_known():
    S.register(_Dummy(name="alpha"))
    with pytest.raises(KeyError, match="alpha"):
        S.get("nope")


def test_runtime_checkable_protocol_matches_duck_type():
    assert isinstance(_Dummy(), S.Strategy)
    assert not isinstance(object(), S.Strategy)


# ---- the contract end to end ------------------------------------------------ #
def test_generate_returns_a_targetbook():
    ov = S.OverlaySpec(mode="index")
    S.register(_Dummy(name="withov", overlay=ov))
    strat = S.get("withov")
    ctx = S.StrategyContext(settings=SimpleNamespace())
    book = strat.generate(ctx, date(2026, 7, 8))
    assert isinstance(book, S.TargetBook)
    assert book.weights.sum() == 1.0 and book.overlay is ov
    assert book.inputs.prices["AAPL"] == 100.0
    assert book.metadata["as_of"] == "2026-07-08"
