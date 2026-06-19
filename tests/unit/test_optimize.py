"""Unit tests for engine.optimize — convex QP + min-position cleanup, no network.

Synthetic μ/Σ/sectors. Guards: rank-μ monotonicity, the convex constraints
(budget, max single name, sector cap, Unknown exemption), the min-position
cleanup (no name left in (0, floor)), and the end-to-end pipeline including the
20–30 name band and the infeasible→hold fallback.
"""

from __future__ import annotations

from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

from engine import optimize


def _diag_sigma(names, var=0.04):
    return pd.DataFrame(np.diag([var] * len(names)), index=names, columns=names)


def _settings(**over):
    p = dict(nav=100_000, min_position_usd=4_000, max_single_name_pct=0.10,
             max_sector_pct=0.30, base_equity_allocation=0.95,
             infeasibility_max_retries=3, risk_aversion_lambda=1.0)
    o = dict(preselect_top_k=50, target_return_scale=0.15)
    p.update(over.pop("portfolio", {}))
    o.update(over.pop("optimizer", {}))
    return SimpleNamespace(portfolio=SimpleNamespace(**p), optimizer=SimpleNamespace(**o))


# --------------------------------------------------------------------- μ ----
def test_compute_mu_monotonic_in_score():
    scores = pd.Series({"A": -2.0, "B": 0.0, "C": 1.0, "D": 3.0})
    mu = optimize.compute_mu(scores, target_return_scale=0.15)
    assert mu["D"] > mu["C"] > mu["B"] > mu["A"]
    assert mu["D"] == pytest.approx(0.15)            # top rank = N/N * scale
    assert mu.drop("D").max() < 0.15


def test_compute_mu_drops_nan():
    mu = optimize.compute_mu(pd.Series({"A": 1.0, "B": np.nan}), 0.15)
    assert list(mu.index) == ["A"]


# ------------------------------------------------------------------ solve_qp ----
def test_solve_qp_concentrates_on_high_mu_and_respects_budget():
    names = ["A", "B", "C"]
    mu = pd.Series({"A": 0.20, "B": 0.10, "C": 0.05})
    sectors = pd.Series({n: "Unknown" for n in names})
    w = optimize.solve_qp(mu, _diag_sigma(names), sectors,
                          budget=0.20, w_max=0.10, sector_cap=1.0, lam=1e-3)
    assert w is not None
    assert w.sum() == pytest.approx(0.20, abs=1e-4)
    assert (w <= 0.10 + 1e-6).all() and (w >= -1e-6).all()
    assert w["A"] == pytest.approx(0.10, abs=1e-3)   # top name pinned at the cap


def test_solve_qp_max_single_name_cap_binds():
    names = list("ABCDE")
    mu = pd.Series({"A": 1.0, "B": 0.1, "C": 0.1, "D": 0.1, "E": 0.1})
    sectors = pd.Series({n: "Unknown" for n in names})
    w = optimize.solve_qp(mu, _diag_sigma(names), sectors,
                          budget=0.30, w_max=0.10, sector_cap=1.0, lam=1e-4)
    assert w["A"] == pytest.approx(0.10, abs=1e-3)   # cannot exceed max single name


def test_solve_qp_sector_cap_binds():
    names = ["T1", "T2", "T3", "E1", "E2"]
    mu = pd.Series({"T1": 0.9, "T2": 0.8, "T3": 0.7, "E1": 0.1, "E2": 0.05})
    sectors = pd.Series({"T1": "Tech", "T2": "Tech", "T3": "Tech", "E1": "Energy", "E2": "Energy"})
    # Without the cap the three Tech names would take 0.45 (3 × 0.15).
    w = optimize.solve_qp(mu, _diag_sigma(names), sectors,
                          budget=0.50, w_max=0.15, sector_cap=0.30, lam=1e-3)
    tech = w[["T1", "T2", "T3"]].sum()
    assert tech <= 0.30 + 1e-5


def test_solve_qp_unknown_sector_is_exempt_from_cap():
    names = list("ABCD")
    mu = pd.Series({n: 0.5 for n in names})
    sectors = pd.Series({n: "Unknown" for n in names})
    # 4 × 0.10 = 0.40 > 0.30; allowed only because Unknown is exempt.
    w = optimize.solve_qp(mu, _diag_sigma(names), sectors,
                          budget=0.40, w_max=0.10, sector_cap=0.30, lam=1e-3)
    assert w is not None
    assert w.sum() == pytest.approx(0.40, abs=1e-4)


def test_solve_qp_lambda_zero_is_pure_alpha_lp():
    # λ=0 (DECISIONS D25): no risk term => maximize mu'w, so the highest-mu names
    # fill to the cap. Σ is irrelevant; pass an absurd one to prove it's unused.
    names = list("ABCDE")
    mu = pd.Series({"A": 0.5, "B": 0.4, "C": 0.3, "D": 0.2, "E": 0.1})
    sectors = pd.Series({n: "Unknown" for n in names})
    junk_sigma = pd.DataFrame(np.full((5, 5), -999.0), index=names, columns=names)
    w = optimize.solve_qp(mu, junk_sigma, sectors,
                          budget=0.20, w_max=0.05, sector_cap=1.0, lam=0.0)
    assert w is not None
    assert w.sum() == pytest.approx(0.20, abs=1e-4)
    # Top-4 by mu funded at the cap; lowest-mu E gets nothing.
    assert w[["A", "B", "C", "D"]].min() == pytest.approx(0.05, abs=1e-3)
    assert w["E"] == pytest.approx(0.0, abs=1e-3)


