"""Dashboard data layer — Postgres-only reads for the live dashboard (Phase 5.1).

Pure read functions over the operational store the engine already populates (the 60s
monitor keeps ``snapshots`` fresh, so the dashboard never touches Alpaca — DECISIONS-aligned
Postgres-only design). Each ``api_*`` returns plain JSON-able dicts/lists; ``dashboard/app.py``
wraps them in FastAPI routes. Tested directly against in-memory sqlite — no HTTP layer / httpx.
"""

from __future__ import annotations

import math
import re

from sqlalchemy import desc, func, select

from engine import db

_TRADING_DAYS = 252.0

# OCC option symbols end in YYMMDD + C/P + an 8-digit strike (e.g. AAPL260821C00215000).
# Used to keep the leverage gauge and position count equity-only once the overlay writes
# options into the same snapshot (a short call carries negative market value).
_OCC_SUFFIX = re.compile(r"\d{6}[CP]\d{8}$")


def _is_option(symbol: str) -> bool:
    return bool(_OCC_SUFFIX.search(str(symbol)))


def _latest_snapshot(conn) -> dict | None:
    row = conn.execute(select(db.snapshots).order_by(desc(db.snapshots.c.ts)).limit(1)).mappings().first()
    return dict(row) if row else None


def _latest_rebalance(conn) -> dict | None:
    row = conn.execute(select(db.rebalance_log).order_by(desc(db.rebalance_log.c.ts)).limit(1)).mappings().first()
    return dict(row) if row else None


def api_state(db_engine) -> dict:
    """Current portfolio state: NAV/cash/drift, leverage, positions vs target, risk gate, P&L, premium.

    ``leverage`` = Σ **equity** position weights = equity gross / account equity (each weight is
    the position's market_value / NAV; options are excluded so a written short call's negative
    market value can't deflate the gauge). ``gross_exposure`` = NAV × leverage. ``day_pnl`` is the
    true intraday change vs Alpaca's prior-trading-day close equity (``snapshots.last_equity``),
    not the change since the last 60-second snapshot. Each position row carries its derived
    ``market_value`` (= weight × NAV).
    """
    with db_engine.connect() as conn:
        snap = _latest_snapshot(conn)
        reb = _latest_rebalance(conn)
        prev_nav = conn.execute(
            select(db.snapshots.c.nav).order_by(desc(db.snapshots.c.ts)).offset(1).limit(1)).scalar()
        premium = conn.execute(
            select(func.coalesce(func.sum(db.options_lifecycle.c.premium), 0.0))).scalar()

    premium = float(premium or 0.0)
    if snap is None:
        return {"nav": None, "cash": None, "drift": None, "ts": None, "positions": [],
                "risk_gate_passed": None, "risk_gate_reason": None, "day_pnl": None,
                "day_pnl_pct": None, "premium_collected": premium, "leverage": None,
                "gross_exposure": None, "n_positions": 0}

    nav = snap.get("nav")
    weights = snap.get("weights") or {}
    positions = snap.get("positions") or {}
    targets = (reb or {}).get("target_weights") or {}
    names = sorted(set(weights) | set(targets) | set(positions))
    rows = [{"symbol": s, "qty": positions.get(s), "weight": weights.get(s),
             "target_weight": targets.get(s),
             "market_value": (weights[s] * nav) if (s in weights and nav is not None) else None}
            for s in names]
    # Equity-only leverage / position count (exclude written options from the gauge).
    leverage = sum(float(v) for s, v in weights.items() if not _is_option(s)) if weights else 0.0
    gross = (nav * leverage) if nav is not None else None
    # True day P&L: current equity vs Alpaca's prior-close equity. Fall back to the previous
    # snapshot's NAV only if last_equity is absent (e.g. pre-migration snapshots).
    basis = snap.get("last_equity")
    if basis is None:
        basis = prev_nav
    day_pnl = (nav - basis) if (nav is not None and basis is not None) else None
    day_pnl_pct = (day_pnl / basis) if (day_pnl is not None and basis) else None
    return {"nav": nav, "cash": snap.get("cash"), "drift": snap.get("drift"),
            "ts": str(snap.get("ts")), "positions": rows,
            "risk_gate_passed": (reb or {}).get("risk_gate_passed"),
            "risk_gate_reason": (reb or {}).get("risk_gate_reason"),
            "day_pnl": day_pnl, "day_pnl_pct": day_pnl_pct, "premium_collected": premium,
            "leverage": leverage, "gross_exposure": gross,
            "n_positions": sum(1 for s, q in positions.items() if q and not _is_option(s))}


