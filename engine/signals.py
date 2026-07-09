"""Composable signal library — the vocabulary for on-the-fly strategies (ADR-001 / D37 north star).

A **signal** is one cross-sectional trait: given the data at ``as_of`` and a universe, it returns a
z-scored series per symbol (higher = more attractive). A strategy is then "pick signals + weights",
which :func:`compose` blends into a composite — exactly the shape a dashboard strategy-builder needs.

**This is additive and does NOT touch the live strategy.** Each built-in signal *wraps the existing
``engine.factors`` math* (``zscore`` / ``_double_z`` / ``market_beta`` / ``realized_vol``) rather
than re-deriving it, so a signal is byte-identical to the corresponding factor sub-score.
:func:`compose` reproduces ``factors.compute_factor_scores``'s weighted, neutral-filled composite —
proven by the parity test — so the current book can later route through this library with **no change
to its targets**. Until that parity-gated switch, ``factors.py`` remains the live path unchanged.

Registry mirrors :mod:`engine.strategy`: ``register`` / ``get`` / ``all_signals`` / ``clear``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any, Mapping, Protocol, Sequence, runtime_checkable

import numpy as np
import pandas as pd

from engine.factors import _double_z, market_beta, realized_vol, zscore


@dataclass(frozen=True)
class SignalContext:
    """Inputs a signal computes from. ``fundamentals_pit`` is point-in-time (indexed by symbol);
    ``price_panel`` is the (date × symbol) close matrix ending at ``as_of``. ``settings`` supplies
    the winsor pct + windows so a signal matches the live factor computation exactly."""
    as_of: date
    settings: Any
    price_panel: pd.DataFrame | None = None
    fundamentals_pit: pd.DataFrame | None = None


@runtime_checkable
class Signal(Protocol):
    """A named trait. ``needs`` declares its data dependencies (``"prices"`` / ``"fundamentals"``)
    so the builder can wire the right sources. ``compute`` returns a **z-scored** series over
    ``symbols``, oriented higher = more attractive (NaN preserved where inputs are missing —
    :func:`compose` neutral-fills). Optional ``label`` / ``category`` / ``desc`` attributes drive
    the dashboard palette (grouping + human copy); they don't affect the math."""
    name: str
    needs: tuple[str, ...]

    def compute(self, ctx: SignalContext, symbols: Sequence[str]) -> pd.Series: ...


def _wp(ctx: SignalContext) -> float:
    return ctx.settings.factors.winsor_pct


@dataclass(frozen=True)
class ValueSignal:
    """Earnings yield (E/P) + book yield (B/P), double-z'd — the live ``value`` factor."""
    name: str = "value"
    needs: tuple[str, ...] = ("fundamentals",)
    label: str = "Value"
    category: str = "Fundamental · composite"
    desc: str = "Earnings yield (E/P) + book yield (B/P), z-scored. Higher = cheaper."

    def compute(self, ctx: SignalContext, symbols: Sequence[str]) -> pd.Series:
        wp = _wp(ctx)
        f = ctx.fundamentals_pit.reindex(symbols)
        ep = 1.0 / f["pe_ratio"].where(f["pe_ratio"] != 0)
        bp = 1.0 / f["pb_ratio"].where(f["pb_ratio"] != 0)
        return _double_z(zscore(ep, wp), zscore(bp, wp), wp)


@dataclass(frozen=True)
class QualitySignal:
    """ROE + gross margin, double-z'd — the live ``quality`` factor."""
    name: str = "quality"
    needs: tuple[str, ...] = ("fundamentals",)
    label: str = "Quality"
    category: str = "Fundamental · composite"
    desc: str = "Return on equity + gross margin, z-scored. Higher = more profitable."

    def compute(self, ctx: SignalContext, symbols: Sequence[str]) -> pd.Series:
        wp = _wp(ctx)
        f = ctx.fundamentals_pit.reindex(symbols)
        return _double_z(zscore(f["roe"], wp), zscore(f["gross_margin"], wp), wp)


@dataclass(frozen=True)
class LowBetaSignal:
    """z(−β) vs the market — the live ``low_beta`` factor (lower β preferred)."""
    name: str = "low_beta"
    needs: tuple[str, ...] = ("prices",)
    label: str = "Low beta"
    category: str = "Price · defensive"
    desc: str = "z(−β) vs the market (SPY). Higher = lower market sensitivity."

    def compute(self, ctx: SignalContext, symbols: Sequence[str]) -> pd.Series:
        fac = ctx.settings.factors
        beta = market_beta(ctx.price_panel, ctx.as_of, fac.beta_window,
                           getattr(fac, "beta_market", "SPY"))
        return zscore(-beta.reindex(symbols), _wp(ctx))


