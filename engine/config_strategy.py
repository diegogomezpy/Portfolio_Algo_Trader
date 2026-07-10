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
    # --- Extended construction / risk knobs (None = inherit the global settings.yaml value). These
    #     are mapped onto an EFFECTIVE settings for the account's run (see effective_settings), so
    #     they actually govern the optimizer AND the pre-trade risk gate for that sleeve. ---
    min_position_pct: Optional[float] = None     # optimizer position floor (else zero)
    max_sector_pct: Optional[float] = None       # max weight per GICS sector
    risk_aversion_lambda: Optional[float] = None # mean-variance risk penalty (optimizer)
    preselect_top_k: Optional[int] = None        # optimizer: rank then optimize over this many
    target_return_scale: Optional[float] = None  # optimizer: mu scale
    beta_window: Optional[int] = None            # factor window overrides
    vol_window: Optional[int] = None
    winsor_pct: Optional[float] = None
    mom_lookback: Optional[int] = None
    mom_skip: Optional[int] = None
    min_price: Optional[float] = None            # universe filters
    min_adv_usd: Optional[float] = None
    require_edgar: Optional[bool] = None
    # --- Operational (captured on the spec; consumed by the scheduler / runner, not construction) ---
    rebalance_frequency: str = "monthly"         # monthly | weekly | daily (FB5 scheduler cadence)
    mode: str = "normal"                         # execution mode: normal | express
    metadata: Mapping[str, object] = field(default_factory=dict)


def effective_settings(base, spec: StrategySpec):
    """A deep copy of the global ``settings`` with this spec's parameter overrides applied — the
    settings the account's run actually uses. Crucially it maps the spec's construction knobs onto
    the *same* leaves the optimizer AND the pre-trade risk gate read (``portfolio.max_single_name_pct``,
    ``max_sector_pct``, ``min_position_pct``, ``target_leverage`` …), so a builder-chosen 8% name cap
    or a different leverage isn't rejected by the global 5%/2× defaults on that sleeve. ``None``
    fields inherit the global value untouched. The primary book never goes through here."""
    import copy
    s = copy.deepcopy(base)
    # Read every field defensively — a partial/duck-typed spec (only some attributes set) inherits
    # the global value for the rest rather than raising.
    sg = lambda k: getattr(spec, k, None)

    def put(section: str, key: str, value):
        if value is None:
            return
        sec = getattr(s, section, None)
        if sec is not None:
            setattr(sec, key, value)

    # Construction caps → the leaves the optimizer + risk gate enforce.
    put("portfolio", "max_single_name_pct", sg("max_weight"))
    put("portfolio", "min_position_pct", sg("min_position_pct"))
    put("portfolio", "max_sector_pct", sg("max_sector_pct"))
    put("portfolio", "risk_aversion_lambda", sg("risk_aversion_lambda"))
    # Leverage: the spec's target governs; lift the hard cap to admit it on the sleeve (the risk
    # gate blocks target > max_leverage — never let the global cap veto the sleeve's own choice).
    lev = sg("leverage")
    put("portfolio", "target_leverage", lev)
    pf = getattr(s, "portfolio", None)
    if pf is not None and lev is not None:
        pf.max_leverage = max(float(getattr(pf, "max_leverage", lev)), float(lev))
    put("optimizer", "preselect_top_k", sg("preselect_top_k"))
    put("optimizer", "target_return_scale", sg("target_return_scale"))
    # Factor windows.
    put("factors", "beta_window", sg("beta_window"))
    put("factors", "vol_window", sg("vol_window"))
    put("factors", "winsor_pct", sg("winsor_pct"))
    put("factors", "mom_lookback", sg("mom_lookback"))
    put("factors", "mom_skip", sg("mom_skip"))
    # Universe filters.
    put("universe", "min_price", sg("min_price"))
    put("universe", "min_adv_usd", sg("min_adv_usd"))
    put("universe", "require_edgar_fundamentals", sg("require_edgar"))
    return s


# Fields serialized to / from the strategy_specs store (JSON). Overlay + metadata handled specially.
_SPEC_SCALARS = ("name", "formula", "construction", "max_names", "max_weight", "leverage",
                 "min_score", "min_position_pct", "max_sector_pct", "risk_aversion_lambda",
                 "preselect_top_k", "target_return_scale", "beta_window", "vol_window",
                 "winsor_pct", "mom_lookback", "mom_skip", "min_price", "min_adv_usd",
                 "require_edgar", "rebalance_frequency", "mode")


