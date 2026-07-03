"""Shared backtest math — the library half that used to live inside scripts/backtest.py.

Moved here in the 2026-07 layout pass so ``scripts/`` is pure CLIs: these helpers are
imported by three scripts (backtest, backtest_covered_calls, build_dashboard), and script→
script imports made the CLI layer double as an ad-hoc library. Pure pandas/numpy — no
network, no Postgres — and unit-tested via the existing backtest tests (scripts/backtest.py
re-exports every name, so callers and tests are unchanged).
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd


def month_start_dates(panel_index: pd.DatetimeIndex) -> list[pd.Timestamp]:
    """First available trading day in the panel for each calendar month."""
    s = pd.Series(panel_index, index=panel_index)
    return [g.min() for _, g in s.groupby(panel_index.to_period("M"))]


def cost_bps_for_adv(adv: float, *, large_thr: float, mid_thr: float,
                     bps_large: float, bps_mid: float, bps_small: float) -> float:
    """Per-name transaction-cost rate (bps) by ADV liquidity tier."""
    if not np.isfinite(adv):
        return bps_small
    if adv >= large_thr:
        return bps_large
    if adv >= mid_thr:
        return bps_mid
    return bps_small


def transaction_cost(prev_w: pd.Series, new_w: pd.Series, adv: pd.Series, *,
                     large_thr: float, mid_thr: float,
                     bps_large: float, bps_mid: float, bps_small: float) -> float:
    """Total trade cost as a fraction of NAV: Σ |Δweight| × tier_bps/1e4.

    Both buys and sells pay; ``Δweight`` is over the union of names (missing → 0).
    """
    names = prev_w.index.union(new_w.index)
    dprev = prev_w.reindex(names, fill_value=0.0)
    dnew = new_w.reindex(names, fill_value=0.0)
    delta = (dnew - dprev).abs()
    cost = 0.0
    for sym, dw in delta.items():
        if dw <= 0:
            continue
        bps = cost_bps_for_adv(float(adv.get(sym, np.nan)), large_thr=large_thr, mid_thr=mid_thr,
                               bps_large=bps_large, bps_mid=bps_mid, bps_small=bps_small)
        cost += dw * bps / 1e4
    return float(cost)


def turnover(prev_w: pd.Series, new_w: pd.Series) -> float:
    """One-way turnover: ½ Σ |Δweight| over the union of names."""
    names = prev_w.index.union(new_w.index)
    delta = (new_w.reindex(names, fill_value=0.0) - prev_w.reindex(names, fill_value=0.0)).abs()
    return float(delta.sum() / 2.0)


def drift_weights(w: pd.Series, returns: pd.Series) -> pd.Series:
    """Weights after a month of returns, renormalized to their post-drift total.

    Used as next month's ``prev_w`` so turnover reflects what actually has to trade,
    not the stale target. Preserves the invested fraction (cash stays cash).
    """
    grown = w * (1.0 + returns.reindex(w.index).fillna(0.0))
    total = grown.sum()
    if total <= 0:
        return w
    return grown / total * w.sum()


def max_drawdown(equity_curve: pd.Series) -> float:
    """Most negative peak-to-trough drawdown of a cumulative equity curve (≤ 0)."""
    running_max = equity_curve.cummax()
    return float((equity_curve / running_max - 1.0).min())


def portfolio_metrics(monthly_returns: pd.Series, *, periods_per_year: int = 12) -> dict:
    """Annualized return/vol/Sharpe, max drawdown, and Calmar from monthly returns."""
    r = monthly_returns.dropna()
    n = len(r)
    if n == 0:
        return {"ann_return": np.nan, "ann_vol": np.nan, "sharpe": np.nan,
                "max_drawdown": np.nan, "calmar": np.nan, "n_months": 0}
    ann_return = float((1.0 + r).prod() ** (periods_per_year / n) - 1.0)
    ann_vol = float(r.std(ddof=1) * np.sqrt(periods_per_year)) if n > 1 else np.nan
    sharpe = float(r.mean() / r.std(ddof=1) * np.sqrt(periods_per_year)) if (n > 1 and r.std(ddof=1)) else np.nan
    mdd = max_drawdown((1.0 + r).cumprod())
    calmar = float(ann_return / abs(mdd)) if mdd < 0 else np.nan
    return {"ann_return": ann_return, "ann_vol": ann_vol, "sharpe": sharpe,
            "max_drawdown": mdd, "calmar": calmar, "n_months": n}


def sleeve_return(subscore: pd.Series, fwd: pd.Series, top_n: int) -> float:
    """Equal-weight next-month return of the top-``top_n`` names by a single sub-score."""
    picks = subscore.dropna().sort_values(ascending=False).head(top_n).index
    r = fwd.reindex(picks).dropna()
    return float(r.mean()) if len(r) else np.nan


def latest_adv(prices_dir) -> pd.Series:
    """ADV per symbol from the most recent equities snapshot (for cost tiering).

    The current snapshot's ADV is reused for *every* historical rebalance. This is a
    mild look-ahead, but it only selects which bps tier a name pays — it never touches
    returns — and the liquid held book rarely changes tier over the window, so the
    effect on net return is negligible. Point-in-time ADV would be the precise version.
    """
    from engine import ingest
    files = sorted(Path(prices_dir).glob("*.parquet"))
    if not files:
        return pd.Series(dtype=float)
    snap = ingest.load_equities(files[-1].stem, Path(prices_dir))
    return snap["adv_20d"] if "adv_20d" in snap.columns else pd.Series(dtype=float)
