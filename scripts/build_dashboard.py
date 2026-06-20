"""Compute the backtest-dashboard dataset and render the self-contained HTML report.

Instruments the full walk-forward monthly loop (score → covariance → optimize) to
capture aggregate performance *and* the detail an analyst wants: per-month holdings
and sector mix (for the time-scrubbing pie), company names + ADR classification,
individual trades and costs, name-level return contribution, the covered-call overlay
growth curve, rolling vol, best/worst months, and win/loss streaks.

Writes `data/backtest/dashboard_data.json` and injects it into
`reports/dashboard_template.html` → `reports/backtest_dashboard.html` (+ a body-only
`_dashboard_widget.html` for inline preview).

    python scripts/build_dashboard.py
"""

from __future__ import annotations

import glob
import json
import logging
import re
import sys
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from engine import covariance, factors, optimize, sectors  # noqa: E402
from engine.config import load_settings  # noqa: E402
from engine.sectors import SECTORS as SECTOR_ORDER  # noqa: E402
from scripts import backtest as eq  # noqa: E402
from scripts import backtest_covered_calls as cc  # noqa: E402

DATA_OUT = Path("data/backtest/dashboard_data.json")
TEMPLATE = Path("reports/dashboard_template.html")
HTML_OUT = Path("reports/backtest_dashboard.html")
FRAG_OUT = Path("reports/_dashboard_widget.html")
START, END = date(2021, 7, 1), date(2026, 5, 1)
TRADE_W_MIN = 0.005


def _r(x, n=5):
    if x is None or (isinstance(x, float) and not np.isfinite(x)):
        return None
    return round(float(x), n)


def _ser(s, n=5):
    return [_r(v, n) for v in s]


def _clean_name(raw: str) -> str:
    n = re.split(r" American Depositary| Class [A-Z] (?:Common|Ordinary)| Common Stock|"
                 r" Ordinary Shares| Sponsored ADR| Depositary Shares", raw)[0]
    return n.strip().rstrip(",").strip()[:46] or raw[:46]


def _is_adr(raw: str) -> bool:
    r = raw.lower()
    return "american depositary" in r or "sponsored adr" in r or r.endswith(" ads")


def _foreign_set() -> set[str]:
    """Symbols classified foreign/ADR: their fundamentals fell back to yfinance because
    they file 20-F/IFRS rather than US-GAAP (so EDGAR's companyfacts lacks them)."""
    out: set[str] = set()
    for f in glob.glob(str(Path(eq.FUNDAMENTALS_DIR) / "*.parquet")):
        df = pd.read_parquet(f, columns=["source"])
        out.update(str(s) for s in df.index[df["source"] == "yfinance"])
    return out


def _load_names() -> dict[str, str]:
    try:
        from engine import config
        config.load_env("paper")
        assets = config.get_alpaca_client().list_assets()
        return {a["symbol"]: a["name"] for a in assets if a.get("name")}
    except Exception as exc:  # noqa: BLE001
        print(f"(name lookup unavailable: {exc}) — falling back to tickers")
        return {}


def _streaks(vals) -> dict:
    win = loss = cw = cl = 0
    for v in vals:
        if v > 0:
            cw += 1; cl = 0; win = max(win, cw)
        elif v < 0:
            cl += 1; cw = 0; loss = max(loss, cl)
        else:
            cw = cl = 0
    return {"win": win, "loss": loss}


