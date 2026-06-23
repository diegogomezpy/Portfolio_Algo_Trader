"""Walk-forward portfolio backtest — the Phase 2 gate (BUILD_ORDER.md).

Unlike the Phase 1 sanity check (`backtest_factors.py`, quintile spreads on raw
scores), this runs the *actual strategy* month by month: score → covariance →
optimizer → target weights, then realizes the held book's net-of-cost return.

Each rebalance (first trading day of the month, mid-2021 → latest after the
252-day burn-in):

1. Score the universe (`engine.factors`); pre-select the top-K by composite.
2. Estimate Σ over the pre-selected names (`engine.covariance`, FF5 factor model).
3. Optimize target weights (`engine.optimize`) — ~19 names at the 5% cap (D24).
4. Realize next-month returns on the held names; charge a tiered-bps transaction
   cost on the traded notional vs. the previous (drifted) weights.
5. Track net return, turnover, and four single-factor sleeve returns (each sub-score
   ranked alone, top-N equal-weight) for the "all four factors positive" attribution.

**Realism scope (gate-sufficient).** Monthly rebalance, trade at the adjusted close,
tiered-bps costs, turnover on drifted weights. T+1 settlement is a no-op for monthly
close-to-close returns; partial fills / slippage / a settlement ledger are Phase 3.

**Look-ahead safety.** Scores/Σ at the rebalance date use only data up to it; returns
are strictly the following month. A held name's monthly return is clipped to
±`backtest.return_clip` purely as a data-glitch guard (the liquid held book triggers
it rarely, and every trigger is logged) — not a routine winsorization.

Gate: net Sharpe > 1.0, average monthly turnover < 30%, all four factor sleeves
positive, and no Sharpe > 3 (a too-good number flags look-ahead leakage).

Usage::

    python scripts/backtest.py
    python scripts/backtest.py --start 2021-07-01 --end 2026-05-01
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from engine import covariance, factors, optimize, sectors  # noqa: E402
from engine.config import load_settings  # noqa: E402
from engine.logger import get_logger  # noqa: E402

log = get_logger(__name__)

PRICES_DIR = "data/raw/equities"
FUNDAMENTALS_DIR = "data/raw/fundamentals"
RESULTS_PATH = Path("data/backtest/walkforward.parquet")
SUBSCORES = ["quality_score", "value_score", "momentum_score", "lowvol_score"]


# ====================================================================== #
# Pure helpers — unit-tested
# ====================================================================== #
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


def safe_covariance(price_panel, ff5, *, as_of, window, min_obs):
    """``covariance.covariance_from_prices`` that degrades to an empty Σ instead of
    raising when the FF5 factor data doesn't cover enough of the window.

    Ken French's daily file lags publication, so a window dated near the live edge can
    overlap too few factor days to estimate Σ. With λ=0 the optimizer doesn't use Σ
    anyway (it falls back to the full top-K when Σ is empty — see engine.optimize), so a
    publication gap should keep the rebalance alive, not crash it. In-sample the window
    is fully covered, so this returns the real Σ and changes nothing.
    """
    try:
        return covariance.covariance_from_prices(price_panel, ff5, as_of=as_of,
                                                 window=window, min_obs=min_obs)
    except ValueError as exc:
        log.warning("covariance unavailable; proceeding without Σ",
                    extra={"as_of": str(as_of), "error": str(exc)})
        return pd.DataFrame()


# ====================================================================== #
# Walk-forward driver
# ====================================================================== #
def run_walkforward(start: date, end: date, *, settings=None) -> pd.DataFrame:
    """Run the monthly walk-forward backtest; return one row per rebalance."""
    settings = settings or load_settings()
    ex, bt = settings.execution, settings.backtest
    cost_kw = dict(large_thr=ex.large_cap_adv_threshold, mid_thr=ex.mid_cap_adv_threshold,
                   bps_large=ex.cost_bps_large, bps_mid=ex.cost_bps_mid, bps_small=ex.cost_bps_small)
    clip = bt.return_clip
    bench = bt.benchmark

    log.info("loading panel / fundamentals / FF5 / sectors")
    panel = factors.load_close_panel(PRICES_DIR, end=end, lookback=10**9)
    allf, eligible = factors.load_scored_fundamentals(FUNDAMENTALS_DIR, settings)
    ff5 = covariance.load_ff5_daily()
    sector_map = sectors.load_sector_map()["sector"]
    adv = _latest_adv(panel.index[-1])

    burn = settings.factors.momentum_long_window + 1
    rebal = [d for d in month_start_dates(panel.index)
             if start <= d.date() <= end and panel.index.get_loc(d) >= burn]
    if len(rebal) < 2:
        raise SystemExit(f"only {len(rebal)} scorable months in range — need ≥ 2 (burn-in {burn}d)")

    prev_w = pd.Series(dtype=float)
    rows = []
    for d0, d1 in zip(rebal[:-1], rebal[1:]):
        sc = factors.score_date(d0.date(), settings=settings, price_panel=panel,
                                all_fundamentals=allf, eligible_symbols=eligible).set_index("symbol")
        composite = sc["composite_score"].dropna()
        if composite.empty:
            continue
        top = composite.sort_values(ascending=False).head(settings.optimizer.preselect_top_k).index
        sigma = safe_covariance(
            panel[top], ff5, as_of=d0, window=settings.covariance.estimation_window_days,
            min_obs=settings.covariance.min_regression_obs)
        res = optimize.optimize_portfolio(composite, sigma, sector_map, settings=settings, prev_weights=prev_w)
        w = res.weights
        if w.empty:
            continue

        fwd_all = (panel.loc[d1] / panel.loc[d0] - 1.0)
        held_fwd = _clip_returns(fwd_all.reindex(w.index), clip, d1)
        gross = float((w * held_fwd).sum())                  # cash (1−Σw) earns 0
        cost = transaction_cost(prev_w, w, adv, **cost_kw)
        net = gross - cost
        tno = turnover(prev_w, w)

        n_held = len(w)
        sleeves = {s.replace("_score", ""): sleeve_return(sc[s], fwd_all, n_held) for s in SUBSCORES}
        bench_ret = float(fwd_all.get(bench, np.nan))

        rows.append({"date": d1.date(), "n_names": n_held, "status": res.status,
                     "gross_return": gross, "cost": cost, "net_return": net,
                     "turnover": tno, "benchmark_return": bench_ret, **sleeves})
        prev_w = drift_weights(w, held_fwd)

    results = pd.DataFrame(rows)
    RESULTS_PATH.parent.mkdir(parents=True, exist_ok=True)
    results.to_parquet(RESULTS_PATH)
    log.info("walk-forward complete", extra={"months": len(results), "out": str(RESULTS_PATH)})
    return results


def _clip_returns(fwd: pd.Series, clip: float, d1) -> pd.Series:
    """Clip held-name monthly returns to ±``clip``; log any trigger (data-glitch guard)."""
    hit = fwd[(fwd.abs() > clip)]
    if len(hit):
        log.warning("return clip triggered", extra={"date": str(getattr(d1, 'date', lambda: d1)()),
                                                     "names": {k: round(v, 2) for k, v in hit.items()}})
    return fwd.clip(-clip, clip)


def _latest_adv(latest_date) -> pd.Series:
    """ADV per symbol from the most recent equities snapshot (for cost tiering).

    The current snapshot's ADV is reused for *every* historical rebalance. This is a
    mild look-ahead, but it only selects which bps tier a name pays — it never touches
    returns — and the liquid held book rarely changes tier over the window, so the
    effect on net return is negligible. Point-in-time ADV would be the precise version.
    """
    from engine import ingest
    files = sorted(Path(PRICES_DIR).glob("*.parquet"))
    if not files:
        return pd.Series(dtype=float)
    snap = ingest.load_equities(files[-1].stem, PRICES_DIR)
    return snap["adv_20d"] if "adv_20d" in snap.columns else pd.Series(dtype=float)


# ====================================================================== #
# Summary / gate
# ====================================================================== #
def summarize(results: pd.DataFrame) -> dict:
    """Print the Phase 2 gate verdict and return the computed metrics."""
    net = results.set_index("date")["net_return"]
    gross = results.set_index("date")["gross_return"]
    bench = results.set_index("date")["benchmark_return"]
    m = portfolio_metrics(net)
    mg = portfolio_metrics(gross)
    mb = portfolio_metrics(bench)
    sleeves = {s.replace("_score", ""): portfolio_metrics(results.set_index("date")[s.replace("_score", "")])["ann_return"]
               for s in SUBSCORES}
    avg_turnover = float(results["turnover"].mean())

    print("\n" + "=" * 64)
    print(f"WALK-FORWARD BACKTEST — {m['n_months']} months "
          f"({results['date'].iloc[0]} → {results['date'].iloc[-1]})")
    print("=" * 64)
    print(f"  Annualized return (net / gross): {m['ann_return']*100:+.1f}% / {mg['ann_return']*100:+.1f}%")
    print(f"  Annualized volatility          : {m['ann_vol']*100:.1f}%")
    print(f"  Sharpe (net)                   : {m['sharpe']:+.2f}")
    print(f"  Max drawdown                   : {m['max_drawdown']*100:.1f}%")
    print(f"  Calmar                         : {m['calmar']:.2f}")
    print(f"  Avg monthly turnover           : {avg_turnover*100:.1f}%")
    print(f"  Benchmark ({results['benchmark_return'].notna().any() and 'SPY' or 'n/a'}) ann. return    : {mb['ann_return']*100:+.1f}%")
    print("\n  Single-factor sleeve annualized returns (all should be > 0):")
    for name, ann in sleeves.items():
        flag = "✅" if ann > 0 else "❌"
        print(f"    {name:10s}: {ann*100:+.1f}%  {flag}")

    sharpe_ok = m["sharpe"] is not None and m["sharpe"] > 1.0
    turn_ok = avg_turnover < 0.30
    sleeves_ok = all(v > 0 for v in sleeves.values())
    no_leak = not (m["sharpe"] is not None and m["sharpe"] > 3.0)
    passed = sharpe_ok and turn_ok and sleeves_ok and no_leak
    print("\n  Gate:")
    print(f"    net Sharpe > 1.0          {'✅' if sharpe_ok else '❌'}  ({m['sharpe']:+.2f})")
    print(f"    turnover < 30%/mo         {'✅' if turn_ok else '❌'}  ({avg_turnover*100:.1f}%)")
    print(f"    all 4 factors positive    {'✅' if sleeves_ok else '❌'}")
    print(f"    no leakage (Sharpe ≤ 3)   {'✅' if no_leak else '❌'}")
    print(f"\n  {'✅ PASS' if passed else '❌ FAIL'}")
    print("=" * 64)
    return {**m, "avg_turnover": avg_turnover, "sleeves": sleeves, "passed": passed}


def main() -> None:
    settings = load_settings()
    ap = argparse.ArgumentParser(description="Walk-forward portfolio backtest (Phase 2 gate)")
    ap.add_argument("--start", type=lambda s: date.fromisoformat(s), default=date(2021, 7, 1))
    ap.add_argument("--end", type=lambda s: date.fromisoformat(s), default=date.today())
    ap.add_argument("--quiet", action="store_true", help="suppress per-month factor logs")
    args = ap.parse_args()
    if args.quiet:
        logging.getLogger("engine.factors").setLevel(logging.WARNING)
    results = run_walkforward(args.start, args.end, settings=settings)
    summarize(results)


if __name__ == "__main__":
    main()
