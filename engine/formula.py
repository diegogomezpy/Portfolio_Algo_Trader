"""Sandboxed formula engine — "mix the inputs how I want" (ADR-001 / D37 north star).

A **formula** is a plain arithmetic expression over the composition vocabulary (:mod:`engine.signals`):
z-scored signals referenced by their bare name (``quality``, ``low_beta``, …), raw fields referenced
by their ``raw_*`` name (``raw_pe``, ``raw_beta``, …), a set of transform functions (``z``, ``rank``,
``winsor``, ``clip``, ``log``, …), numeric literals, and the operators ``+ - * / ** %`` plus the
comparisons that feed ``where(cond, a, b)``. It evaluates to one composite score per symbol — exactly
what :mod:`engine.config_strategy` then constructs a book from.

**It is NOT ``eval``.** The expression is parsed to an AST and walked with a strict allow-list: only
the node types below are permitted, ``Name`` nodes must resolve to a vocabulary variable, and ``Call``
targets must be one of the whitelisted functions. There is no attribute access, no subscripting, no
comprehensions, no lambdas, no dunder — so ``().__class__`` / ``__import__`` / ``os.system`` and every
other escape simply fail to parse-validate. Values are pandas Series / numbers only.
"""

from __future__ import annotations

import ast
from typing import Iterable, Mapping

import numpy as np
import pandas as pd

from engine.factors import zscore


class FormulaError(ValueError):
    """A formula that won't parse, references an unknown name, or uses a disallowed construct."""


# --------------------------------------------------------------------------- #
# Transform functions available inside a formula. Each takes/returns Series (or
# a scalar) and is cross-sectional (operates over the symbol axis).
# --------------------------------------------------------------------------- #
def _idx_of(*vals) -> pd.Index | None:
    for v in vals:
        if isinstance(v, pd.Series):
            return v.index
    return None


def _as_array(v, index: pd.Index):
    if isinstance(v, pd.Series):
        return v.reindex(index).to_numpy(dtype=float)
    return np.full(len(index), float(v))


def _z(x, p: float = 0.01) -> pd.Series:
    """Winsorized cross-sectional z-score — the same robust standardization the factors use."""
    if not isinstance(x, pd.Series):
        raise FormulaError("z() needs a series argument")
    return zscore(x, float(p))


def _rank(x) -> pd.Series:
    """Cross-sectional percentile rank in [0, 1] (NaNs preserved)."""
    if not isinstance(x, pd.Series):
        raise FormulaError("rank() needs a series argument")
    return x.rank(pct=True)


def _winsor(x, p: float = 0.01) -> pd.Series:
    if not isinstance(x, pd.Series):
        raise FormulaError("winsor() needs a series argument")
    v = x.dropna()
    if len(v) < 2:
        return x
    return x.clip(v.quantile(float(p)), v.quantile(1.0 - float(p)))


def _clip(x, lo, hi):
    return x.clip(lo, hi) if isinstance(x, pd.Series) else float(np.clip(x, lo, hi))


def _where(cond, a, b) -> pd.Series:
    """Elementwise select: where ``cond`` is true take ``a`` else ``b`` (either may be scalar)."""
    if not isinstance(cond, pd.Series):
        raise FormulaError("where() needs a series condition")
    idx = cond.index
    return pd.Series(np.where(cond.fillna(False).to_numpy(), _as_array(a, idx), _as_array(b, idx)),
                     index=idx)


def _binary_np(fn):
    def f(a, b):
        idx = _idx_of(a, b)
        if idx is None:
            return float(fn(a, b))
        return pd.Series(fn(_as_array(a, idx), _as_array(b, idx)), index=idx)
    return f


def _unary_np(fn):
    def f(x):
        return pd.Series(fn(x.to_numpy(dtype=float)), index=x.index) if isinstance(x, pd.Series) \
            else float(fn(x))
    return f


FUNCTIONS = {
    "z": _z,
    "rank": _rank,
    "winsor": _winsor,
    "clip": _clip,
    "where": _where,
    "log": _unary_np(np.log),
    "log1p": _unary_np(np.log1p),
    "sqrt": _unary_np(np.sqrt),
    "abs": _unary_np(np.abs),
    "sign": _unary_np(np.sign),
    "minimum": _binary_np(np.minimum),
    "maximum": _binary_np(np.maximum),
}

_FUNCTION_SIGS = {
    "z": ("z(x, p=0.01)", "winsorized cross-sectional z-score — higher keeps the sign of x"),
    "rank": ("rank(x)", "cross-sectional percentile rank in [0, 1]"),
    "winsor": ("winsor(x, p=0.01)", "clip x to its [p, 1−p] quantiles (no standardize)"),
    "clip": ("clip(x, lo, hi)", "clip x to [lo, hi]"),
    "where": ("where(cond, a, b)", "elementwise: a where cond is true, else b"),
    "log": ("log(x)", "natural log"),
    "log1p": ("log1p(x)", "log(1 + x)"),
    "sqrt": ("sqrt(x)", "square root"),
    "abs": ("abs(x)", "absolute value"),
    "sign": ("sign(x)", "−1 / 0 / +1"),
    "minimum": ("minimum(a, b)", "elementwise min"),
    "maximum": ("maximum(a, b)", "elementwise max"),
}


