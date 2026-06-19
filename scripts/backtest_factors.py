"""Quick factor sanity backtest — the Phase 1 gate check (BUILD_ORDER.md).

This is *not* the full walk-forward backtest (that's Phase 2 with the optimizer and
transaction costs). It answers one question cheaply: **does the composite score sort
stocks in the right direction?** If the top quintile doesn't out-return the bottom
quintile over history, the signal is broken and there's no point building an optimizer
on top of it.

Method, monthly from the first scorable month to the latest data:

1. On the first trading day of the month, score the universe (``engine.factors``).
2. Sort into quintiles by composite score; equal-weight within each.
3. Measure each quintile's *next-month* return (close-to-close on the adjusted panel).
4. Record the top-minus-bottom spread and the rank IC (Spearman corr of composite vs
   next-month return).

Aggregate output: mean monthly long-short spread (+ annualized + long-short Sharpe),
hit rate, mean IC and its t-stat, and the per-quintile return ladder (which should be
roughly monotonic top-to-bottom).

Date range is bounded to the free IEX history floor (~2020-07-27, DECISIONS D21); with
a 252-day momentum/vol burn-in the first scorable month is ~mid-2021. The price panel
and the fundamentals frame are loaded once and reused across every month.

Look-ahead safety: momentum/vol use only prices up to the scoring date, and
fundamentals are lagged ``settings.factors.report_lag_days`` past quarter-end
(``engine.factors.point_in_time_fundamentals``). Forward returns are strictly
out-of-sample relative to the scores that rank on them.

Usage::

    python scripts/backtest_factors.py
    python scripts/backtest_factors.py --start 2021-07-01 --end 2026-06-18 --quintiles 5
"""

from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from engine import factors  # noqa: E402
from engine.config import load_settings  # noqa: E402
from engine.logger import get_logger  # noqa: E402

log = get_logger(__name__)

PRICES_DIR = "data/raw/equities"
FUNDAMENTALS_DIR = "data/raw/fundamentals"


def month_start_dates(panel_index: pd.DatetimeIndex) -> list[pd.Timestamp]:
    """First available trading day in the panel for each calendar month."""
    s = pd.Series(panel_index, index=panel_index)
    return [g.min() for _, g in s.groupby(panel_index.to_period("M"))]


def run_backtest(
    start: date,
    end: date,
    *,
    quintiles: int = 5,
    settings=None,
    fwd_winsor_pct: float | None = None,
) -> pd.DataFrame:
    """Run the monthly quintile/IC sanity backtest; return one row per rebalance.

    ``fwd_winsor_pct`` winsorizes each month's forward returns cross-sectionally to
    strip split/adjustment artifacts (a liquid >$5 name doesn't really return +1000%
    in a month). Defaults to ``settings.factors.winsor_pct``. NOTE: this is a sanity
    backtest hygiene step; the Phase 2 walk-forward backtest should fix adjustment at
    the source (consistent split basis across the panel) rather than winsorize.
    """
    settings = settings or load_settings()
    burn = settings.factors.momentum_long_window + 1
    if fwd_winsor_pct is None:
        fwd_winsor_pct = settings.factors.winsor_pct

    log.info("loading price panel + fundamentals", extra={"start": str(start), "end": str(end)})
    panel = factors.load_close_panel(PRICES_DIR, end=end, lookback=10**9)
    all_fundamentals = factors.load_all_fundamentals(FUNDAMENTALS_DIR)

    rebal = [d for d in month_start_dates(panel.index)
             if start <= d.date() <= end and panel.index.get_loc(d) >= burn]
    if len(rebal) < 2:
        raise SystemExit(
            f"only {len(rebal)} scorable months in range — need ≥2 "
            f"(momentum needs {burn} trading days of burn-in before {start})"
        )

    rows = []
    for d, d_next in zip(rebal[:-1], rebal[1:]):
        scores = factors.score_date(
            d.date(), settings=settings, price_panel=panel, all_fundamentals=all_fundamentals
        ).set_index("symbol")
        fwd = (panel.loc[d_next] / panel.loc[d] - 1.0).reindex(scores.index)
        scores["fwd"] = fwd
        sub = scores.dropna(subset=["composite_score", "fwd"])
        if len(sub) < quintiles * 5:
            continue
        lo, hi = sub["fwd"].quantile(fwd_winsor_pct), sub["fwd"].quantile(1 - fwd_winsor_pct)
        sub["fwd"] = sub["fwd"].clip(lo, hi)
        # rank-then-qcut avoids duplicate-edge errors on tied composite scores
        q = pd.qcut(sub["composite_score"].rank(method="first"), quintiles, labels=False)
        qret = sub.groupby(q)["fwd"].mean()
        ic = sub["composite_score"].corr(sub["fwd"], method="spearman")
        rows.append({
            "date": d.date(),
            "n": len(sub),
            "fund_cov": float((~sub["stale"]).mean()),  # share with real fundamentals
            "top": qret[quintiles - 1],
            "bottom": qret[0],
            "spread": qret[quintiles - 1] - qret[0],
            "ic": ic,
            **{f"q{k+1}": qret[k] for k in range(quintiles)},
        })

    return pd.DataFrame(rows)