def test_solve_qp_infeasible_returns_none():
    names = ["A", "B"]
    mu = pd.Series({"A": 0.2, "B": 0.1})
    sectors = pd.Series({n: "Unknown" for n in names})
    # Budget 0.95 unreachable with two names capped at 0.10 each.
    w = optimize.solve_qp(mu, _diag_sigma(names), sectors,
                          budget=0.95, w_max=0.10, sector_cap=1.0, lam=1e-3)
    assert w is None


# --------------------------------------------------------------- cleanup ----
def test_solve_with_cleanup_enforces_min_position():
    names = [f"S{i}" for i in range(12)]
    mu = pd.Series(np.linspace(0.20, 0.02, 12), index=names)
    funded = optimize._solve_with_cleanup(
        mu, _diag_sigma(names), pd.Series({n: "Unknown" for n in names}),
        budget=0.95, w_max=0.10, sector_cap=1.0, lam=0.5, w_min=0.04,
    )
    assert funded is not None
    assert (funded >= 0.04 - 1e-9).all()             # every funded name clears the floor
    assert funded.sum() == pytest.approx(0.95, abs=1e-3)


# ------------------------------------------------------ optimize_portfolio ----
# Six sectors so a 30% cap leaves the 95% budget deployable (2 sectors couldn't).
_SIX = ["Tech", "Energy", "Health Care", "Financials", "Industrials", "Materials"]


def test_optimize_portfolio_end_to_end_band_and_cash():
    names = [f"S{i}" for i in range(30)]
    scores = pd.Series(np.linspace(3.0, -3.0, 30), index=names)   # S0 best
    sigma = _diag_sigma(names, var=0.05)
    sectors = pd.Series({n: _SIX[i % len(_SIX)] for i, n in enumerate(names)})
    res = optimize.optimize_portfolio(scores, sigma, sectors, settings=_settings())

    assert res.status == "optimal"
    assert res.weights.sum() == pytest.approx(0.95, abs=1e-3)
    assert res.cash == pytest.approx(0.05, abs=1e-3)
    assert (res.weights >= 0.04 - 1e-9).all() and (res.weights <= 0.10 + 1e-6).all()
    assert 10 <= res.n_names <= 23                   # 95% budget / [10%,4%] band
    # Funded names are the top scorers, and the result is sorted high→low.
    assert res.weights.index[0] == "S0"
    assert list(res.weights) == sorted(res.weights, reverse=True)


def test_optimize_portfolio_respects_sector_cap_end_to_end():
    names = [f"S{i}" for i in range(30)]
    scores = pd.Series(np.linspace(3.0, -3.0, 30), index=names)
    sigma = _diag_sigma(names, var=0.05)
    # The six best-scoring names are all Tech (pressures the cap); the rest spread
    # across other sectors so 95% is still deployable.
    sectors = pd.Series({
        n: ("Tech" if i < 6 else _SIX[1 + (i % (len(_SIX) - 1))])
        for i, n in enumerate(names)
    })
    res = optimize.optimize_portfolio(scores, sigma, sectors, settings=_settings())
    by_sector = res.weights.groupby(sectors.reindex(res.weights.index)).sum()
    assert by_sector.get("Tech", 0.0) <= 0.30 + 1e-4
    assert res.weights.sum() == pytest.approx(0.95, abs=1e-3)


def test_optimize_portfolio_infeasible_holds_previous():
    names = ["A", "B", "C"]                          # 3 × 0.10 = 0.30 < 0.95 budget
    scores = pd.Series({"A": 2.0, "B": 1.0, "C": 0.0})
    sigma = _diag_sigma(names)
    sectors = pd.Series({n: "Unknown" for n in names})
    prev = pd.Series({"X": 0.5, "Y": 0.45})
    res = optimize.optimize_portfolio(scores, sigma, sectors, settings=_settings(), prev_weights=prev)
    assert res.status == "held"
    pd.testing.assert_series_equal(res.weights, prev)


def test_optimize_portfolio_excludes_names_missing_from_sigma():
    names = [f"S{i}" for i in range(20)]
    scores = pd.Series(np.linspace(3.0, -3.0, 20), index=names)
    # Σ omits the top name S0 (e.g. dropped for thin history) — it must not be funded.
    modelled = names[1:]
    sigma = _diag_sigma(modelled, var=0.05)
    sectors = pd.Series({n: "Unknown" for n in names})
    res = optimize.optimize_portfolio(scores, sigma, sectors, settings=_settings())
    assert "S0" not in res.weights.index
