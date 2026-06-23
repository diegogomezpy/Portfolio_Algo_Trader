"""Dashboard data layer — Postgres-only reads for the live dashboard (Phase 5.1).

Pure read functions over the operational store the engine already populates (the 60s
monitor keeps ``snapshots`` fresh, so the dashboard never touches Alpaca — DECISIONS-aligned
Postgres-only design). Each ``api_*`` returns plain JSON-able dicts/lists; ``dashboard/app.py``
wraps them in FastAPI routes. Tested directly against in-memory sqlite — no HTTP layer / httpx.
"""

from __future__ import annotations

from sqlalchemy import desc, func, select

from engine import db


def _latest_snapshot(conn) -> dict | None:
    row = conn.execute(select(db.snapshots).order_by(desc(db.snapshots.c.ts)).limit(1)).mappings().first()
    return dict(row) if row else None


def _latest_rebalance(conn) -> dict | None:
    row = conn.execute(select(db.rebalance_log).order_by(desc(db.rebalance_log.c.ts)).limit(1)).mappings().first()
    return dict(row) if row else None


def api_state(db_engine) -> dict:
    """Current portfolio state: NAV/cash/drift, leverage, positions vs target, risk gate, P&L, premium.

    ``leverage`` = Σ position weights = gross market value / equity (each weight is the
    position's market_value / NAV and NAV is the account equity, so the sum is gross/equity —
    honest, derived from the snapshot, no fabrication). ``gross_exposure`` = NAV × leverage.
    Each position row carries its derived ``market_value`` (= weight × NAV).
    """
    with db_engine.connect() as conn:
        snap = _latest_snapshot(conn)
        reb = _latest_rebalance(conn)
        navs = [r[0] for r in conn.execute(
            select(db.snapshots.c.nav).order_by(desc(db.snapshots.c.ts)).limit(2)).all()]
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
    leverage = sum(float(v) for v in weights.values()) if weights else 0.0
    gross = (nav * leverage) if nav is not None else None
    day_pnl = (navs[0] - navs[1]) if (len(navs) >= 2 and navs[0] is not None and navs[1] is not None) else None
    day_pnl_pct = (day_pnl / navs[1]) if (day_pnl is not None and navs[1]) else None
    return {"nav": nav, "cash": snap.get("cash"), "drift": snap.get("drift"),
            "ts": str(snap.get("ts")), "positions": rows,
            "risk_gate_passed": (reb or {}).get("risk_gate_passed"),
            "risk_gate_reason": (reb or {}).get("risk_gate_reason"),
            "day_pnl": day_pnl, "day_pnl_pct": day_pnl_pct, "premium_collected": premium,
            "leverage": leverage, "gross_exposure": gross,
            "n_positions": sum(1 for q in positions.values() if q)}


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
