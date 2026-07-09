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
    so the builder can wire the right sources. ``compute`` returns a z-scored series over ``symbols``
    (NaN preserved where inputs are missing — :func:`compose` neutral-fills)."""
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

    def compute(self, ctx: SignalContext, symbols: Sequence[str]) -> pd.Series:
        wp = _wp(ctx)
        f = ctx.fundamentals_pit.reindex(symbols)
        return _double_z(zscore(f["roe"], wp), zscore(f["gross_margin"], wp), wp)


@dataclass(frozen=True)
class LowBetaSignal:
    """z(−β) vs the market — the live ``low_beta`` factor (lower β preferred)."""
    name: str = "low_beta"
    needs: tuple[str, ...] = ("prices",)

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

    def compute(self, ctx: SignalContext, symbols: Sequence[str]) -> pd.Series:
        vol = realized_vol(ctx.price_panel, ctx.as_of, ctx.settings.factors.vol_window)
        return zscore(-vol.reindex(symbols), _wp(ctx))


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


def register_builtins() -> None:
    """Register the four built-in signals (the current book's factors). Idempotent."""
    for sig in (QualitySignal(), ValueSignal(), LowBetaSignal(), LowVolSignal()):
        if sig.name not in _REGISTRY:
            _REGISTRY[sig.name] = sig