def api_nav_history(db_engine, limit: int = 120) -> list[dict]:
    """Recent NAV (and cash) snapshots, oldest-first, for the live equity sparkline."""
    with db_engine.connect() as conn:
        rows = conn.execute(
            select(db.snapshots.c.ts, db.snapshots.c.nav, db.snapshots.c.cash)
            .order_by(desc(db.snapshots.c.ts)).limit(limit)).all()
    return [{"ts": str(ts), "nav": nav, "cash": cash} for ts, nav, cash in reversed(rows)]


def api_orders(db_engine, limit: int = 50) -> list[dict]:
    """Most recent orders (descending)."""
    with db_engine.connect() as conn:
        rows = conn.execute(
            select(db.orders).order_by(desc(db.orders.c.created_at)).limit(limit)).mappings().all()
    return [{"symbol": r["symbol"], "side": r["side"], "qty": r["qty"], "type": r["order_type"],
             "status": r["status"], "filled_qty": r["filled_qty"],
             "filled_avg_price": r["filled_avg_price"],
             "submitted_at": str(r["submitted_at"]) if r["submitted_at"] else None} for r in rows]


def api_calls(db_engine) -> list[dict]:
    """Currently-open covered calls, derived from the options_lifecycle event log.

    Net contracts per option symbol = writes − closes; a positive net is still open.
    """
    with db_engine.connect() as conn:
        rows = conn.execute(
            select(db.options_lifecycle).order_by(db.options_lifecycle.c.ts)).mappings().all()
    net: dict[str, dict] = {}
    for r in rows:
        sym = r["option_symbol"]
        e = net.setdefault(sym, {"option_symbol": sym, "underlying": r["underlying"],
                                 "strike": r["strike"], "expiration": None, "delta": r["delta"],
                                 "contracts": 0, "premium": 0.0})
        e["contracts"] += (r["contracts"] or 0) * (1 if r["event_type"] == "write" else -1)
        e["premium"] += r["premium"] or 0.0
        if r["event_type"] == "write":                       # keep the written contract's terms
            e.update(strike=r["strike"], delta=r["delta"],
                     expiration=str(r["expiration"]) if r["expiration"] else None)
    return [e for e in net.values() if e["contracts"] > 0]


def api_factors(db_engine) -> list[dict]:
    """Latest factor scores for currently-held names."""
    with db_engine.connect() as conn:
        snap = _latest_snapshot(conn)
        held = set((snap or {}).get("positions") or {})
        if not held:
            return []
        latest_date = conn.execute(select(func.max(db.factor_scores.c.date))).scalar()
        if latest_date is None:
            return []
        rows = conn.execute(
            select(db.factor_scores).where(db.factor_scores.c.date == latest_date)).mappings().all()
    return [{"symbol": r["symbol"], "composite": r["composite_score"], "quality": r["quality_score"],
             "value": r["value_score"], "momentum": r["momentum_score"], "lowvol": r["lowvol_score"]}
            for r in rows if r["symbol"] in held]


def api_alerts(db_engine, limit: int = 50) -> list[dict]:
    """Most recent alerts (descending)."""
    with db_engine.connect() as conn:
        rows = conn.execute(
            select(db.alerts).order_by(desc(db.alerts.c.ts)).limit(limit)).mappings().all()
    return [{"ts": str(r["ts"]), "type": r["alert_type"], "message": r["message"],
             "delivered": r["delivered"]} for r in rows]


# ====================================================================== #
# Live track record — realized paper performance since inception (Phase 5.6)
# ====================================================================== #
def series_stats(values: list[float]) -> dict:
    """Performance stats from a daily value series. Pure; reused for strategy + benchmarks.

    Annualized figures use 252 trading days and ddof=1 vol. With < 2 points everything is
    ``None`` (insufficient history); annualized numbers are noisy until ~10+ days, which the
    caller flags via ``mature`` rather than hiding here.
    """
    n = len(values)
    base = {"total_return": None, "ann_return": None, "ann_vol": None, "sharpe": None,
            "max_drawdown": None, "n": n}
    if n < 2 or not values[0]:
        return base
    rets = [values[i] / values[i - 1] - 1 for i in range(1, n) if values[i - 1]]
    total = values[-1] / values[0] - 1
    ann_ret = (1 + total) ** (_TRADING_DAYS / len(rets)) - 1 if rets else None
    if len(rets) > 1:
        mean = sum(rets) / len(rets)
        sd = math.sqrt(sum((r - mean) ** 2 for r in rets) / (len(rets) - 1))
    else:
        sd = 0.0
    ann_vol = sd * math.sqrt(_TRADING_DAYS)
    sharpe = (ann_ret / ann_vol) if (ann_vol and ann_ret is not None) else None
    peak = values[0]
    mdd = 0.0
    for v in values:
        peak = max(peak, v)
        mdd = min(mdd, v / peak - 1) if peak else mdd
    return {"total_return": total, "ann_return": ann_ret, "ann_vol": ann_vol,
            "sharpe": sharpe, "max_drawdown": mdd, "n": n}