@dataclass(frozen=True)
class LowVolSignal:
    """z(−realized vol) — the live ``low_vol`` factor."""
    name: str = "low_vol"
    needs: tuple[str, ...] = ("prices",)
    label: str = "Low volatility"
    category: str = "Price · defensive"
    desc: str = "z(−realized volatility). Higher = calmer price action."

    def compute(self, ctx: SignalContext, symbols: Sequence[str]) -> pd.Series:
        vol = realized_vol(ctx.price_panel, ctx.as_of, ctx.settings.factors.vol_window)
        return zscore(-vol.reindex(symbols), _wp(ctx))


# ---------------------------------------------------------------------------- #
# Single-metric signals — finer-grained building blocks than the composites above.
# Each is the z-score of one raw metric, oriented higher = more attractive, so they
# blend on the same unit-variance scale as the four factors.
# ---------------------------------------------------------------------------- #
def _momentum(price_panel: pd.DataFrame, as_of: date, lookback: int, skip: int) -> pd.Series:
    """Raw price momentum: return from ``lookback`` days ago to ``skip`` days ago (the classic
    12-1 "skip the last month" construction). Clamps the windows to the available history so it
    still yields a varying cross-section on short panels; NaN when there's too little data."""
    panel = price_panel.loc[: pd.Timestamp(as_of)]
    n = len(panel)
    if n < 3:
        return pd.Series(np.nan, index=price_panel.columns, dtype=float)
    sk = max(0, min(int(skip), n - 2))
    lb = min(int(lookback), n - 1)
    if lb <= sk:
        lb = sk + 1
    end = panel.iloc[-(sk + 1)]
    start = panel.iloc[-(lb + 1)]
    return end / start.where(start != 0) - 1.0


def _mom_windows(ctx: SignalContext) -> tuple[int, int]:
    fac = ctx.settings.factors
    return int(getattr(fac, "mom_lookback", 252)), int(getattr(fac, "mom_skip", 21))


@dataclass(frozen=True)
class RoeSignal:
    name: str = "roe"
    needs: tuple[str, ...] = ("fundamentals",)
    label: str = "ROE"
    category: str = "Fundamental"
    desc: str = "z(return on equity). Higher = more profitable."

    def compute(self, ctx: SignalContext, symbols: Sequence[str]) -> pd.Series:
        return zscore(ctx.fundamentals_pit.reindex(symbols)["roe"], _wp(ctx))


@dataclass(frozen=True)
class GrossMarginSignal:
    name: str = "gross_margin"
    needs: tuple[str, ...] = ("fundamentals",)
    label: str = "Gross margin"
    category: str = "Fundamental"
    desc: str = "z(gross profit margin). Higher = wider margins."

    def compute(self, ctx: SignalContext, symbols: Sequence[str]) -> pd.Series:
        return zscore(ctx.fundamentals_pit.reindex(symbols)["gross_margin"], _wp(ctx))


@dataclass(frozen=True)
class EarningsYieldSignal:
    name: str = "earnings_yield"
    needs: tuple[str, ...] = ("fundamentals",)
    label: str = "Earnings yield"
    category: str = "Fundamental"
    desc: str = "z(E/P = 1/PE). Higher = cheaper on earnings."

    def compute(self, ctx: SignalContext, symbols: Sequence[str]) -> pd.Series:
        pe = ctx.fundamentals_pit.reindex(symbols)["pe_ratio"]
        return zscore(1.0 / pe.where(pe != 0), _wp(ctx))


@dataclass(frozen=True)
class BookYieldSignal:
    name: str = "book_yield"
    needs: tuple[str, ...] = ("fundamentals",)
    label: str = "Book yield"
    category: str = "Fundamental"
    desc: str = "z(B/P = 1/PB). Higher = cheaper on book value."

    def compute(self, ctx: SignalContext, symbols: Sequence[str]) -> pd.Series:
        pb = ctx.fundamentals_pit.reindex(symbols)["pb_ratio"]
        return zscore(1.0 / pb.where(pb != 0), _wp(ctx))


@dataclass(frozen=True)
class MomentumSignal:
    name: str = "momentum"
    needs: tuple[str, ...] = ("prices",)
    label: str = "Momentum"
    category: str = "Price"
    desc: str = "z(12-1 month price momentum). Higher = stronger trend."

    def compute(self, ctx: SignalContext, symbols: Sequence[str]) -> pd.Series:
        lb, sk = _mom_windows(ctx)
        return zscore(_momentum(ctx.price_panel, ctx.as_of, lb, sk).reindex(symbols), _wp(ctx))


def compose(scores: Mapping[str, pd.Series], weights: Mapping[str, float]) -> pd.Series:
    """Blend signal ``scores`` by ``weights`` into a composite, neutral-filling missing sub-scores
    to 0 — identical to ``factors.compute_factor_scores``'s composite. Only weighted signals present
    in ``scores`` contribute; a weight for an absent signal is skipped."""
    total: pd.Series | None = None
    for name, w in weights.items():
        s = scores.get(name)
        if s is None:
            continue
        contrib = float(w) * s.fillna(0.0)
        total = contrib if total is None else total.add(contrib, fill_value=0.0)
    return total if total is not None else pd.Series(dtype=float)


