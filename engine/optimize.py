"""Portfolio construction — convex max-Sharpe optimizer (DECISIONS D23b).

Turns composite factor scores + an FF5 covariance into target weights:

    maximize   μᵀw − λ·wᵀΣw
    subject to sum(w) = base_equity_allocation        (rest is cash)
               0 ≤ w ≤ max_single_name_pct
               sum(w over sector k) ≤ max_sector_pct  ∀ k

μ is a rank proxy: ``rank(composite)/N × target_return_scale`` (DECISIONS D20),
so the best-scoring names get the highest expected-return stand-in.

**Default is alpha-driven (λ = 0, DECISIONS D25).** The composite score already
prices risk (low-vol is one of its four sub-scores), so a mean-variance penalty
λ·wᵀΣw double-counts the defensive tilt and cost ~10%/yr in backtest. With λ=0 the
objective is the pure LP ``max μᵀw``; the box / sector / min-position constraints
supply the diversification, and Σ is kept for risk reporting and the thin-history
filter. The λ>0 mean-variance path is preserved for when the constraints don't
already pin the weights (e.g. a widened position band).

**Why this shape (D23b).** The real mandate also wants a *minimum* position
(``w ≥ min or w = 0``), which is non-convex, and no mixed-integer QP solver is
installed. So instead of an exact MIQP we (1) pre-select the top-K names by
composite, (2) solve the convex QP above, (3) zero any sub-minimum weight and
re-solve on the survivors until none remain below the floor, then (4) walk an
infeasibility-relaxation ladder if the QP cannot be solved. The 95% budget over a
4% floor caps the book near ~23 names organically, hitting the target band.

The math is isolated in pure functions (:func:`compute_mu`, :func:`solve_qp`,
:func:`optimize_portfolio`) and unit-tested on synthetic inputs — no network, no
solver surprises. ``"Unknown"``-sector names (ETFs/ADRs without a SIC) are exempt
from the sector cap rather than lumped into one artificial bucket.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import cvxpy as cp
import numpy as np
import pandas as pd

from engine.logger import get_logger

log = get_logger(__name__)

# Weights below this are treated as numerically zero (not "below the minimum").
_ZERO_TOL = 1e-6


@dataclass
class OptimizeResult:
    """Outcome of one optimization.

    ``weights`` holds only the funded names (each ≥ the min-position floor), as a
    fraction of NAV summing to ``base_equity_allocation``; ``cash`` is the rest.
    ``status`` is ``"optimal"`` (solved at the base constraints), ``"relaxed"``
    (needed the infeasibility ladder), or ``"held"`` (never feasible — previous
    weights returned, caller should alert).
    """
    weights: pd.Series
    status: str
    cash: float
    sector_relax: float = 0.0
    applied_min_pct: float = 0.0
    n_names: int = field(default=0)

    def __post_init__(self):
        self.n_names = int(len(self.weights))


def compute_mu(scores: pd.Series, target_return_scale: float) -> pd.Series:
    """Rank-based expected-return proxy: ``rank(composite)/N × scale``.

    Highest composite → ≈ ``scale``; lowest → ≈ ``scale/N``. Ties share the
    average rank. NaN scores are dropped (unscorable names cannot be sized).
    """
    s = scores.dropna()
    if s.empty:
        return s
    return s.rank(method="average") / len(s) * target_return_scale


def solve_qp(
    mu: pd.Series,
    sigma: pd.DataFrame,
    sectors: pd.Series,
    *,
    budget: float,
    w_max: float,
    sector_cap: float,
    lam: float,
) -> pd.Series | None:
    """Solve the convex QP over the given names; ``None`` if infeasible/failed.

    ``sectors`` maps each name to a bucket; ``"Unknown"`` names are exempt from the
    sector cap. ``mu`` and ``sigma`` must already be aligned to the same names.
    """
    names = list(mu.index)
    n = len(names)
    if n == 0:
        return None
    w = cp.Variable(n)
    expr = mu.to_numpy() @ w
    if lam > 0:  # mean-variance; λ=0 ⇒ pure alpha-weighting LP (DECISIONS D25)
        Σ = sigma.loc[names, names].to_numpy()
        Σ = (Σ + Σ.T) / 2.0  # enforce exact symmetry for the solver
        expr = expr - lam * cp.quad_form(w, cp.psd_wrap(Σ))
    objective = cp.Maximize(expr)
    constraints = [cp.sum(w) == budget, w >= 0, w <= w_max]
    for sec in sectors.loc[names].unique():
        if sec == "Unknown":
            continue
        mask = (sectors.loc[names] == sec).to_numpy()
        constraints.append(cp.sum(w[mask]) <= sector_cap)

    prob = cp.Problem(objective, constraints)
    try:
        prob.solve(solver=cp.CLARABEL)
    except Exception as exc:  # noqa: BLE001 — any solver failure ⇒ treat this name set as infeasible
        log.debug("QP solve raised; treating as infeasible", extra={"n": n, "error": str(exc)})
        return None
    if w.value is None or prob.status not in ("optimal", "optimal_inaccurate"):
        return None
    return pd.Series(np.clip(w.value, 0.0, None), index=names)


def _solve_with_cleanup(
    mu: pd.Series,
    sigma: pd.DataFrame,
    sectors: pd.Series,
    *,
    budget: float,
    w_max: float,
    sector_cap: float,
    lam: float,
    w_min: float,
    max_iter: int = 60,
) -> pd.Series | None:
    """Solve, then drop the smallest sub-``w_min`` name and re-solve until none remain.

    Dropping **one** name at a time (not all sub-floor names at once) is what lets
    the book settle into the 20–30 interior: when the unconstrained optimum spreads
    weight across many names below the floor, removing the single smallest and
    redistributing nudges the rest up, and the process converges to the largest set
    whose members all clear ``w_min`` (≈ ``budget/w_min`` names). Dropping the whole
    sub-floor batch instead would overshoot — collapsing to the ~``budget/w_max``
    concentrated corner or to nothing. Returns funded weights (each ≥ ``w_min``), or
    ``None`` if no feasible solve exists. The active set only shrinks, so it ends.
    """
    active = list(mu.index)
    funded = None
    for _ in range(max_iter):
        w = solve_qp(mu.loc[active], sigma, sectors, budget=budget,
                     w_max=w_max, sector_cap=sector_cap, lam=lam)
        if w is None:
            return funded  # a drop made it infeasible; keep the last good set
        below = w[(w > _ZERO_TOL) & (w < w_min)]
        funded = w[w >= w_min]
        if below.empty:
            return funded if not funded.empty else None
        active.remove(below.idxmin())   # drop only the single smallest below-floor name
        if not active:
            return funded if (funded is not None and not funded.empty) else None
    return funded if (funded is not None and not funded.empty) else None


def _incumbent_bonus(settings) -> float:
    """Hysteresis bonus from settings.rebalancing (0 if unset — back-compat for tests)."""
    reb = getattr(settings, "rebalancing", None)
    return float(getattr(reb, "incumbent_bonus", 0.0)) if reb is not None else 0.0


def _apply_incumbent_bonus(scores: pd.Series, prev_weights: pd.Series | None, bonus: float) -> pd.Series:
    """Add ``bonus`` to the composite score of currently-held names (in score units)."""
    if not bonus or prev_weights is None or prev_weights.empty:
        return scores
    eff = scores.copy()
    held = prev_weights.index.intersection(eff.index)
    eff.loc[held] = eff.loc[held] + bonus
    return eff


def optimize_portfolio(
    scores: pd.Series,
    sigma: pd.DataFrame,
    sector_map: pd.Series,
    *,
    settings,
    prev_weights: pd.Series | None = None,
) -> OptimizeResult:
    """Build target weights from composite scores and covariance.

    Reads constraints from ``settings.portfolio`` and ``settings.optimizer``.
    Pre-selects the top-K by composite, optimizes, enforces the min position via
    cleanup, and walks the infeasibility ladder (sector cap +5/+10/+15%, min
    position −$250/−$500) before falling back to ``prev_weights`` (status
    ``"held"``).
    """
    p, o = settings.portfolio, settings.optimizer
    budget = p.base_equity_allocation
    w_max = p.max_single_name_pct
    lam = p.risk_aversion_lambda
    # The position floor is a WEIGHT (min_position_pct). It used to be min_position_usd ÷ the
    # static YAML nav — which silently became a 4% weight floor the day the account stopped
    # being $100k (audit: "min_position_usd: 4000" on the $1M book was actually ~$76k). The
    # legacy pair is still honored for old fixtures.
    if hasattr(p, "min_position_pct"):
        base_min_pct = float(p.min_position_pct)
    else:  # legacy settings shape
        base_min_pct = float(p.min_position_usd) / float(p.nav)

    # Hysteresis (DECISIONS D26): give currently-held names a composite-score premium so
    # they stay selected unless a challenger clearly beats them — cuts membership churn /
    # turnover. The bonus shifts ranks; everything downstream (caps, weights) is unchanged.
    eff_scores = _apply_incumbent_bonus(scores, prev_weights, _incumbent_bonus(settings))
    mu_all = compute_mu(eff_scores, o.target_return_scale)
    # Pre-select top-K. Σ membership doubles as a min-history guard (names without
    # enough return history to risk-model are dropped from Σ). But when λ=0 the solver
    # never touches Σ (DECISIONS D25), so an *unavailable* Σ — e.g. the Ken French FF5
    # file lagging publication, leaving the window too thin to estimate — must not be
    # allowed to silently empty the universe or kill the rebalance. Filter by Σ only
    # when we actually have one, or when λ>0 genuinely needs it; else keep the full top-K.
    topk = mu_all.sort_values(ascending=False).head(o.preselect_top_k).index
    if lam > 0 or not sigma.empty:
        active = [s for s in topk if s in sigma.index]
    else:
        active = list(topk)
    mu = mu_all.loc[active]
    sectors = sector_map.reindex(active).fillna("Unknown")

    # Infeasibility-relaxation ladder (DECISIONS / ARCHITECTURE). The floor relaxations are
    # absolute weight steps (−0.25pp / −0.5pp), the pct equivalent of the old −$250/−$500 on
    # the $100k reference nav — identical rungs, no longer tied to a stale dollar figure.
    ladder = [
        (p.max_sector_pct, base_min_pct),
        (p.max_sector_pct + 0.05, base_min_pct),
        (p.max_sector_pct + 0.10, max(base_min_pct - 0.0025, 0.0)),
        (p.max_sector_pct + 0.15, max(base_min_pct - 0.0050, 0.0)),
    ]
    for attempt, (sector_cap, w_min) in enumerate(ladder):
        funded = _solve_with_cleanup(
            mu, sigma, sectors, budget=budget, w_max=w_max,
            sector_cap=sector_cap, lam=lam, w_min=w_min,
        )
        if funded is not None and not funded.empty:
            status = "optimal" if attempt == 0 else "relaxed"
            if attempt > 0:
                log.info("optimizer relaxed constraints",
                         extra={"attempt": attempt, "sector_cap": sector_cap, "w_min": w_min})
            return OptimizeResult(
                weights=funded.sort_values(ascending=False),
                status=status, cash=float(1.0 - funded.sum()),
                sector_relax=sector_cap - p.max_sector_pct,
                applied_min_pct=w_min,
            )

    # Never feasible — hold previous weights and alert.
    log.warning("optimizer infeasible after ladder; holding previous weights",
                extra={"candidates": len(active)})
    held = prev_weights if prev_weights is not None else pd.Series(dtype=float)
    return OptimizeResult(weights=held, status="held", cash=float(1.0 - held.sum()))


def risk_contributions(weights: pd.Series, sigma: pd.DataFrame) -> dict:
    """Per-name percent contribution to portfolio risk, from weights + covariance Σ.

    Standard Euler risk decomposition: with portfolio variance ``σ²ₚ = wᵀΣw``, each name's
    component risk contribution is ``RCᵢ = wᵢ·(Σw)ᵢ`` and the shares ``RCᵢ / σ²ₚ`` sum to 1.
    A concentrated name shows a risk share well above its weight; a diversifier shows below.

    Σ from :mod:`engine.covariance` is annualized, so ``portfolio_vol = √(wᵀΣw)`` reads as the
    book's annualized volatility. Names absent from Σ (too little history to risk-model) are
    dropped — their risk can't be attributed. Returns ``{"portfolio_vol", "contrib", "weight"}``
    with empty maps when there's nothing to decompose (no overlap or degenerate variance), so a
    caller can persist the result unconditionally. Pure and defensive by design — never raises
    on empty/degenerate input.
    """
    empty = {"portfolio_vol": None, "contrib": {}, "weight": {}}
    if weights is None or sigma is None or weights.empty or sigma.empty:
        return empty
    names = [s for s in weights.index if s in sigma.index]
    if not names:
        return empty
    w = weights.loc[names].to_numpy(dtype=float)
    cov = sigma.loc[names, names].to_numpy(dtype=float)
    cov = (cov + cov.T) / 2.0                       # symmetrize (Σ estimate may be slightly off)
    var = float(w @ cov @ w)
    if not (var > 0):                               # flat/degenerate → nothing to attribute
        return empty
    comp = w * (cov @ w)                            # component risk contributions (sum to var)
    pct = comp / var                                # shares that sum to 1.0
    return {"portfolio_vol": float(var ** 0.5),
            "contrib": {s: float(p) for s, p in zip(names, pct)},
            "weight": {s: float(x) for s, x in zip(names, w)}}
