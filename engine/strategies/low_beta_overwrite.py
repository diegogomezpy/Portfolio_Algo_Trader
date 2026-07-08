"""Low-beta factor equity + SPY call-spread overwrite — the first Strategy plugin (ADR-001 A2).

This is the book the engine has run all along, now expressed behind the `Strategy` interface so the
platform can treat it like any other plugin. **Wrap-in-place (A2):** `generate()` delegates to the
existing `compute_targets` (still in `scripts/run_eod.py`) and maps its `TargetPlan` into a
`TargetBook` — no target logic moves yet, so behaviour is provably identical (the parity test pins
the mapping as lossless). Relocating `compute_targets` into the engine, behind this now-stable
boundary, is a later increment.

The overlay is declared as an `OverlaySpec` derived from `settings` (the single source of truth for
the mode is `covered_calls.overlay_mode`). Whether it actually runs stays a platform/run decision
(the `overlay` flag), wired when `run_cycle` starts reading `TargetBook.overlay` (A3).
"""

from __future__ import annotations

from datetime import date
from typing import Callable, Optional

from engine import covered_calls, strategy as S


def _default_targets_fn() -> Callable:
    """The production target producer. **Temporary** engine→scripts reach (A2 wrap): a late import
    so there is no module-load cycle. Removed when `compute_targets` relocates into the engine."""
    from scripts.run_eod import compute_targets
    return compute_targets


class LowBetaOverwrite:
    """The low-beta + index-overwrite strategy as a registered plugin.

    ``targets_fn`` is injectable (tests pass a fake; `run_cycle` will inject the real
    `compute_targets` from its own scope in A3); ``None`` falls back to the late-imported production
    function so the plugin also works standalone.
    """
    name = "low_beta_overwrite"

    def __init__(self, targets_fn: Optional[Callable] = None):
        self._targets_fn = targets_fn

    def generate(self, ctx: S.StrategyContext, as_of: date) -> S.TargetBook:
        targets_fn = self._targets_fn or _default_targets_fn()
        # Pass through only the dirs the context actually carries, so an unset context field can't
        # override compute_targets' own defaults with an empty string.
        kw = {"db_engine": ctx.db_engine}
        if ctx.prices_dir:
            kw["prices_dir"] = ctx.prices_dir
        if ctx.fundamentals_dir:
            kw["fundamentals_dir"] = ctx.fundamentals_dir
        plan = targets_fn(ctx.settings, as_of, **kw)

        # Faithful, lossless map — the SAME objects, no reshaping (the parity test asserts identity).
        inputs = S.PlanInputs(
            prices=plan.prices, adv=plan.adv, spread=plan.spread, universe=plan.universe,
            sector_map=plan.sector_map, panel=plan.panel, sigma=plan.sigma)
        return S.TargetBook(
            weights=plan.weights, inputs=inputs,
            overlay=self._overlay_spec(ctx.settings),
            metadata={"strategy": self.name})

    @staticmethod
    def _overlay_spec(settings) -> Optional[S.OverlaySpec]:
        """The overlay this strategy intends, from settings — mode via the single source of truth
        (`covered_calls.overlay_mode`), plus a snapshot of the sizing knobs the overlay step reads."""
        cc = getattr(settings, "covered_calls", None)
        if cc is None:
            return None
        mode = covered_calls.overlay_mode(settings)
        market = str(getattr(getattr(settings, "factors", None), "beta_market", "SPY"))
        params = {
            "coverage": float(getattr(cc, "overwrite_coverage", 1.0)),
            "target_delta": float(getattr(cc, "target_delta", 0.30)),
            "wing_delta": float(getattr(cc, "wing_delta", 0.05)),
        }
        return S.OverlaySpec(mode=mode, market=market, params=params)
