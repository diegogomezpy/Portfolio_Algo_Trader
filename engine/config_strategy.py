"""Declarative, config-defined strategy — the on-the-fly strategy engine (ADR-001 / D37 north star).

A :class:`StrategySpec` is a strategy expressed as *data*, not code: which **signals** to score on
(and their weights), how to **construct** the book (how many names, caps, leverage), and the
**overlay**. :class:`ConfigStrategy` turns a spec into the standard :class:`~engine.strategy.Strategy`
(→ ``TargetBook``), so a spec authored in the dashboard runs through the exact same pipeline
(reconcile → risk → execute → monitor) as any code strategy.

This is the "select traits + how to trade" layer of the builder, sitting on the composable
:mod:`engine.signals` library. **It does not touch the live book** — it's a new, registrable strategy
type intended first for the *test-account testbed*; the primary book keeps running
``low_beta_overwrite`` unchanged.

Construction here is a deterministic top-N (score-weighted, capped, leverage-scaled) — self-contained
and unit-testable, no cvxpy/covariance. A future spec option can select the full
``optimize.optimize_portfolio`` path; the spec shape leaves room for it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Callable, Mapping, Optional, Sequence

import pandas as pd

from engine import formula as _formula
from engine import signals as _signals
from engine import strategy as _strategy
from engine.logger import get_logger

log = get_logger(__name__)


@dataclass(frozen=True)
class StrategySpec:
    """A strategy as data. The score is defined one of two ways:

    * ``signals`` — a signal name → weight blend (the simple, one-click composition), OR
    * ``formula`` — a free-form expression over the same vocabulary + raw fields (:mod:`engine.formula`).
      When ``formula`` is set it takes precedence over ``signals``.

    ``construction``/``max_names``/``max_weight``/``leverage``/``min_score`` are the construction
    knobs; ``overlay`` is the optional derivatives overlay (an :class:`~engine.strategy.OverlaySpec`)."""
    name: str
    signals: Mapping[str, float] = field(default_factory=dict)
    formula: Optional[str] = None
    construction: str = "topn"                   # "topn" (self-contained) | "optimizer" (cap-respecting)
    max_names: int = 20
    max_weight: float = 0.05
    leverage: float = 1.0
    min_score: float = 0.0                       # only names scoring above this are held (long-only)
    overlay: Optional[_strategy.OverlaySpec] = None
    metadata: Mapping[str, object] = field(default_factory=dict)


def construct_weights(composite: pd.Series, spec: StrategySpec) -> pd.Series:
    """Top-``max_names`` by composite score, weighted ∝ (positive) score, each capped at
    ``max_weight``, normalized to sum 1.0. Long-only; empty if nothing qualifies.

    Weights are **fractions of the deployable base** (like ``optimize.optimize_portfolio``); the
    runner applies ``spec.leverage`` at sizing (``nav = equity × leverage``), matching the live
    convention — so ``leverage`` is NOT baked into the weights here."""
    s = composite.dropna()
    s = s[s > spec.min_score]
    if s.empty:
        return pd.Series(dtype=float)
    top = s.nlargest(int(spec.max_names)).clip(lower=0.0)
    if top.sum() <= 0:
        return pd.Series(dtype=float)
    # Score-proportional, normalized to 1.0, then capped at max_weight. If the cap binds (too few
    # names for the cap, e.g. 15 names @ 5% can't reach 100%), the book holds CASH rather than
    # breaching the cap — we never renormalize back up past the cap (that reintroduced the breach).
    # Fully-specified strategies (>= 1/max_weight names) still invest ~100%.
    return (top / top.sum()).clip(upper=float(spec.max_weight))


def _default_load(ctx: _strategy.StrategyContext, as_of: date, needs: set[str]):
    """Production data load: (price_panel, fundamentals_pit, universe) from the parquet stores —
    the same loaders the live factor path uses. Only pulls fundamentals if a chosen signal needs
    them. Kept behind a seam so tests inject synthetic data instead."""
    from engine import factors
    fac = ctx.settings.factors
    lookback = max(int(fac.beta_window), int(fac.vol_window)) + 5
    panel = factors.load_close_panel(ctx.prices_dir, end=as_of, lookback=lookback)
    fpit = None
    universe: Sequence[str] = [c for c in panel.columns
                               if c != getattr(fac, "beta_market", "SPY")]
    if "fundamentals" in needs:
        allf, eligible = factors.load_scored_fundamentals(ctx.fundamentals_dir, ctx.settings)
        fpit = factors.point_in_time_fundamentals(allf, as_of, int(fac.report_lag_days))
        universe = [s for s in universe if eligible is None or s in eligible]
    return panel, fpit, list(universe)


class ConfigStrategy:
    """A :class:`StrategySpec` as a runnable :class:`~engine.strategy.Strategy`.

    ``load_data(ctx, as_of, needs) -> (price_panel, fundamentals_pit, universe)`` is injectable
    (tests pass synthetic data); ``None`` uses the production parquet loaders. ``generate`` computes
    the spec's signals over the universe, blends them, constructs weights, and returns a
    ``TargetBook`` carrying the spec's overlay.
    """

    def __init__(self, spec: StrategySpec, *, load_data: Optional[Callable] = None):
        self.spec = spec
        self.name = spec.name
        self._load = load_data or _default_load

    def _construct(self, composite: pd.Series, ctx: _strategy.StrategyContext) -> pd.Series:
        """Turn composite scores into weights. ``optimizer`` uses the live constrained optimizer
        (respects the SAME sector/name/min-position caps the risk gate enforces → tradeable books);
        ``topn`` is the self-contained score-weighted top-N. Optimizer degrades to topn if the full
        settings.portfolio/optimizer config or sector map is unavailable."""
        if self.spec.construction != "optimizer":
            return construct_weights(composite, self.spec)
        try:
            from engine import optimize, sectors
            try:
                smap = sectors.load_sector_map()["sector"]
            except Exception:  # noqa: BLE001 — no sector map → optimizer runs without the sector cap
                smap = pd.Series(dtype=object)
            res = optimize.optimize_portfolio(composite, pd.DataFrame(), smap, settings=ctx.settings)
            return res.weights
        except Exception as exc:  # noqa: BLE001 — missing optimizer config → fall back to top-N
            log.warning("optimizer construction unavailable; using top-N",
                        extra={"strategy": self.name, "error": str(exc)})
            return construct_weights(composite, self.spec)

    def _needs(self) -> set[str]:
        """Which data sources to load — union of the referenced signals'/fields' needs. A formula
        drives off the names it references; the blend drives off its selected signals."""
        _signals.register_builtins()
        out: set[str] = set()
        if self.spec.formula:
            known = _signals.all_signals()
            for name in _formula.referenced_names(self.spec.formula):
                if name in known:
                    out.update(getattr(known[name], "needs", ()))
                elif name in _signals.FIELD_META:
                    out.add(_signals.FIELD_META[name][0])
            return out
        for name in self.spec.signals:
            out.update(getattr(_signals.get(name), "needs", ()))
        return out

    def _composite(self, sctx: _signals.SignalContext, universe: Sequence[str]) -> pd.Series:
        """The per-symbol composite score: evaluate the formula over the referenced signals + raw
        fields if a formula is set, else the weighted signal blend."""
        if self.spec.formula:
            known = _signals.all_signals()
            fields = _signals.raw_fields(sctx, universe)
            ns: dict = {}
            for name in _formula.referenced_names(self.spec.formula):
                if name in known:
                    ns[name] = known[name].compute(sctx, universe)
                elif name in fields:
                    ns[name] = fields[name]
            return _formula.evaluate(self.spec.formula, ns)
        scores = {name: _signals.get(name).compute(sctx, universe) for name in self.spec.signals}
        return _signals.compose(scores, self.spec.signals)

    def generate(self, ctx: _strategy.StrategyContext, as_of: date) -> _strategy.TargetBook:
        _signals.register_builtins()
        panel, fpit, universe = self._load(ctx, as_of, self._needs())
        sctx = _signals.SignalContext(as_of=as_of, settings=ctx.settings,
                                      price_panel=panel, fundamentals_pit=fpit)
        composite = self._composite(sctx, universe)
        weights = self._construct(composite, ctx)
        prices = {}
        if panel is not None and len(panel):
            last = panel.iloc[-1]
            prices = {s: float(last[s]) for s in weights.index
                      if s in last.index and pd.notna(last[s])}
        inputs = _strategy.PlanInputs(prices=prices, universe=set(universe), panel=panel)
        meta = {"strategy": self.name, "kind": "config"}
        if self.spec.formula:
            meta["formula"] = self.spec.formula
        else:
            meta["signals"] = dict(self.spec.signals)
        return _strategy.TargetBook(weights=weights, inputs=inputs, overlay=self.spec.overlay,
                                    metadata=meta)


def from_spec(spec: StrategySpec, **kw) -> ConfigStrategy:
    """Build (and return) a ConfigStrategy for ``spec`` — the builder's entry point."""
    return ConfigStrategy(spec, **kw)
