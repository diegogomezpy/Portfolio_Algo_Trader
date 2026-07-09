"""Unit tests for engine.formula — the sandboxed "mix how I want" expression engine.

Two things matter: (1) it computes the arithmetic/transform vocabulary correctly over per-symbol
Series, and (2) it is a SANDBOX — every escape hatch (attribute access, imports, subscripting,
lambdas, comprehensions, unknown names/functions) must fail to validate, not execute.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from engine import formula as F


def _ns():
    idx = ["A", "B", "C", "D"]
    return {
        "quality": pd.Series([2.0, 1.0, -1.0, np.nan], index=idx),
        "value": pd.Series([1.0, -1.0, 0.5, 2.0], index=idx),
        "raw_pe": pd.Series([10.0, 20.0, 0.0, 5.0], index=idx),
        "raw_beta": pd.Series([1.2, 0.8, 1.5, 0.5], index=idx),
    }


# --------------------------- arithmetic + transforms --------------------------- #
def test_weighted_blend_matches_manual():
    ns = _ns()
    out = F.evaluate("0.6*quality + 0.4*value", ns)
    exp = 0.6 * ns["quality"] + 0.4 * ns["value"]
    pd.testing.assert_series_equal(out, exp)


def test_subtraction_and_unary_minus():
    ns = _ns()
    pd.testing.assert_series_equal(F.evaluate("-raw_beta", ns), -ns["raw_beta"])
    pd.testing.assert_series_equal(F.evaluate("value - quality", ns), ns["value"] - ns["quality"])


def test_z_is_zero_mean_unit_var():
    out = F.evaluate("z(raw_beta)", _ns())
    assert abs(float(out.mean())) < 1e-9 and abs(float(out.std(ddof=0)) - 1.0) < 1e-9


def test_rank_and_clip_and_functions():
    ns = _ns()
    r = F.evaluate("rank(raw_beta)", ns)
    assert r["D"] == r.min() and r["C"] == r.max()          # lowest beta → lowest pct-rank
    c = F.evaluate("clip(value, 0, 1)", ns)
    assert c.max() <= 1.0 and c.min() >= 0.0
    pd.testing.assert_series_equal(F.evaluate("abs(value)", ns), ns["value"].abs(), check_dtype=False)


def test_where_selects_elementwise():
    ns = _ns()
    out = F.evaluate("where(raw_beta < 1.0, value, 0)", ns)
    assert out["B"] == pytest.approx(ns["value"]["B"])       # beta 0.8 < 1 → value
    assert out["A"] == pytest.approx(0.0)                    # beta 1.2 → 0


def test_division_by_zero_becomes_nan_not_inf():
    out = F.evaluate("1 / raw_pe", _ns())                    # raw_pe['C'] == 0
    assert np.isnan(out["C"]) and not np.isinf(out).any()


def test_scalar_only_formula_is_rejected():
    with pytest.raises(F.FormulaError):
        F.evaluate("1 + 2", _ns())


# --------------------------- validation + introspection ------------------------ #
def test_referenced_names_excludes_functions():
    assert F.referenced_names("z(quality) + 0.5*raw_pe") == {"quality", "raw_pe"}


def test_validate_returns_referenced_vocab_and_flags_unknowns():
    assert F.validate("quality + value", {"quality", "value", "raw_pe"}) == {"quality", "value"}
    with pytest.raises(F.FormulaError, match="unknown name"):
        F.validate("quality + nope", {"quality", "value"})
    with pytest.raises(F.FormulaError, match="unknown function"):
        F.validate("frobnicate(quality)", {"quality"})


@pytest.mark.parametrize("expr", [
    "__import__('os').system('echo hi')",   # import via builtins
    "quality.__class__",                     # attribute access
    "().__class__.__bases__",                # dunder walk
    "value[0]",                              # subscript
    "(lambda: 1)()",                         # lambda
    "[x for x in value]",                    # comprehension
    "quality if value else 0",               # conditional expr
    "'string'",                              # non-numeric literal
    "quality < value < 1",                   # chained comparison
    "open('x')",                             # unknown/dangerous call
])
def test_sandbox_rejects_escapes(expr):
    with pytest.raises(F.FormulaError):
        F.evaluate(expr, _ns())


def test_function_specs_shape():
    specs = {f["name"]: f for f in F.function_specs()}
    assert "z" in specs and specs["z"]["sig"] and specs["z"]["desc"]
    assert "where" in specs and "clip" in specs