def spec_to_dict(spec: StrategySpec) -> dict:
    """Serialize a spec to a JSON-safe dict for the ``strategy_specs`` store / API."""
    out = {k: getattr(spec, k) for k in _SPEC_SCALARS}
    out["signals"] = {str(k): float(v) for k, v in (spec.signals or {}).items()}
    out["overlay"] = ({"mode": spec.overlay.mode, "market": spec.overlay.market,
                       "params": dict(spec.overlay.params)} if spec.overlay else None)
    return out


def spec_from_dict(d: Mapping) -> StrategySpec:
    """Rebuild a :class:`StrategySpec` from :func:`spec_to_dict` output (or a builder POST body).
    Unknown keys are ignored; missing keys fall back to the dataclass defaults."""
    kw = {k: d[k] for k in _SPEC_SCALARS if d.get(k) is not None}
    kw["signals"] = {str(k): float(v) for k, v in (d.get("signals") or {}).items()}
    ov = d.get("overlay")
    if ov and ov.get("mode"):
        kw["overlay"] = _strategy.OverlaySpec(mode=str(ov["mode"]),
                                              market=str(ov.get("market") or "SPY"),
                                              params=dict(ov.get("params") or {}))
    return StrategySpec(**kw)


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


def _latest_universe_snapshot(prices_dir, as_of: date):
    """The most recent equity snapshot (close + adv_20d, indexed by symbol) on/before ``as_of`` —
    the liquidity inputs for the universe filter. ``None`` if none exists / unreadable."""
    from pathlib import Path
    from engine import factors
    dated = [(f, d) for f in sorted(Path(prices_dir).glob("*.parquet"))
             if (d := factors._date_from_name(f)) is not None and d <= as_of]
    if not dated:
        return None
    try:
        return pd.read_parquet(dated[-1][0], columns=["close", "adv_20d"])
    except Exception:  # noqa: BLE001 — missing columns / bad file → skip the filter, don't crash
        return None


def _apply_liquidity_filter(prices_dir, as_of: date, universe: Sequence[str], u) -> list:
    """Drop names below the universe's min price / min ADV (matches the live factor-path gate)."""
    min_price = float(getattr(u, "min_price", 0) or 0) if u is not None else 0.0
    min_adv = float(getattr(u, "min_adv_usd", 0) or 0) if u is not None else 0.0
    if not (min_price or min_adv):
        return list(universe)
    snap = _latest_universe_snapshot(prices_dir, as_of)
    if snap is None:
        return list(universe)
    keep = snap[(snap["close"] > min_price) & (snap["adv_20d"] > min_adv)]
    return [s for s in universe if s in keep.index]


def _default_load(ctx: _strategy.StrategyContext, as_of: date, needs: set[str]):
    """Production data load: (price_panel, fundamentals_pit, universe) from the parquet stores —
    the same loaders the live factor path uses. Pulls fundamentals only if a chosen signal/field
    needs them (or the EDGAR-only universe policy is on), and applies the min-price/ADV liquidity
    gate. Kept behind a seam so tests inject synthetic data instead."""
    from engine import factors
    fac = ctx.settings.factors
    u = getattr(ctx.settings, "universe", None)
    lookback = max(int(fac.beta_window), int(fac.vol_window)) + 5
    panel = factors.load_close_panel(ctx.prices_dir, end=as_of, lookback=lookback)
    fpit = None
    universe: Sequence[str] = [c for c in panel.columns
                               if c != getattr(fac, "beta_market", "SPY")]
    need_edgar = bool(getattr(u, "require_edgar_fundamentals", False)) if u is not None else False
    if "fundamentals" in needs or need_edgar:
        allf, eligible = factors.load_scored_fundamentals(ctx.fundamentals_dir, ctx.settings)
        if "fundamentals" in needs:
            fpit = factors.point_in_time_fundamentals(allf, as_of, int(fac.report_lag_days))
        universe = [s for s in universe if eligible is None or s in eligible]
    universe = _apply_liquidity_filter(ctx.prices_dir, as_of, universe, u)
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
