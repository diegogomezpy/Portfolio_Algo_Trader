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

from engine import covariance, factors, options, options_data, optimize, sectors  # noqa: E402
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
                iv_scale: float | None = None, target_delta: float | None = None,
                premium_source: str = "model", with_puts: bool = False,
                put_delta: float | None = None) -> pd.DataFrame:
    """Walk-forward equity book + covered-call overlay; one row per rebalance.

    Columns: equity_net (book, no overlay), cc_net (book + covered calls), premium_income,
    upside_given_up, assignment_rate, turnover, n_names. Both *_net are net of the same
    equity transaction costs; cc_net also nets the premium-execution haircut.

    ``premium_source='dolthub'`` (DECISIONS D33) replaces the modeled premium with REAL
    historical call mids (and real strikes) from DoltHub where available — falling back to the
    BS model elsewhere — and records implied-vs-realized vol per name for the variance-risk-
    premium analysis (extra columns: real_coverage, implied_vol, realized_vol_fwd, vrp_vol,
    vrp_var). Default ``'model'`` is the original BS path.

    ``with_puts=True`` adds the **cash-secured-put sleeve** (the optional put-wheel overlay):
    a ``put_delta`` (default = the call delta) OTM put per held name, short-put return =
    premium + min(eq_ret − put_strike_ret, 0). Reported as separate sleeve columns
    (``put_net, put_premium_income, put_assignment_rate`` + put VRP under dolthub) so the wheel
    can be blended within the leverage cap downstream — it does NOT alter the equity/cc columns.
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

    burn = settings.factors.beta_window + 1
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
        strike_ret = pd.Series(K / S0 - 1.0, index=names)                      # BS baseline
        prem_yield = pd.Series(options.bs_call_price(S0, K, T, iv.to_numpy()) / S0, index=names) * (1.0 - slippage)

        # Real premiums + implied vol from DoltHub (D33). Three-way treatment of each held name:
        #   1. real DoltHub chain        → real mid premium + real strike cap (+ VRP data)
        #   2. missing but optionable    → keep the BS-imputed premium (a data gap, still hedgeable)
        #   3. missing & NOT optionable  → leave UNHEDGED (live we couldn't write a call at all)
        impl_v, real_v, real_names, unhedged = {}, {}, set(), []
        if premium_source == "dolthub":
            spot = pd.Series(S0, index=names)
            dte_min, dte_max = settings.covered_calls.min_dte_entry, settings.covered_calls.max_dte_entry
            try:
                chains = options_data.fetch_calls(list(names), d0.date(), dte_min=dte_min, dte_max=dte_max)
            except Exception as exc:  # noqa: BLE001 — one month's API hiccup → BS this month, don't abort
                log.warning("dolthub call fetch failed %s; modeled premiums this month: %s", d0.date(), exc)
                chains = {}
            for s in names:
                c = options_data.select_call(chains.get(s, []), d0.date(), target_delta=delta,
                                             dte_min=dte_min, dte_max=dte_max)
                py = options_data.premium_yield(c, float(spot[s]), slippage=slippage) if c else None
                if py is None:
                    continue
                prem_yield[s] = py                                            # real mid premium
                strike_ret[s] = float(c["strike"]) / float(spot[s]) - 1.0     # real strike cap
                real_names.add(s)
                ivc = float(c["vol"]) if c.get("vol") is not None and not pd.isna(c["vol"]) else np.nan
                rvf = options_data.forward_realized_vol(panel[s], d0, d1)
                if not np.isnan(ivc) and rvf is not None:
                    impl_v[s], real_v[s] = ivc, rvf
            missing = [s for s in names if s not in real_names]
            optionable = options_data.optionable_underlyings(missing) if missing else set()
            unhedged = [s for s in missing if s not in optionable]
            for s in unhedged:                                               # can't be hedged live
                strike_ret[s] = 1e9                                          # no cap → keeps full eq_ret
                prem_yield[s] = 0.0                                          # no premium collected

        cc_ret = options.covered_call_return(eq_ret, strike_ret, prem_yield)
        assigned = eq_ret > strike_ret

        # Optional cash-secured-put sleeve (the put-wheel). Mirror the call leg: a put_delta OTM
        # put per held name, priced by BS at the same IV (or real DoltHub put mids/strikes), with
        # the short-put payoff. Kept as its own columns so the wheel is blended within the
        # leverage cap downstream — never folded into equity_net / cc_net.
        put_cols: dict = {}
        if with_puts:
            pdelta = put_delta if put_delta is not None else delta
            spot_p = pd.Series(S0, index=names)
            Kp = options.strike_for_put_delta(S0, T, iv.to_numpy(), pdelta)
            put_strike_ret = pd.Series(Kp / S0 - 1.0, index=names)
            put_prem = pd.Series(options.bs_put_price(S0, Kp, T, iv.to_numpy()) / S0, index=names) * (1.0 - slippage)
            p_impl, p_real, p_real_names = {}, {}, set()
            if premium_source == "dolthub":
                try:
                    pchains = options_data.fetch_puts(list(names), d0.date(), dte_min=dte_min, dte_max=dte_max)
                except Exception as exc:  # noqa: BLE001 — degrade to modeled puts this month
                    log.warning("dolthub put fetch failed %s; modeled puts this month: %s", d0.date(), exc)
                    pchains = {}
                for s in names:
                    pc = options_data.select_put(pchains.get(s, []), d0.date(), target_delta=pdelta,
                                                 dte_min=dte_min, dte_max=dte_max)
                    py = options_data.premium_yield(pc, float(spot_p[s]), slippage=slippage) if pc else None
                    if py is None:
                        continue
                    put_prem[s] = py                                          # real put mid premium
                    put_strike_ret[s] = float(pc["strike"]) / float(spot_p[s]) - 1.0   # real strike
                    p_real_names.add(s)
                    ivc = float(pc["vol"]) if pc.get("vol") is not None and not pd.isna(pc["vol"]) else np.nan
                    rvf = options_data.forward_realized_vol(panel[s], d0, d1)
                    if not np.isnan(ivc) and rvf is not None:
                        p_impl[s], p_real[s] = ivc, rvf
            put_ret = options.cash_secured_put_return(eq_ret, put_strike_ret, put_prem)
            put_cols = {
                "put_net": float((w * put_ret).sum()),
                "put_premium_income": float((w * put_prem).sum()),
                "put_assignment_rate": float((eq_ret < put_strike_ret).mean()),
            }
            if premium_source == "dolthub":
                put_cols["put_real_coverage"] = len(p_real_names) / max(len(names), 1)
                vn = [s for s in names if s in p_impl]
                if vn:
                    ww = w.reindex(vn); ww = ww / ww.sum()
                    iv_s, rv_s = pd.Series(p_impl).reindex(vn), pd.Series(p_real).reindex(vn)
                    put_cols["put_implied_vol"] = float((ww * iv_s).sum())
                    put_cols["put_realized_vol_fwd"] = float((ww * rv_s).sum())
                    put_cols["put_vrp_vol"] = put_cols["put_implied_vol"] - put_cols["put_realized_vol_fwd"]
                    put_cols["put_vrp_var"] = float((ww * (iv_s ** 2 - rv_s ** 2)).sum())
                else:
                    put_cols["put_implied_vol"] = put_cols["put_realized_vol_fwd"] = \
                        put_cols["put_vrp_vol"] = put_cols["put_vrp_var"] = np.nan

        cost = eq.transaction_cost(prev_w, w, adv, **cost_kw)
        row = {
            "date": d1.date(), "n_names": len(names),
            "equity_net": float((w * eq_ret).sum()) - cost,
            "cc_net": float((w * cc_ret).sum()) - cost,
            "premium_income": float((w * prem_yield).sum()),
            "upside_given_up": float((w * (eq_ret - strike_ret).clip(lower=0)).sum()),
            "assignment_rate": float(assigned.mean()),
            "turnover": eq.turnover(prev_w, w),
        }
        if premium_source == "dolthub":
            n = max(len(names), 1)
            row["real_coverage"] = len(real_names) / n           # priced from real chains
            row["n_unhedged"] = len(unhedged)                    # not optionable → no call written
            row["unhedged_weight"] = float(w.reindex(unhedged).sum()) if unhedged else 0.0
            vn = [s for s in names if s in impl_v]               # names with both implied & realized
            if vn:
                ww = w.reindex(vn); ww = ww / ww.sum()
                iv_s, rv_s = pd.Series(impl_v).reindex(vn), pd.Series(real_v).reindex(vn)
                row["implied_vol"] = float((ww * iv_s).sum())
                row["realized_vol_fwd"] = float((ww * rv_s).sum())
                row["vrp_vol"] = row["implied_vol"] - row["realized_vol_fwd"]
                row["vrp_var"] = float((ww * (iv_s ** 2 - rv_s ** 2)).sum())
            else:
                row["implied_vol"] = row["realized_vol_fwd"] = row["vrp_vol"] = row["vrp_var"] = np.nan
        row.update(put_cols)
        rows.append(row)
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

    # Real premiums + VARIANCE RISK PREMIUM (DoltHub, D33): does the modeled premium hold up
    # against real prices, and how much VRP did we actually harvest (implied vs realized vol)?
    real = results_by_mode.get("dolthub_real")
    if real is not None and "vrp_var" in real.columns:
        cov = float(real["real_coverage"].mean()) if "real_coverage" in real else float("nan")
        real_prem = real["premium_income"].mean() * 12
        mkt_prem = results_by_mode.get("vix_scaled", real)["premium_income"].mean() * 12
        uw = float(real["unhedged_weight"].mean()) if "unhedged_weight" in real else 0.0
        print(f"\n  Real premiums + variance risk premium (DoltHub real chains, "
              f"{cov*100:.0f}% of name-months covered):")
        print(f"    hedge mix: {cov*100:.0f}% real chains + BS-imputed (optionable, data gap), "
              f"{uw*100:.1f}% of NAV left UNHEDGED (non-optionable on Alpaca, e.g. DDS/UI/WTM)")
        print(f"    premium/yr   real {real_prem*100:.1f}%   vs   modeled @ market-IV {mkt_prem*100:.1f}%"
              f"   ({'model OPTIMISTIC' if mkt_prem > real_prem else 'model conservative'})")
        v = real.dropna(subset=["implied_vol", "realized_vol_fwd"])
        if not v.empty:
            imp, rea = float(v["implied_vol"].mean()), float(v["realized_vol_fwd"].mean())
            vrp_var, ratio = float(v["vrp_var"].mean()), (imp / rea if rea > 0 else float("nan"))
            hit = float((v["vrp_vol"] > 0).mean())
            out["vrp"] = {"implied_vol": imp, "realized_vol": rea, "vrp_vol": imp - rea,
                          "vrp_var": vrp_var, "iv_rv_ratio": ratio, "hit_rate": hit, "coverage": cov}
            print(f"    sold @ implied vol {imp*100:.1f}%   names realized {rea*100:.1f}%   "
                  f"→  VRP {(imp - rea) * 100:+.1f} vol pts  (IV/RV {ratio:.2f}; IV>RV {hit*100:.0f}% of months)")
            print(f"    variance risk premium  IV²−RV² = {vrp_var:+.4f}   "
                  f"({'options were RICH — selling them was paid for risk' if vrp_var > 0 else 'options were CHEAP — no edge'})")

    # Verdict (DECISIONS D27, refined D30): vol / drawdown / premium improve
    # UNCONDITIONALLY across every IV assumption (exact, data-driven). Sharpe AND the
    # assignment rate both depend on the IV assumption — a lower IV writes tighter
    # strikes, which lifts assignment — so both are judged at the realistic market-
    # implied 'vix_scaled' case (the conservative realized floor is reported alongside
    # for range, not used as a pass/fail bar: its tight strikes overstate assignment).
    floor = out["modes"].get("realized")
    market = out["modes"].get("vix_scaled", out["modes"][list(out["modes"])[-1]])
    # Gate judges the MODELED sensitivity (realized / vix_scaled); dolthub_real is informational.
    model_modes = [m for k, m in out["modes"].items() if k != "dolthub_real"]
    vol_ok = all(m["ann_vol"] < eq_metrics["ann_vol"] for m in model_modes)
    dd_ok = all(m["max_drawdown"] >= eq_metrics["max_drawdown"] - 1e-9 for m in model_modes)
    prem_ok = all(m["premium_yr"] > 0 for m in model_modes)
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


def wheel_summary(df: pd.DataFrame, eq_metrics: dict, *, budget: float, delta: float) -> dict:
    """Print the cash-secured-put sleeve + blended-wheel metrics + the put-skew VRP / left tail.

    ``budget`` is the share of gross given to puts (they displace equity within the leverage cap),
    so the blended wheel = ``(1−budget)·cc_net + budget·put_net``. Reported at 1× (the backtest
    basis); leverage scales the sleeve's drawdown ~linearly, and the put sleeve is long-delta.
    """
    if "put_net" not in df.columns:
        return {}
    s = df.set_index("date")
    put_m = eq.portfolio_metrics(s["put_net"])
    cc_m = eq.portfolio_metrics(s["cc_net"])
    wheel = (1.0 - budget) * s["cc_net"] + budget * s["put_net"]
    wh_m = eq.portfolio_metrics(wheel)
    print(f"\n  Cash-secured-put WHEEL ({delta:.2f}Δ puts · {budget*100:.0f}% of gross to puts · 1× backtest):")
    print(f"    {'sleeve':18}{'ann':>9}{'vol':>8}{'Sharpe':>8}{'maxDD':>8}")
    for label, mm in (("equity-only", eq_metrics), ("covered-call", cc_m),
                      ("put sleeve", put_m), ("WHEEL (cc+puts)", wh_m)):
        print(f"    {label:18}{mm['ann_return']*100:>8.1f}%{mm['ann_vol']*100:>7.1f}%"
              f"{mm['sharpe']:>8.2f}{mm['max_drawdown']*100:>7.1f}%")
    print(f"    put premium/yr {s['put_premium_income'].mean()*12*100:.1f}%   "
          f"put assignment {s['put_assignment_rate'].mean()*100:.0f}%")
    out = {"put_sleeve": put_m, "wheel": wh_m, "budget": budget, "delta": delta}
    v = df.dropna(subset=["put_implied_vol", "put_realized_vol_fwd"]) if "put_implied_vol" in df.columns else df.iloc[0:0]
    if not v.empty:
        imp, rea = float(v["put_implied_vol"].mean()), float(v["put_realized_vol_fwd"].mean())
        vrp_var = float(v["put_vrp_var"].mean())
        cov = float(df["put_real_coverage"].mean()) if "put_real_coverage" in df.columns else float("nan")
        hit = float((v["put_vrp_vol"] > 0).mean())
        out["vrp"] = {"implied_vol": imp, "realized_vol": rea, "vrp_var": vrp_var, "coverage": cov}
        print(f"    put VRP: sold IV {imp*100:.1f}% vs realized {rea*100:.1f}% = {(imp-rea)*100:+.1f} vol pts "
              f"(IV>RV {hit*100:.0f}% of months; real coverage {cov*100:.0f}%)")
        print(f"    put variance term IV²−RV² = {vrp_var:+.4f}  "
              f"({'left tail WAS paid for' if vrp_var > 0 else 'left tail NOT fully paid — crash-dominated'})")
    print("    ⚠ 1× backtest — at 2× leverage the put sleeve is long-delta, so its drawdown ~doubles.")
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description="Covered-call overlay backtest (Phase 2b)")
    ap.add_argument("--start", type=lambda s: date.fromisoformat(s), default=date(2021, 7, 1))
    ap.add_argument("--end", type=lambda s: date.fromisoformat(s), default=date.today())
    ap.add_argument("--validate-bxm", action="store_true", help="cross-check the engine vs ^BXM")
    ap.add_argument("--no-real-premium", action="store_true",
                    help="skip the DoltHub real-premium run (model-only; no network)")
    ap.add_argument("--with-puts", action="store_true",
                    help="also run the optional cash-secured-put wheel sleeve (real DoltHub put premiums)")
    args = ap.parse_args()
    for n in ("engine.factors", "engine.covariance", "scripts.backtest"):
        logging.getLogger(n).setLevel(logging.WARNING)
    settings = load_settings()
    pcfg = getattr(settings, "puts", None)
    pdelta = float(getattr(pcfg, "target_delta", 0.30))
    pbudget = float(getattr(pcfg, "budget_pct", 0.25))

    results = {mode: run_overlay(args.start, args.end, settings=settings, iv_mode=mode)
               for mode in ("realized", "vix_scaled")}
    if not args.no_real_premium:                 # real DoltHub premiums + the VRP analysis
        print("  pulling real historical chains from DoltHub (cached after first run)…")
        results["dolthub_real"] = run_overlay(args.start, args.end, settings=settings,
                                              iv_mode="vix_scaled", premium_source="dolthub",
                                              with_puts=args.with_puts, put_delta=pdelta)
    out = summarize(results)
    if args.with_puts and "dolthub_real" in results:
        wheel_summary(results["dolthub_real"], out["equity"], budget=pbudget, delta=pdelta)

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
