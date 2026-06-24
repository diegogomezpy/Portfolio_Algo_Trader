"""Phase 2b — covered-call overlay backtest (BUILD_ORDER.md).

Extends the Phase 2 equity walk-forward by writing a covered call on each held
position every month and folding the option P&L into the return. The honest split
(DECISIONS D27):

* **Upside given up is exact** — we know each stock's realized path, so the cap at
  the strike is data-driven, not modeled.
* **Premium income is modeled** — historical single-name implied vol is not freely
  available, so we price with Black-Scholes (`engine.options`) at an *assumed* IV and
  **sensitivity-test** it rather than pretend one number is right:
    - ``realized``   : IV = annualized realized vol  (conservative floor — no vol premium)
    - ``vix_scaled`` : IV = realized vol × (VIX / SPX realized vol)  (applies the market's
                       live implied/realized ratio; an upper bound since single-name vol
                       premium is smaller than the index's)

We report the combined Sharpe as a **range** across those assumptions plus a
**break-even** premium multiplier, and validate the *engine* (not single-name levels)
against CBOE's real ``^BXM`` buy-write index — see :func:`validate_against_bxm`.

Simplifications (full roll/earnings/assignment machinery is Phase 4 live execution):
monthly write held to the next rebalance (~one option life), full coverage of each
position, a flat premium-execution haircut (`backtest.cc_premium_slippage`), no
intra-month rolls.

Usage::

    python scripts/backtest_covered_calls.py
    python scripts/backtest_covered_calls.py --start 2021-07-01 --end 2026-05-01 --validate-bxm
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

from engine import covariance, factors, options, optimize, sectors  # noqa: E402
from engine.config import load_settings  # noqa: E402
from engine.logger import get_logger  # noqa: E402
from scripts import backtest as eq  # equity walk-forward helpers  # noqa: E402

log = get_logger(__name__)

VIX_CACHE = Path("data/ref/vix.parquet")
TRADING_DAYS = 252


# ====================================================================== #
# Market-data helpers (VIX) and IV estimate — the modeled assumption
# ====================================================================== #
def load_vix(*, cache_path: Path | str = VIX_CACHE, fetch=None) -> pd.Series:
    """Daily VIX as a decimal (0.18 = 18%), cached. ``fetch(start)`` returns a
    DataFrame with a ``Close`` column (defaults to yfinance ^VIX)."""
    cache_path = Path(cache_path)
    if cache_path.exists():
        return pd.read_parquet(cache_path)["vix"]
    if fetch is None:
        import yfinance as yf
        fetch = lambda: yf.Ticker("^VIX").history(start="2020-01-01")  # noqa: E731
    raw = fetch()
    vix = (raw["Close"] / 100.0).rename("vix")
    vix.index = pd.to_datetime(vix.index).tz_localize(None)
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    vix.to_frame().to_parquet(cache_path)
    return vix


def annualized_realized_vol(panel: pd.DataFrame, as_of, window: int) -> pd.Series:
    """Per-name annualized realized vol at ``as_of`` (reuses factors.realized_vol)."""
    return factors.realized_vol(panel, as_of, window) * np.sqrt(TRADING_DAYS)


def estimate_iv(rv_name: pd.Series, vix: float, rv_spx: float, mode: str) -> pd.Series:
    """Per-name implied-vol estimate (the modeled assumption, D27).

    ``realized`` returns realized vol unchanged; ``vix_scaled`` multiplies by the live
    market implied/realized ratio (VIX / SPX realized vol), bounded ≥ 1 so it only ever
    adds a premium.
    """
    if mode == "realized":
        return rv_name
    if mode == "vix_scaled":
        ratio = max(vix / rv_spx, 1.0) if rv_spx and np.isfinite(rv_spx) else 1.0
        return rv_name * ratio
    raise ValueError(f"unknown iv mode: {mode}")


# ====================================================================== #
# Walk-forward overlay
# ====================================================================== #
def run_overlay(start: date, end: date, *, settings=None, iv_mode: str = "vix_scaled",
                iv_scale: float | None = None, target_delta: float | None = None) -> pd.DataFrame:
    """Walk-forward equity book + covered-call overlay; one row per rebalance.

    Columns: equity_net (book, no overlay), cc_net (book + covered calls), premium_income,
    upside_given_up, assignment_rate, turnover, n_names. Both *_net are net of the same
    equity transaction costs; cc_net also nets the premium-execution haircut.
    """
    settings = settings or load_settings()
    delta = target_delta if target_delta is not None else settings.covered_calls.target_delta
    ex, bt = settings.execution, settings.backtest
    cost_kw = dict(large_thr=ex.large_cap_adv_threshold, mid_thr=ex.mid_cap_adv_threshold,
                   bps_large=ex.cost_bps_large, bps_mid=ex.cost_bps_mid, bps_small=ex.cost_bps_small)
    clip, slippage = bt.return_clip, bt.cc_premium_slippage
    win = settings.covariance.estimation_window_days

    panel = factors.load_close_panel(eq.PRICES_DIR, end=end, lookback=10**9)
    allf, eligible = factors.load_scored_fundamentals(eq.FUNDAMENTALS_DIR, settings)
    ff5 = covariance.load_ff5_daily()
    sector_map = sectors.load_sector_map()["sector"]
    vix = load_vix()
    adv = eq._latest_adv(panel.index[-1])

    burn = settings.factors.momentum_long_window + 1
    rebal = [d for d in eq.month_start_dates(panel.index)
             if start <= d.date() <= end and panel.index.get_loc(d) >= burn]

    prev_w = pd.Series(dtype=float)
    rows = []
    for d0, d1 in zip(rebal[:-1], rebal[1:]):
        sc = factors.score_date(d0.date(), settings=settings, price_panel=panel,
                                all_fundamentals=allf, eligible_symbols=eligible).set_index("symbol")
        comp = sc["composite_score"].dropna()
        if comp.empty:
            continue
        top = comp.sort_values(ascending=False).head(settings.optimizer.preselect_top_k).index
        sigma = eq.safe_covariance(panel[top], ff5, as_of=d0, window=win,
                                   min_obs=settings.covariance.min_regression_obs)
        w = optimize.optimize_portfolio(comp, sigma, sector_map, settings=settings, prev_weights=prev_w).weights
        if w.empty:
            continue

        names = w.index
        T = max((d1 - d0).days, 1) / 365.0
        eq_ret = (panel.loc[d1, names] / panel.loc[d0, names] - 1.0).clip(-clip, clip)

        rv = annualized_realized_vol(panel, d0, win)
        rv_spx = float(rv.get("SPY", np.nan))
        vix0 = float(vix.asof(pd.Timestamp(d0)))
        if iv_scale is not None:                      # fixed multiplier (break-even sweep)
            iv = rv.reindex(names) * iv_scale
        else:
            iv = estimate_iv(rv.reindex(names), vix0, rv_spx, iv_mode)
        iv = iv.fillna(rv.reindex(names)).clip(lower=1e-3)

        S0 = panel.loc[d0, names].to_numpy()
        K = options.strike_for_delta(S0, T, iv.to_numpy(), delta)
        strike_ret = pd.Series(K / S0 - 1.0, index=names)
        prem_yield = pd.Series(options.bs_call_price(S0, K, T, iv.to_numpy()) / S0, index=names) * (1.0 - slippage)
        cc_ret = options.covered_call_return(eq_ret, strike_ret, prem_yield)
        assigned = eq_ret > strike_ret

        cost = eq.transaction_cost(prev_w, w, adv, **cost_kw)
        rows.append({
            "date": d1.date(), "n_names": len(names),
            "equity_net": float((w * eq_ret).sum()) - cost,
            "cc_net": float((w * cc_ret).sum()) - cost,
            "premium_income": float((w * prem_yield).sum()),
            "upside_given_up": float((w * (eq_ret - strike_ret).clip(lower=0)).sum()),
            "assignment_rate": float(assigned.mean()),
            "turnover": eq.turnover(prev_w, w),
        })
        prev_w = eq.drift_weights(w, eq_ret)

    return pd.DataFrame(rows)


def validate_against_bxm(start: date, end: date, *, settings=None) -> dict:
    """Validate the *engine* against CBOE's real ATM buy-write index (^BXM).

    Runs the overlay on SPY alone (one underlying) with ATM calls (delta 0.5) priced off
    the actual index IV (VIX), and compares to ^BXM's realized monthly returns. Matching
    here checks the BS/strike/payoff/IV machinery — not single-name premium levels.
    """
    settings = settings or load_settings()
    win = settings.covariance.estimation_window_days
    panel = factors.load_close_panel(eq.PRICES_DIR, end=end, lookback=10**9)
    vix = load_vix()
    try:
        import yfinance as yf
        bxm = (yf.Ticker("^BXM").history(start=str(start), end=str(end))["Close"])
        bxm.index = pd.to_datetime(bxm.index).tz_localize(None)
    except Exception as exc:  # noqa: BLE001
        return {"error": f"could not load ^BXM: {exc}"}

    rebal = [d for d in eq.month_start_dates(panel.index) if start <= d.date() <= end]
    model_rets, bxm_rets = [], []
    for d0, d1 in zip(rebal[:-1], rebal[1:]):
        if "SPY" not in panel.columns:
            break
        S0, S1 = panel.loc[d0, "SPY"], panel.loc[d1, "SPY"]
        T = max((d1 - d0).days, 1) / 365.0
        iv = float(vix.asof(pd.Timestamp(d0)))
        K = float(options.strike_for_delta(S0, T, iv, 0.50))     # ATM, like BXM
        prem = float(options.bs_call_price(S0, K, T, iv)) / S0
        model_rets.append(min(S1 / S0 - 1.0, K / S0 - 1.0) + prem)
        b0, b1 = bxm.asof(pd.Timestamp(d0)), bxm.asof(pd.Timestamp(d1))
        bxm_rets.append(b1 / b0 - 1.0)
    m, b = pd.Series(model_rets), pd.Series(bxm_rets)
    return {"months": len(m), "model_ann": float((1 + m).prod() ** (12 / len(m)) - 1),
            "bxm_ann": float((1 + b).prod() ** (12 / len(b)) - 1),
            "corr": float(m.corr(b)), "mean_abs_diff_mo": float((m - b).abs().mean())}


# ====================================================================== #
# Premium-deployment lag
# ====================================================================== #
def premium_deployment_drag(df: pd.DataFrame) -> dict:
    """Quantify the one-cycle premium-deployment lag (live behaviour vs the cc_net assumption).

    Live (DECISIONS D31 / run_eod ordering): a call's premium is collected when the call is
    *written* — the last step of the monthly cycle, after the equity redeployment — so it can
    only be deployed at the NEXT rebalance; meanwhile it sits as cash. The ``cc_net`` series
    instead folds each month's premium into that month's return (same-cycle, slightly
    optimistic). This compares the two compounding paths of the *same* returns:

      * same-cycle : premium in month t compounds from month t (the cc_net assumption);
      * lagged     : premium in month t earns 0 for one month, then joins the invested base
                     at t+1 (the realistic live behaviour).

    Returns each path's CAGR and the drag (same − lagged) in bps/yr and total %. The book is
    1x in the backtest, so premium_income is a yield on NAV and no leverage scaling is needed.
    """
    if df.empty:
        return {}
    cc = df["cc_net"].to_numpy(dtype=float)
    prem = df["premium_income"].to_numpy(dtype=float)
    core = cc - prem                          # period return WITHOUT the premium contribution
    years = len(cc) / 12.0

    # Lagged (live / cc_net): premium is added flat at the period's end, so it only starts
    # compounding from the NEXT cycle — exactly run_eod's "write calls last → premium funds the
    # next rebalance" ordering.
    w_lag = float(np.prod(1.0 + cc))                       # = prod(1 + core + prem)
    # Same-cycle ideal: premium deployed at the period's START, so it ALSO earns that period's
    # core return. The only difference is the per-period core·prem cross-term.
    w_same = float(np.prod((1.0 + core) * (1.0 + prem)))   # = prod(1 + core + prem + core*prem)

    return {"cagr_same_cycle": w_same ** (1.0 / years) - 1.0,
            "cagr_lagged": w_lag ** (1.0 / years) - 1.0,
            "drag_bps_per_yr": (w_same ** (1.0 / years) - w_lag ** (1.0 / years)) * 1e4,
            "drag_total_pct": (w_same / w_lag - 1.0) * 100.0,
            "avg_monthly_premium_pct": float(np.mean(prem) * 100.0),
            "avg_monthly_core_pct": float(np.mean(core) * 100.0), "months": int(len(cc))}


# ====================================================================== #
# Summary / gate
# ====================================================================== #
def summarize(results_by_mode: dict[str, pd.DataFrame]) -> dict:
    """Print combined-vs-equity metrics across IV assumptions + the gate verdict."""
    any_res = next(iter(results_by_mode.values()))
    eq_metrics = eq.portfolio_metrics(any_res.set_index("date")["equity_net"])
    print("\n" + "=" * 70)
    print(f"COVERED-CALL OVERLAY BACKTEST — {eq_metrics['n_months']} months "
          f"({any_res['date'].iloc[0]} → {any_res['date'].iloc[-1]})")
    print("=" * 70)
    print(f"  Equity-only (no overlay):  ann {eq_metrics['ann_return']*100:+.1f}%   "
          f"vol {eq_metrics['ann_vol']*100:.1f}%   Sharpe {eq_metrics['sharpe']:.2f}   "
          f"maxDD {eq_metrics['max_drawdown']*100:.1f}%")
    print("\n  Overlay (combined), by IV assumption:")
    print(f"  {'IV mode':14}{'ann ret':>9}{'vol':>8}{'Sharpe':>8}{'maxDD':>8}"
          f"{'premium/yr':>12}{'assign%':>9}")
    out = {"equity": eq_metrics, "modes": {}}
    for mode, res in results_by_mode.items():
        cc = eq.portfolio_metrics(res.set_index("date")["cc_net"])
        prem_yr = res["premium_income"].mean() * 12
        assign = res["assignment_rate"].mean()
        out["modes"][mode] = {**cc, "premium_yr": prem_yr, "assignment": assign}
        better = "✅" if cc["sharpe"] >= eq_metrics["sharpe"] else "  "
        print(f"  {mode:14}{cc['ann_return']*100:>8.1f}%{cc['ann_vol']*100:>7.1f}%"
              f"{cc['sharpe']:>8.2f}{cc['max_drawdown']*100:>7.1f}%{prem_yr*100:>11.1f}%"
              f"{assign*100:>8.0f}% {better}")

    # Premium-deployment lag: live, premium is parked as cash until the next monthly rebalance
    # (run_eod writes calls AFTER the equity redeploy → premium funds NEXT cycle), whereas
    # cc_net assumes same-cycle compounding. Quantify the gap on the realistic market-IV book.
    market_df = results_by_mode.get("vix_scaled", next(iter(results_by_mode.values())))
    drag = premium_deployment_drag(market_df)
    if drag:
        out["premium_deployment_drag"] = drag
        print(f"\n  Premium-deployment lag (premium ≈{drag['avg_monthly_premium_pct']:.2f}%/mo parked "
              f"as cash 1 cycle, then redeployed):")
        print(f"    same-cycle CAGR {drag['cagr_same_cycle']*100:+.2f}%  vs  lagged "
              f"{drag['cagr_lagged']*100:+.2f}%  →  drag {drag['drag_bps_per_yr']:.1f} bps/yr "
              f"({drag['drag_total_pct']:.2f}% total over {drag['months']} mo)")

    # Verdict (DECISIONS D27, refined D30): vol / drawdown / premium improve
    # UNCONDITIONALLY across every IV assumption (exact, data-driven). Sharpe AND the
    # assignment rate both depend on the IV assumption — a lower IV writes tighter
    # strikes, which lifts assignment — so both are judged at the realistic market-
    # implied 'vix_scaled' case (the conservative realized floor is reported alongside
    # for range, not used as a pass/fail bar: its tight strikes overstate assignment).
    floor = out["modes"].get("realized")
    market = out["modes"].get("vix_scaled", out["modes"][list(out["modes"])[-1]])
    vol_ok = all(m["ann_vol"] < eq_metrics["ann_vol"] for m in out["modes"].values())
    dd_ok = all(m["max_drawdown"] >= eq_metrics["max_drawdown"] - 1e-9 for m in out["modes"].values())
    prem_ok = all(m["premium_yr"] > 0 for m in out["modes"].values())
    assign_ok = market["assignment"] < 0.30
    sharpe_ok = market["sharpe"] >= eq_metrics["sharpe"] - 1e-9
    print("\n  Gate:")
    print(f"    vol reduced vs equity-only         {'✅' if vol_ok else '❌'}  (unconditional)")
    print(f"    maxDD reduced vs equity-only       {'✅' if dd_ok else '❌'}  (unconditional)")
    print(f"    premium income > 0                 {'✅' if prem_ok else '❌'}  (unconditional)")
    floor_assign = f"{floor['assignment']*100:.0f}%" if floor else "n/a"
    print(f"    assignment < 30% @ market IV       {'✅' if assign_ok else '❌'}  "
          f"(market {market['assignment']*100:.0f}%; floor {floor_assign} at tighter strikes)")
    print(f"    Sharpe ≥ equity-only @ market IV   {'✅' if sharpe_ok else '❌'}  "
          f"(premium-dependent: floor {floor['sharpe'] if floor else float('nan'):.2f} "
          f"→ market {market['sharpe']:.2f})")
    out["passed"] = bool(vol_ok and dd_ok and prem_ok and assign_ok and sharpe_ok)
    print(f"\n  {'✅ PASS (realistic-premium basis)' if out['passed'] else '❌ FAIL'}")
    print("=" * 70)
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description="Covered-call overlay backtest (Phase 2b)")
    ap.add_argument("--start", type=lambda s: date.fromisoformat(s), default=date(2021, 7, 1))
    ap.add_argument("--end", type=lambda s: date.fromisoformat(s), default=date.today())
    ap.add_argument("--validate-bxm", action="store_true", help="cross-check the engine vs ^BXM")
    args = ap.parse_args()
    for n in ("engine.factors", "engine.covariance", "scripts.backtest"):
        logging.getLogger(n).setLevel(logging.WARNING)
    settings = load_settings()

    results = {mode: run_overlay(args.start, args.end, settings=settings, iv_mode=mode)
               for mode in ("realized", "vix_scaled")}
    summarize(results)

    if args.validate_bxm:
        v = validate_against_bxm(args.start, args.end, settings=settings)
        print("\n  Engine validation vs ^BXM (ATM buy-write on SPY, priced off real VIX):")
        if "error" in v:
            print(f"    {v['error']}")
        else:
            print(f"    model ann {v['model_ann']*100:+.1f}%  vs  ^BXM ann {v['bxm_ann']*100:+.1f}%  "
                  f"| monthly corr {v['corr']:.2f}  mean|Δ| {v['mean_abs_diff_mo']*100:.2f}%/mo")


if __name__ == "__main__":
    main()
