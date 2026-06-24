"""Pre-trade risk gate (ARCHITECTURE.md).

The last check before any order reaches the broker. It runs *after* the optimizer
(and, from Phase 4, the covered-call overlay) and *before* `engine/execute.py`, and
it can hard-block the whole batch. It re-derives every constraint **independently**
from the proposed post-trade state rather than trusting the optimizer — defense in
depth: a bug upstream, a stale price, or an unexpected position should be caught here,
not sent to the market.

Checks (all on the *proposed post-trade* book):

* long-only — no negative target weights (SPEC: no short selling)
* single-name cap — no weight > ``max_single_name_pct`` (of the deployable base)
* sector cap — no sector's weight sum > ``max_sector_pct`` (``Unknown`` exempt, as in
  the optimizer)
* over-deployment — total invested weight ≤ 1.0 of the deployable base (no accidental
  over-allocation of the base)
* leverage cap — gross/equity (``leverage``, passed by the caller) ≤ ``max_leverage``
  (DECISIONS D32): the hard bound on how levered the book may run
* universe — every name is in the approved tradable universe
* no naked calls — every covered call is backed by enough held shares of its underlying
* valid option expiry — no covered call already expired

Pure and Alpaca-free: it takes the proposed weights / orders and returns a
:class:`RiskCheckResult`. On failure the caller logs at ERROR and alerts (the gate
logs the reasons); ``execute.py`` must not submit when ``approved`` is False.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Iterable, Mapping, Sequence

import pandas as pd

from engine.logger import get_logger

log = get_logger(__name__)

_TOL = 1e-4  # cap tolerance — absorbs float noise, not real breaches
_CALL_CONTRACT_SIZE = 100  # standard option; minis (10) are validated by their own size


@dataclass
class RiskCheckResult:
    """Outcome of the pre-trade gate. ``approved`` is True only if ``failures`` is empty."""
    approved: bool
    failures: list[str] = field(default_factory=list)

    @property
    def reason(self) -> str:
        return "; ".join(self.failures) if self.failures else "ok"


def _check_long_only(weights: pd.Series) -> list[str]:
    bad = weights[weights < -_TOL]
    return [f"negative weight (short) {s}: {w:.4f}" for s, w in bad.items()]


def _check_single_name(weights: pd.Series, cap: float) -> list[str]:
    bad = weights[weights > cap + _TOL]
    return [f"single-name cap breached {s}: {w:.3f} > {cap:.3f}" for s, w in bad.items()]


def _check_sector(weights: pd.Series, sector_map: pd.Series, cap: float) -> list[str]:
    sectors = sector_map.reindex(weights.index).fillna("Unknown")
    by_sector = weights.groupby(sectors).sum()
    by_sector = by_sector[by_sector.index != "Unknown"]
    bad = by_sector[by_sector > cap + _TOL]
    return [f"sector cap breached {s}: {v:.3f} > {cap:.3f}" for s, v in bad.items()]


def _check_leverage(weights: pd.Series, max_total: float = 1.0) -> list[str]:
    """Guard against over-deploying the base: invested weight ≤ ``max_total`` (of base)."""
    total = float(weights.clip(lower=0).sum())
    return [f"over-deployed: invested {total:.3f} > {max_total:.3f} of base"] if total > max_total + _TOL else []


def _check_leverage_cap(leverage: float, max_leverage: float) -> list[str]:
    """Hard cap on gross/equity leverage (DECISIONS D32)."""
    return ([f"leverage {leverage:.2f}x exceeds cap {max_leverage:.2f}x"]
            if leverage > max_leverage + _TOL else [])


def _check_universe(weights: pd.Series, universe: Iterable[str]) -> list[str]:
    approved = set(universe)
    return [f"off-universe symbol: {s}" for s in weights.index if s not in approved]


def check_covered_call_coverage(option_orders, equity_positions, as_of=None) -> list[str]:
    """No naked calls and no expired contracts (public; reusable as a pre-submit guard).

    Each order: ``underlying``, ``contracts``, optional ``contract_size`` (default 100),
    ``expiration`` (ISO/date). Returns a list of failure strings (empty == all covered).
    The covered-call write path calls this on its plan as defense-in-depth before any
    sell-to-open reaches the broker, independent of the by-construction ``floor(shares/100)``
    sizing — the same gate the pre-trade risk check applies.
    """
    if not option_orders:
        return []
    held = {str(k): float(v) for k, v in (equity_positions or {}).items()}
    fails = []
    for o in option_orders:
        u = o.get("underlying")
        contracts = float(o.get("contracts", 0))
        size = float(o.get("contract_size", _CALL_CONTRACT_SIZE))
        need = contracts * size
        have = held.get(str(u), 0.0)
        if have < need - _TOL:
            fails.append(f"naked call {u}: need {need:.0f} sh, hold {have:.0f}")
        exp = o.get("expiration")
        if exp is not None and as_of is not None:
            exp_d = exp if isinstance(exp, date) else date.fromisoformat(str(exp)[:10])
            if exp_d < as_of:
                fails.append(f"expired option {u}: {exp_d} < {as_of}")
    return fails


def check_pretrade(
    target_weights: pd.Series,
    *,
    settings,
    sector_map: pd.Series,
    universe: Iterable[str],
    equity_positions: Mapping[str, float] | None = None,
    option_orders: Sequence[Mapping] | None = None,
    as_of: date | None = None,
    leverage: float | None = None,
) -> RiskCheckResult:
    """Run every pre-trade check on the proposed post-trade book.

    ``target_weights`` are fractions of the deployable base (the proposed equity
    holdings). ``leverage`` (gross/equity, supplied by the caller) is checked against
    ``settings.portfolio.max_leverage`` when given. Returns a :class:`RiskCheckResult`;
    logs at ERROR with all reasons when it blocks.
    """
    p = settings.portfolio
    w = target_weights.dropna()
    failures: list[str] = []
    failures += _check_long_only(w)
    failures += _check_single_name(w, p.max_single_name_pct)
    failures += _check_sector(w, sector_map, p.max_sector_pct)
    failures += _check_leverage(w)
    if leverage is not None:
        failures += _check_leverage_cap(leverage, float(getattr(p, "max_leverage", 1.0)))
    failures += _check_universe(w, universe)
    failures += check_covered_call_coverage(option_orders, equity_positions, as_of)

    result = RiskCheckResult(approved=not failures, failures=failures)
    if not result.approved:
        log.error("pre-trade risk gate BLOCKED", extra={"failures": failures, "n_names": len(w)})
    else:
        log.info("pre-trade risk gate passed", extra={"n_names": len(w)})
    return result
