"""Strategy plugin interface + registry — the platform keystone (ADR-001, D37).

A **strategy** is the one thing that differs between books: it decides *target weights* and,
optionally, a *derivatives overlay*. Everything else — reconcile, the pre-trade risk gate,
execution, monitoring, the backtest — is shared platform machinery that runs *around* a strategy.

The contract is deliberately tiny:

    class MyStrategy:
        name = "my_strategy"
        def generate(self, ctx: StrategyContext, as_of) -> TargetBook: ...

    strategy.register(MyStrategy())

A strategy is handed **read surfaces** (`StrategyContext`) and returns a **`TargetBook`** — its
decision (weights) plus the per-name execution/risk inputs the platform needs and an optional
overlay spec. It never receives a broker write handle: **strategies decide, they do not trade.**

This module is pure interface — it imports only pandas, so it stays cheap to import and free of
engine cycles. Concrete strategies live in ``engine/strategies/`` and register themselves; the
current low-beta + SPY-overwrite book becomes the first plugin (increment A2). Nothing here is
wired into ``run_cycle`` yet — that is increment A3.

Design note — **why the overlay is a spec, not a plan.** The index overlay is sized on the
*post-trade held book* (realized shares), so it cannot live inside the target weights. A strategy
therefore declares *what* overlay to run (`OverlaySpec`); the platform's post-trade step executes
it. This mirrors today's ``covered_calls.overlay_mode(settings)`` dispatch, now made explicit.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any, Mapping, Optional, Protocol, runtime_checkable

import pandas as pd


@dataclass(frozen=True)
class OverlaySpec:
    """Declarative descriptor of the derivatives overlay to run **after** the equity legs settle.

    ``mode`` — ``"index"`` (the SPY beta-overwrite call spread) or ``"per_name"`` (classic covered
    calls); ``None``-mode strategies simply return ``overlay=None``. ``params`` carries the sizing
    knobs the overlay step reads (coverage, target_delta, wing_delta, …) so a strategy can override
    the YAML defaults without the platform reaching back into ``settings``.
    """
    mode: str
    market: str = "SPY"
    params: Mapping[str, Any] = field(default_factory=dict)


@dataclass
class PlanInputs:
    """Per-name execution + risk inputs carried alongside the weights.

    These are byproducts of the strategy's own computation that the *platform* needs downstream —
    ``prices``/``adv``/``spread`` to size and route orders, ``panel``/``sigma``/``sector_map`` for
    the risk gate and the overlay's spot/IV. Field-for-field the payload of the legacy
    ``run_eod.TargetPlan``, so the current strategy pours into a ``TargetBook`` without reshaping
    (increment A2).
    """
    prices: dict[str, float] = field(default_factory=dict)
    adv: dict[str, float] = field(default_factory=dict)
    spread: dict[str, float] = field(default_factory=dict)
    universe: set = field(default_factory=set)
    sector_map: pd.Series = field(default_factory=lambda: pd.Series(dtype=object))
    panel: Optional[pd.DataFrame] = None            # close panel, for the overlay's spot/IV
    sigma: Optional[pd.DataFrame] = None            # covariance Σ, for the risk decomposition


@dataclass
class TargetBook:
    """A strategy's output for one rebalance: the equity decision + optional overlay + inputs.

    ``weights`` are fractions of the strategy's **deployable base** (``StrategyContext.capital_base``
    when set, else the whole book) — never of raw equity — so leverage and (later) per-strategy
    capital allocation scale the dollar base without touching the weights. ``metadata`` is free-form
    telemetry the dashboard/logs can surface (e.g. ``beta_p``, signal snapshot, sizing notes).
    """
    weights: pd.Series
    inputs: PlanInputs = field(default_factory=PlanInputs)
    overlay: Optional[OverlaySpec] = None
    metadata: dict = field(default_factory=dict)

    @property
    def is_empty(self) -> bool:
        """No investable decision — the platform treats this like ``no_targets`` (skip + retry)."""
        return self.weights is None or len(self.weights) == 0


@dataclass(frozen=True)
class StrategyContext:
    """Read surfaces handed to ``Strategy.generate`` — no broker write handle by design.

    ``capital_base`` is the deployable dollars this strategy sizes against (leverage × equity ×
    its allocation); ``None`` today means "the whole book," which the platform fills in.
    ``positions`` is the strategy's current holdings — empty until the account-per-strategy plumbing
    (Phase B) supplies them. Everything a strategy needs to *compute* a target, nothing it needs to
    *place* one.
    """
    settings: Any
    db_engine: Any = None
    prices_dir: str = ""
    fundamentals_dir: str = ""
    capital_base: Optional[float] = None
    positions: Mapping[str, float] = field(default_factory=dict)


@runtime_checkable
class Strategy(Protocol):
    """The plugin contract: a ``name`` and a pure-ish ``generate`` that returns a ``TargetBook``.

    Implementations may be any object exposing these — a class instance is the norm. ``generate``
    may read data and persist telemetry (factor scores), but must not submit orders; the platform
    owns execution.
    """
    name: str

    def generate(self, ctx: StrategyContext, as_of: date) -> TargetBook: ...


# ---------------------------------------------------------------------------- #
# Registry — strategies register themselves by name; the orchestrator loads one
# by config. Kept as a plain module-level dict (no hidden state beyond it); tests
# use ``clear()`` for isolation.
# ---------------------------------------------------------------------------- #
_REGISTRY: dict[str, Strategy] = {}


def _is_strategy(obj: object) -> bool:
    """Duck-typed check: a non-empty ``name`` and a callable ``generate`` (runtime_checkable
    Protocols don't verify data attributes, so we check explicitly)."""
    return bool(getattr(obj, "name", "")) and callable(getattr(obj, "generate", None))


def register(strategy: Strategy) -> Strategy:
    """Register ``strategy`` under ``strategy.name`` and return it (usable as a decorator).

    Re-registering the *same* object is a no-op; a *different* object under an existing name is a
    programming error (two strategies claiming one name) and raises.
    """
    if not _is_strategy(strategy):
        raise TypeError("a strategy must expose a non-empty `name` and a callable `generate`")
    name = strategy.name
    existing = _REGISTRY.get(name)
    if existing is not None and existing is not strategy:
        raise ValueError(f"strategy {name!r} is already registered to a different object")
    _REGISTRY[name] = strategy
    return strategy


def get(name: str) -> Strategy:
    """The registered strategy for ``name``; raises ``KeyError`` with the known names if absent."""
    try:
        return _REGISTRY[name]
    except KeyError:
        known = ", ".join(sorted(_REGISTRY)) or "(none registered)"
        raise KeyError(f"no strategy named {name!r}; registered: {known}") from None


def all_strategies() -> dict[str, Strategy]:
    """A copy of the registry ``{name: strategy}`` — for the dashboard's strategy list."""
    return dict(_REGISTRY)


def is_registered(name: str) -> bool:
    return name in _REGISTRY


def clear() -> None:
    """Empty the registry — test isolation only; never call from production code."""
    _REGISTRY.clear()