def collect(settings) -> dict:
    for name in ("engine.factors", "engine.covariance"):
        logging.getLogger(name).setLevel(logging.ERROR)
    nav = settings.portfolio.nav
    panel = factors.load_close_panel(eq.PRICES_DIR, end=END, lookback=10**9)
    allf, eligible = factors.load_scored_fundamentals(eq.FUNDAMENTALS_DIR, settings)
    ff5 = covariance.load_ff5_daily()
    sector_map = sectors.load_sector_map()["sector"]
    adv = eq._latest_adv(panel.index[-1])
    win = settings.covariance.estimation_window_days
    cost_kw = dict(large_thr=settings.execution.large_cap_adv_threshold,
                   mid_thr=settings.execution.mid_cap_adv_threshold,
                   bps_large=settings.execution.cost_bps_large,
                   bps_mid=settings.execution.cost_bps_mid,
                   bps_small=settings.execution.cost_bps_small)
    clip, bench_sym = settings.backtest.return_clip, settings.backtest.benchmark

    burn = settings.factors.momentum_long_window + 1
    rebal = [d for d in eq.month_start_dates(panel.index)
             if START <= d.date() <= END and panel.index.get_loc(d) >= burn]

    prev_w = pd.Series(dtype=float)
    labels, net_l, cost_l, turn_l, bench_l = [], [], [], [], []
    nnames_l, cash_l, buys_l, sells_l = [], [], [], []
    sector_series = {s: [] for s in SECTOR_ORDER}
    holdings_by_month: list[list[str]] = []
    contrib: dict[str, float] = {}
    blotter: list[dict] = []
    tickers: set[str] = set()

    for d0, d1 in zip(rebal[:-1], rebal[1:]):
        sc = factors.score_date(d0.date(), settings=settings, price_panel=panel,
                                all_fundamentals=allf, eligible_symbols=eligible).set_index("symbol")
        comp = sc["composite_score"].dropna()
        if comp.empty:
            continue
        top = comp.sort_values(ascending=False).head(settings.optimizer.preselect_top_k).index
        sigma = covariance.covariance_from_prices(panel[top], ff5, as_of=d0, window=win,
                                                  min_obs=settings.covariance.min_regression_obs)
        w = optimize.optimize_portfolio(comp, sigma, sector_map, settings=settings, prev_weights=prev_w).weights
        if w.empty:
            continue
        names = w.sort_values(ascending=False).index
        fwd = (panel.loc[d1, names] / panel.loc[d0, names] - 1.0).clip(-clip, clip)
        label = d1.strftime("%Y-%m")
        secs = sector_map.reindex(names).fillna("Unknown")
        sw = w.groupby(secs).sum()

        idx = prev_w.index.union(names)
        dw = w.reindex(idx, fill_value=0.0) - prev_w.reindex(idx, fill_value=0.0)
        sig = dw[dw.abs() > TRADE_W_MIN]
        for sym, d in sig.items():
            blotter.append({"m": label, "sym": str(sym), "side": "buy" if d > 0 else "sell",
                            "usd": int(round(d * nav))})
            tickers.add(str(sym))
        for sym in names:
            contrib[str(sym)] = contrib.get(str(sym), 0.0) + float(w[sym] * fwd.get(sym, 0.0))

        labels.append(label)
        net_l.append(float((w * fwd).sum()) - eq.transaction_cost(prev_w, w, adv, **cost_kw))
        cost_l.append(eq.transaction_cost(prev_w, w, adv, **cost_kw))
        turn_l.append(eq.turnover(prev_w, w))
        bench_l.append(float(panel.loc[d1, bench_sym] / panel.loc[d0, bench_sym] - 1.0))
        nnames_l.append(len(names)); cash_l.append(1.0 - float(w.sum()))
        buys_l.append(int((sig > 0).sum())); sells_l.append(int((sig < 0).sum()))
        for s in SECTOR_ORDER:
            sector_series[s].append(round(float(sw.get(s, 0.0)), 4))
        hm = [str(s) for s in names]
        holdings_by_month.append(hm); tickers.update(hm)
        prev_w = eq.drift_weights(w, fwd)

    net = pd.Series(net_l, index=labels)
    spy = pd.Series(bench_l, index=labels)
    cum, scum = (1 + net).cumprod(), (1 + spy).cumprod()
    dd = cum / cum.cummax() - 1.0
    roll = net.rolling(12)
    m, mb = eq.portfolio_metrics(net), eq.portfolio_metrics(spy)

    idx_dt = pd.to_datetime(labels)
    years = []
    for y in sorted({d.year for d in idx_dt}):
        pos = [i for i, d in enumerate(idx_dt) if d.year == y]
        years.append({"y": int(y), "strat": _r((1 + net.iloc[pos]).prod() - 1, 4),
                      "spy": _r((1 + spy.iloc[pos]).prod() - 1, 4)})
    csort = sorted(contrib.items(), key=lambda kv: kv[1], reverse=True)
    contributors = [{"sym": k, "v": _r(v, 4)} for k, v in csort[:8]]
    detractors = [{"sym": k, "v": _r(v, 4)} for k, v in csort[-6:] if v < 0][::-1]
    for x in contributors + detractors:
        tickers.add(x["sym"])
    counts, edges = np.histogram(net.values, bins=12)
    bw = sorted(zip(labels, net_l), key=lambda x: x[1])
    keep_sectors = [s for s in SECTOR_ORDER if any(v > 0.001 for v in sector_series[s])]

    return {
        "_tickers": sorted(tickers),
        "meta": {"start": labels[0], "end": labels[-1], "months": len(labels), "nav": nav},
        "labels": labels,
        "equity": {
            "cagr": _r(m["ann_return"], 4), "sharpe": _r(m["sharpe"], 3), "vol": _r(m["ann_vol"], 4),
            "maxdd": _r(m["max_drawdown"], 4), "calmar": _r(m["calmar"], 3),
            "total_return": _r(cum.iloc[-1] - 1, 4), "hit_rate": _r((net > 0).mean(), 3),
            "best": _r(net.max(), 4), "worst": _r(net.min(), 4),
            "streaks": _streaks(net_l),
            "best_months": [{"m": k, "r": _r(v, 4)} for k, v in bw[-5:][::-1]],
            "worst_months": [{"m": k, "r": _r(v, 4)} for k, v in bw[:5]],
            "cum": _ser(cum), "spy_cum": _ser(scum), "dd": _ser(dd),
            "roll_sharpe": _ser(roll.mean() / roll.std() * np.sqrt(12)),
            "roll_vol": _ser(net.rolling(12).std() * np.sqrt(12)),
            "net": _ser(net), "turnover": _ser(pd.Series(turn_l)),
        },
        "spy": {"cagr": _r(mb["ann_return"], 4), "sharpe": _r(mb["sharpe"], 3),
                "vol": _r(mb["ann_vol"], 4), "maxdd": _r(mb["max_drawdown"], 4),
                "total_return": _r(scum.iloc[-1] - 1, 4)},
        "years": years,
        "composition": {
            "sectors_order": keep_sectors,
            "sectors": {s: sector_series[s] for s in keep_sectors},
            "n_names": nnames_l, "cash": _ser(pd.Series(cash_l)),
            "holdings_by_month": holdings_by_month,
        },
        "activity": {
            "buys": buys_l, "sells": sells_l, "cost": _ser(pd.Series(cost_l)),
            "cum_cost": _ser(pd.Series(cost_l).cumsum()),
            "total_cost_usd": int(round(sum(cost_l) * nav)), "total_trades": len(blotter),
            "blotter": blotter[-30:][::-1],
        },
        "contributors": contributors, "detractors": detractors,
        "hist": {"counts": [int(c) for c in counts], "edges": [_r(e, 4) for e in edges]},
    }