def function_specs() -> list[dict]:
    """Function palette for the builder's formula editor."""
    return [{"name": n, "sig": sig, "desc": desc} for n, (sig, desc) in _FUNCTION_SIGS.items()]


# --------------------------------------------------------------------------- #
# Safe AST walk.
# --------------------------------------------------------------------------- #
_BINOPS = {
    ast.Add: lambda a, b: a + b, ast.Sub: lambda a, b: a - b,
    ast.Mult: lambda a, b: a * b, ast.Div: lambda a, b: a / b,
    ast.Pow: lambda a, b: a ** b, ast.Mod: lambda a, b: a % b,
}
_CMPOPS = {
    ast.Lt: lambda a, b: a < b, ast.Gt: lambda a, b: a > b,
    ast.LtE: lambda a, b: a <= b, ast.GtE: lambda a, b: a >= b,
    ast.Eq: lambda a, b: a == b, ast.NotEq: lambda a, b: a != b,
}
# Node types the walker understands. Anything else (Attribute, Subscript, Lambda, comprehensions,
# BoolOp, Starred, …) is rejected — that's the sandbox.
_ALLOWED = (ast.Expression, ast.BinOp, ast.UnaryOp, ast.USub, ast.UAdd, ast.Call, ast.Name,
            ast.Load, ast.Constant, ast.Compare) + tuple(_BINOPS) + tuple(_CMPOPS)


def _parse(expr: str) -> ast.Expression:
    if not expr or not str(expr).strip():
        raise FormulaError("empty formula")
    try:
        return ast.parse(str(expr), mode="eval")
    except SyntaxError as exc:
        raise FormulaError(f"syntax error: {exc.msg}") from None


def _walk_validate(node: ast.AST, allowed_names: set[str]) -> None:
    if not isinstance(node, _ALLOWED):
        raise FormulaError(f"disallowed expression: {type(node).__name__.lower()}")
    if isinstance(node, ast.Constant) and not isinstance(node.value, (int, float)) \
            or isinstance(node, ast.Constant) and isinstance(node.value, bool):
        raise FormulaError("only numeric literals are allowed")
    if isinstance(node, ast.Compare) and len(node.ops) != 1:
        raise FormulaError("chained comparisons are not allowed")
    if isinstance(node, ast.Call):
        if not isinstance(node.func, ast.Name) or node.func.id not in FUNCTIONS:
            raise FormulaError(f"unknown function {getattr(node.func, 'id', '?')!r}")
        if node.keywords:
            raise FormulaError("keyword arguments are not allowed; pass positionally")
    if isinstance(node, ast.Name) and node.id not in allowed_names and node.id not in FUNCTIONS:
        raise FormulaError(f"unknown name {node.id!r}")
    for child in ast.iter_child_nodes(node):
        _walk_validate(child, allowed_names)


def validate(expr: str, allowed_names: Iterable[str]) -> set[str]:
    """Parse + statically check ``expr`` against ``allowed_names`` (the vocabulary). Returns the set
    of vocabulary names it references. Raises :class:`FormulaError` with a human message on any
    disallowed construct, unknown name, or unknown function — the builder surfaces it pre-run."""
    tree = _parse(expr)
    allowed = set(allowed_names)
    _walk_validate(tree, allowed)
    return referenced_names(expr) & allowed


def referenced_names(expr: str) -> set[str]:
    """Every bare variable name the formula references (function names excluded)."""
    tree = _parse(expr)
    return {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)} - set(FUNCTIONS)


def _eval(node: ast.AST, ns: Mapping[str, pd.Series]):
    if isinstance(node, ast.Expression):
        return _eval(node.body, ns)
    if isinstance(node, ast.Constant):
        return node.value
    if isinstance(node, ast.Name):
        if node.id not in ns:
            raise FormulaError(f"unknown name {node.id!r}")
        return ns[node.id]
    if isinstance(node, ast.UnaryOp):
        v = _eval(node.operand, ns)
        return -v if isinstance(node.op, ast.USub) else +v
    if isinstance(node, ast.BinOp):
        return _BINOPS[type(node.op)](_eval(node.left, ns), _eval(node.right, ns))
    if isinstance(node, ast.Compare):
        return _CMPOPS[type(node.ops[0])](_eval(node.left, ns), _eval(node.comparators[0], ns))
    if isinstance(node, ast.Call):
        args = [_eval(a, ns) for a in node.args]
        return FUNCTIONS[node.func.id](*args)
    raise FormulaError(f"disallowed expression: {type(node).__name__.lower()}")


def evaluate(expr: str, namespace: Mapping[str, pd.Series]) -> pd.Series:
    """Evaluate ``expr`` over ``namespace`` (name → per-symbol Series) → a composite Series. The
    namespace is the *only* thing the formula can see. Non-finite results (e.g. a division by zero)
    are neutralized to NaN so downstream construction drops them rather than ranking on ±inf."""
    tree = _parse(expr)
    _walk_validate(tree, set(namespace))
    out = _eval(tree, namespace)
    if not isinstance(out, pd.Series):
        raise FormulaError("formula must reference at least one signal or field "
                           "(it evaluated to a single number)")
    return out.replace([np.inf, -np.inf], np.nan)