def _daily_nav(rows) -> tuple[list[str], list[float]]:
    """(ts, nav) rows (asc) → (ISO dates, last-NAV-per-day) — the daily equity curve."""
    by_day: dict = {}
    for ts, nav in rows:
        if nav is None:
            continue
        d = ts.date() if hasattr(ts, "date") else str(ts)[:10]
        by_day[d] = float(nav)
    days = sorted(by_day)
    return [d.isoformat() if hasattr(d, "isoformat") else str(d) for d in days], \
           [by_day[d] for d in days]


def api_track_record(db_engine) -> dict:
    """Realized paper performance since inception, from the ``snapshots`` equity curve.

    Returns the daily NAV series + normalized curve + stats (return/vol/Sharpe/drawdown) and
    lifetime premium collected. ``available`` is False until the monitor has written snapshots;
    ``mature`` gates the annualized numbers (noisy with < ~10 days). Benchmark comparison is
    layered on in the route (needs Alpaca), so this stays Postgres-only + unit-testable.
    """
    with db_engine.connect() as conn:
        rows = conn.execute(
            select(db.snapshots.c.ts, db.snapshots.c.nav).order_by(db.snapshots.c.ts)).all()
        premium = conn.execute(
            select(func.coalesce(func.sum(db.options_lifecycle.c.premium), 0.0))).scalar()
    rows = [(ts, nav) for ts, nav in rows if nav is not None]
    if not rows:
        return {"available": False, "days": 0, "dates": [], "nav": [], "norm": [],
                "premium_collected": float(premium or 0.0)}
    dates, navs = _daily_nav(rows)
    stats = series_stats(navs)
    norm = [v / navs[0] for v in navs] if navs[0] else navs
    return {"available": True, "inception": dates[0], "days": len(dates),
            "mature": len(dates) >= 10, "nav0": navs[0], "nav_now": navs[-1],
            "premium_collected": float(premium or 0.0), "dates": dates, "nav": navs,
            "norm": norm, **stats}


def api_slippage(db_engine) -> dict:
    """Execution quality from filled orders: realized fill vs the intended (limit/mid) price.

    Per filled limit order, ``slippage = (fill − intended)`` signed so **positive = adverse**
    (paid more on a buy / received less on a sell), in bps of the intended price and in dollars.
    Market orders carry no intended price (limit None) and are excluded. Aggregates are
    notional-weighted. The dollar total is the realized execution cost vs decision-time mid.
    """
    with db_engine.connect() as conn:
        rows = conn.execute(
            select(db.orders).where(db.orders.c.status == "filled")
            .order_by(desc(db.orders.c.created_at))).mappings().all()
    fills: list[dict] = []
    tot_usd = tot_notional = wbps = 0.0
    for r in rows:
        intended, filled, fq = r["limit_price"], r["filled_avg_price"], (r["filled_qty"] or 0)
        if not intended or not filled or not fq:
            continue
        adverse = (filled - intended) if str(r["side"]).lower() == "buy" else (intended - filled)
        bps = adverse / intended * 1e4
        usd, notional = adverse * fq, filled * fq
        tot_usd += usd
        tot_notional += notional
        wbps += bps * notional
        fills.append({"symbol": r["symbol"], "side": str(r["side"]).lower(), "qty": fq,
                      "intended": round(float(intended), 2), "filled": round(float(filled), 2),
                      "slippage_bps": round(bps, 1), "slippage_usd": round(usd, 2),
                      "filled_at": str(r["filled_at"]) if r["filled_at"] else None})
    avg_bps = (wbps / tot_notional) if tot_notional else None
    return {"n_fills": len(fills),
            "avg_slippage_bps": round(avg_bps, 1) if avg_bps is not None else None,
            "total_slippage_usd": round(tot_usd, 2), "fills": fills[:30]}