def summarize(res: pd.DataFrame, quintiles: int = 5) -> None:
    """Print the aggregate verdict against the Phase 1 gate criteria."""
    n = len(res)
    spread = res["spread"]
    ic = res["ic"].dropna()
    mean_spread = spread.mean()
    ann_spread = mean_spread * 12
    ls_sharpe = (mean_spread / spread.std(ddof=1) * np.sqrt(12)) if spread.std(ddof=1) else float("nan")
    hit = (spread > 0).mean()
    ic_t = (ic.mean() / (ic.std(ddof=1) / np.sqrt(len(ic)))) if len(ic) > 1 else float("nan")
    ladder = res[[f"q{k+1}" for k in range(quintiles)]].mean() * 100

    print("\n" + "=" * 64)
    print(f"FACTOR SANITY BACKTEST — {n} monthly rebalances "
          f"({res['date'].iloc[0]} → {res['date'].iloc[-1]})")
    print("=" * 64)
    print(f"  Mean monthly top–bottom spread : {mean_spread*100:+.2f}%   "
          f"(annualized {ann_spread*100:+.1f}%)")
    print(f"  Long-short Sharpe (×√12)       : {ls_sharpe:+.2f}")
    print(f"  Hit rate (months spread > 0)   : {hit*100:.0f}%")
    print(f"  Mean monthly rank IC           : {ic.mean():+.4f}   (t-stat {ic_t:+.2f})")
    print(f"  Avg universe / month           : {res['n'].mean():.0f} names")
    months_with_fund = int((res["fund_cov"] > 0).sum())
    print(f"  Months with fundamentals        : {months_with_fund}/{n}  "
          f"(avg {res['fund_cov'].mean()*100:.0f}% of names; rest score on "
          f"momentum+low-vol only — yfinance fundamentals begin ~2024-Q3)")
    print("\n  Quintile return ladder (mean monthly %, Q1=worst → "
          f"Q{quintiles}=best):")
    for k in range(quintiles):
        bar = "█" * max(0, int((ladder[f'q{k+1}'] - ladder.min()) * 8))
        print(f"    Q{k+1}: {ladder[f'q{k+1}']:+.2f}%  {bar}")

    monotone = ladder[f"q{quintiles}"] > ladder["q1"]
    print("\n  Gate: top quintile beats bottom quintile —",
          "✅ PASS" if (mean_spread > 0 and monotone) else "❌ FAIL")
    print("=" * 64)


def main() -> None:
    settings = load_settings()
    ap = argparse.ArgumentParser(description="Factor sanity backtest (Phase 1 gate)")
    ap.add_argument("--start", type=lambda s: date.fromisoformat(s), default=date(2021, 7, 1),
                    help="first rebalance month (default 2021-07-01, after momentum burn-in)")
    ap.add_argument("--end", type=lambda s: date.fromisoformat(s), default=date.today(),
                    help="last date to consider (default today)")
    ap.add_argument("--quintiles", type=int, default=5)
    args = ap.parse_args()

    res = run_backtest(args.start, args.end, quintiles=args.quintiles, settings=settings)
    if res.empty:
        raise SystemExit("no rebalances produced — check data range and burn-in")
    summarize(res, quintiles=args.quintiles)


if __name__ == "__main__":
    main()