def overlay_block(settings) -> dict:
    for name in ("scripts.backtest", "scripts.backtest_covered_calls"):
        logging.getLogger(name).setLevel(logging.ERROR)
    rows, cums, eq_sharpe, eq_cum = [], {}, None, None
    # IV-premium scenarios (mult of realized vol; None = market-implied via VIX).
    cases = [("Realized vol (floor)", 1.0, "realized"), ("IV 1.10× realized", 1.10, "iv110"),
             ("IV 1.20× realized", 1.20, "iv120"), ("VIX-scaled (market)", None, "market")]
    for label, mult, key in cases:
        res = (cc.run_overlay(START, END, settings=settings, iv_scale=mult) if mult is not None
               else cc.run_overlay(START, END, settings=settings, iv_mode="vix_scaled"))
        s = res.set_index("date")
        cm = eq.portfolio_metrics(s["cc_net"])
        cums[key] = _ser((1 + s["cc_net"]).cumprod())
        if eq_sharpe is None:
            eq_sharpe = eq.portfolio_metrics(s["equity_net"])["sharpe"]
            eq_cum = _ser((1 + s["equity_net"]).cumprod())
        rows.append({"label": label, "mult": mult, "sharpe": _r(cm["sharpe"], 3),
                     "ann": _r(cm["ann_return"], 4), "vol": _r(cm["ann_vol"], 4),
                     "maxdd": _r(cm["max_drawdown"], 4),
                     "premium": _r(res["premium_income"].mean() * 12, 4),
                     "assign": _r(res["assignment_rate"].mean(), 3)})
    # Delta optimization — sweep the call delta at BOTH the realized-vol floor and
    # market-implied IV, so the choice is robust to the premium assumption. The
    # assignment-rate cap (< 30%) is the practical ceiling on delta.
    delta_sweep = []
    for dlt in (0.15, 0.20, 0.25, 0.30, 0.35, 0.40):
        mk = cc.run_overlay(START, END, settings=settings, iv_mode="vix_scaled", target_delta=dlt)
        fl = cc.run_overlay(START, END, settings=settings, iv_scale=1.0, target_delta=dlt)
        cmk, cfl = eq.portfolio_metrics(mk.set_index("date")["cc_net"]), eq.portfolio_metrics(fl.set_index("date")["cc_net"])
        delta_sweep.append({"delta": dlt, "sharpe_market": _r(cmk["sharpe"], 3),
                            "sharpe_floor": _r(cfl["sharpe"], 3), "ann": _r(cmk["ann_return"], 4),
                            "vol": _r(cmk["ann_vol"], 4), "premium": _r(mk["premium_income"].mean() * 12, 4),
                            "assign": _r(mk["assignment_rate"].mean(), 3)})
    # "Best" = highest market Sharpe among deltas that keep assignment under 30%.
    feasible = [r for r in delta_sweep if r["assign"] < 0.30] or delta_sweep
    best = max(feasible, key=lambda r: r["sharpe_market"])
    s0, s1 = rows[0]["sharpe"], rows[1]["sharpe"]
    be = 1.0 + (eq_sharpe - s0) / (s1 - s0) * 0.10 if s1 != s0 else None
    return {"rows": rows, "breakeven_mult": _r(be, 3),
            "cum_market": cums["market"], "cums": cums, "eq_cum": eq_cum,
            "delta_sweep": delta_sweep, "best_delta": best["delta"],
            "current_delta": settings.covered_calls.target_delta}