# ---------------------------------------------------------------------------- #
# Registry — signals register by name; the builder lists + selects them.
# ---------------------------------------------------------------------------- #
_REGISTRY: dict[str, Signal] = {}


def _is_signal(obj: object) -> bool:
    return bool(getattr(obj, "name", "")) and callable(getattr(obj, "compute", None))


def register(signal: Signal) -> Signal:
    if not _is_signal(signal):
        raise TypeError("a signal must expose a non-empty `name` and a callable `compute`")
    existing = _REGISTRY.get(signal.name)
    if existing is not None and existing is not signal:
        raise ValueError(f"signal {signal.name!r} is already registered to a different object")
    _REGISTRY[signal.name] = signal
    return signal


def get(name: str) -> Signal:
    try:
        return _REGISTRY[name]
    except KeyError:
        known = ", ".join(sorted(_REGISTRY)) or "(none)"
        raise KeyError(f"no signal named {name!r}; registered: {known}") from None


def all_signals() -> dict[str, Signal]:
    return dict(_REGISTRY)


def clear() -> None:
    """Test isolation only."""
    _REGISTRY.clear()


_BUILTINS = (QualitySignal, ValueSignal, LowBetaSignal, LowVolSignal,
             RoeSignal, GrossMarginSignal, EarningsYieldSignal, BookYieldSignal, MomentumSignal)


def register_builtins() -> None:
    """Register the built-in signals (the four live factors + finer single-metric traits).
    Idempotent — safe to call on every request."""
    for cls in _BUILTINS:
        sig = cls()
        if sig.name not in _REGISTRY:
            _REGISTRY[sig.name] = sig


def signal_specs() -> list[dict]:
    """The palette the dashboard builder renders: one dict per registered signal with its
    human label/category/description and data needs. Sorted by category then name so related
    traits group together."""
    register_builtins()
    out = [{"name": s.name, "label": getattr(s, "label", s.name),
            "category": getattr(s, "category", ""), "needs": list(getattr(s, "needs", ())),
            "desc": getattr(s, "desc", "")} for s in _REGISTRY.values()]
    return sorted(out, key=lambda d: (d["category"], d["name"]))


# ---------------------------------------------------------------------------- #
# Raw fields — native-unit per-symbol series, the formula engine's variables (FB3). Distinct
# ``raw_*`` names so they never collide with the z-scored signals above: a bare name in a
# formula is the z-scored signal (higher = better); ``raw_*`` is the untouched value. Transform
# a raw field explicitly with z()/rank() in a formula.
# ---------------------------------------------------------------------------- #
# name -> (needs, description). Values computed by raw_fields().
FIELD_META: dict[str, tuple[str, str]] = {
    "raw_pe": ("fundamentals", "Price / earnings ratio (raw)"),
    "raw_pb": ("fundamentals", "Price / book ratio (raw)"),
    "raw_ep": ("fundamentals", "Earnings yield E/P = 1/PE (raw)"),
    "raw_bp": ("fundamentals", "Book yield B/P = 1/PB (raw)"),
    "raw_roe": ("fundamentals", "Return on equity (raw)"),
    "raw_gross_margin": ("fundamentals", "Gross profit margin (raw)"),
    "raw_beta": ("prices", "Market beta vs SPY (raw, higher = more sensitive)"),
    "raw_vol": ("prices", "Realized volatility (raw)"),
    "raw_ret": ("prices", "12-1 price momentum return (raw)"),
}


def raw_fields(ctx: SignalContext, symbols: Sequence[str]) -> dict[str, pd.Series]:
    """The raw (non-z-scored) variables available to a formula, keyed by their ``raw_*`` name.
    Only the fields whose source is present are returned (fundamentals and/or prices)."""
    out: dict[str, pd.Series] = {}
    fac = ctx.settings.factors
    idx = list(symbols)
    if ctx.fundamentals_pit is not None:
        f = ctx.fundamentals_pit.reindex(idx)
        pe, pb = f["pe_ratio"], f["pb_ratio"]
        out["raw_pe"] = pe
        out["raw_pb"] = pb
        out["raw_ep"] = 1.0 / pe.where(pe != 0)
        out["raw_bp"] = 1.0 / pb.where(pb != 0)
        out["raw_roe"] = f["roe"]
        out["raw_gross_margin"] = f["gross_margin"]
    if ctx.price_panel is not None:
        out["raw_beta"] = market_beta(ctx.price_panel, ctx.as_of, fac.beta_window,
                                      getattr(fac, "beta_market", "SPY")).reindex(idx)
        out["raw_vol"] = realized_vol(ctx.price_panel, ctx.as_of, fac.vol_window).reindex(idx)
        lb, sk = _mom_windows(ctx)
        out["raw_ret"] = _momentum(ctx.price_panel, ctx.as_of, lb, sk).reindex(idx)
    return out


def field_specs() -> list[dict]:
    """The raw-field palette for the formula builder (name + source + description)."""
    return [{"name": n, "needs": [need], "desc": desc, "category": "Raw field"}
            for n, (need, desc) in FIELD_META.items()]