def sleeves_block(settings) -> dict:
    res = eq.run_walkforward(START, END, settings=settings).set_index("date")
    return {k: _r(eq.portfolio_metrics(res[k])["ann_return"], 4)
            for k in ("quality", "value", "momentum", "lowvol")}


def main() -> None:
    settings = load_settings()
    data = collect(settings)
    raw_names = _load_names()
    tickers = data.pop("_tickers")
    sector_map = sectors.load_sector_map()["sector"]
    foreign = _foreign_set()
    data["names"] = {t: _clean_name(raw_names.get(t, t)) for t in tickers}
    data["adr"] = {t: (t in foreign or _is_adr(raw_names.get(t, ""))) for t in tickers}
    data["tsec"] = {t: str(sector_map.get(t, "Unknown")) for t in tickers}
    # ADR vs domestic exposure over time (each held name is ~5% of NAV).
    adr_exp = [round(0.05 * sum(1 for s in hm if data["adr"].get(s)), 4)
               for hm in data["composition"]["holdings_by_month"]]
    data["composition"]["adr_exposure"] = adr_exp
    data["sleeves"] = sleeves_block(settings)
    data["overlay"] = overlay_block(settings)

    DATA_OUT.parent.mkdir(parents=True, exist_ok=True)
    DATA_OUT.write_text(json.dumps(data))
    print(f"wrote {DATA_OUT} ({DATA_OUT.stat().st_size/1024:.1f} KB)")
    if TEMPLATE.exists():
        frag = TEMPLATE.read_text().replace("/*__DASHBOARD_DATA__*/null", json.dumps(data))
        FRAG_OUT.write_text(frag)
        full = ('<!DOCTYPE html>\n<html lang="en">\n<head><meta charset="utf-8">'
                '<meta name="viewport" content="width=device-width, initial-scale=1">'
                "<title>sharpe-engine — backtest dashboard</title></head>\n"
                '<body style="margin:0;padding:20px 0;background:#f4f3ef;">\n' + frag + "\n</body></html>")
        HTML_OUT.write_text(full)
        print(f"wrote {HTML_OUT} ({HTML_OUT.stat().st_size/1024:.1f} KB) and {FRAG_OUT}")
    else:
        print(f"(template {TEMPLATE} not found — JSON only)")


if __name__ == "__main__":
    main()
