"""Dashboard data layer — Postgres-only reads for the live dashboard (Phase 5.1).

Pure read functions over the operational store the engine already populates (the 60s
monitor keeps ``snapshots`` fresh, so the dashboard never touches Alpaca — DECISIONS-aligned
Postgres-only design). Each ``api_*`` returns plain JSON-able dicts/lists; ``dashboard/app.py``
wraps them in FastAPI routes. Tested directly against in-memory sqlite — no HTTP layer / httpx.
"""

from __future__ import annotations

import math
from datetime import date, datetime, timedelta, timezone

from sqlalchemy import desc, func, select

from engine import db, symbols
from engine.alerts import severity as alert_severity
from engine.execute import arrival_reference        # shared with the execution pricer (one truth)
from engine.monitor import PRIMARY_ACCOUNT          # tracked-sleeves: the engine-traded book's id

_TRADING_DAYS = 252.0

def _parse_occ(symbol: str) -> dict | None:
    """``{underlying, type, strike, expiration(ISO str)}`` via the canonical engine.symbols
    parser (audit: this regex used to be duplicated here)."""
    occ = symbols.parse_occ(symbol)
    if occ is None:
        return None
    return {**occ, "expiration": occ["expiration"].isoformat()}

# Engine-heartbeat thresholds (seconds). The monitor writes a snapshot every ~60s regardless
# of market hours, so a growing gap means the monitor/engine process is wedged — not a quiet
# market. < live: healthy; live..stale: degraded; beyond stale (or no data): down.
_HB_LIVE_S = 180
_HB_STALE_S = 600
# Per-name |weight − target| above this counts a name as "drifting" in the reconciliation tile.
_DRIFT_NAME_EPS = 0.005


_is_option = symbols.is_option        # canonical option test (engine.symbols)


def _latest_snapshot(conn, account: str = PRIMARY_ACCOUNT) -> dict | None:
    row = conn.execute(select(db.snapshots).where(db.snapshots.c.account == account)
                       .order_by(desc(db.snapshots.c.ts)).limit(1)).mappings().first()
    return dict(row) if row else None


def _latest_rebalance(conn) -> dict | None:
    row = conn.execute(select(db.rebalance_log).order_by(desc(db.rebalance_log.c.ts)).limit(1)).mappings().first()
    return dict(row) if row else None


def api_state(db_engine, account: str = PRIMARY_ACCOUNT) -> dict:
    """Current portfolio state: NAV/cash/drift, leverage, positions vs target, risk gate, P&L, premium.

    ``leverage`` = Σ **equity** position weights = equity gross / account equity (each weight is
    the position's market_value / NAV; options are excluded so a written short call's negative
    market value can't deflate the gauge). ``gross_exposure`` = NAV × leverage. ``day_pnl`` is the
    true intraday change vs Alpaca's prior-trading-day close equity (``snapshots.last_equity``),
    not the change since the last 60-second snapshot. Each position row carries its derived
    ``market_value`` (= weight × NAV).
    """
    with db_engine.connect() as conn:
        snap = _latest_snapshot(conn, account)
        reb = _latest_rebalance(conn)
        prev_nav = conn.execute(
            select(db.snapshots.c.nav).where(db.snapshots.c.account == account)
            .order_by(desc(db.snapshots.c.ts)).offset(1).limit(1)).scalar()
        # Premium is options_lifecycle (not account-tagged until per-account trading) — a real
        # figure only for the engine-traded book; other accounts show 0 until they trade.
        premium = conn.execute(
            select(func.coalesce(func.sum(db.options_lifecycle.c.premium), 0.0))
        ).scalar() if account == PRIMARY_ACCOUNT else 0.0

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


_LIVE_PX_BAND = 0.35   # reject a live tick > 35% off the 60s snapshot price as a bad/stale print


def apply_live_prices(state: dict, prices: dict, prev_close: dict | None = None) -> dict:
    """Overlay live streamed trade ``prices`` onto an :func:`api_state` dict for sub-second
    headline metrics (Phase 2 latency). Each equity position is marked to its live price and NAV
    to the **mark-to-market delta** from the 60s snapshot (``snap_price = market_value / qty``);
    day-P&L, gross exposure and weights are re-derived off the live NAV. ``prev_close`` (prior-day
    close per symbol) yields a live per-position ``day_pct``. Options / names with no live tick keep
    their snapshot value. Mutates and returns ``state``; a no-op with no prices.
    """
    nav = state.get("nav")
    positions = state.get("positions") or []
    prev_close = prev_close or {}
    if nav is None or not prices:
        return state
    delta = 0.0
    for row in positions:
        sym, qty, mv = row.get("symbol"), row.get("qty"), row.get("market_value")
        live = prices.get(sym)
        snap_px = (mv / qty) if (qty and mv is not None) else None
        # Bad-tick guard: ignore a live print that deviates > _LIVE_PX_BAND from the 60s snapshot
        # price (Alpaca truth) — a stray IEX print shouldn't 10× a position or spike NAV for a tick.
        if live and snap_px and abs(float(live) / snap_px - 1.0) > _LIVE_PX_BAND:
            live = None
        if live and qty and mv is not None:
            delta += qty * (float(live) - mv / qty)          # mark-to-market move vs the snapshot
            row["last_price"] = round(float(live), 4)
            row["market_value"] = round(qty * float(live), 2)
        pc = prev_close.get(sym)
        if live and pc:
            row["day_pct"] = float(live) / float(pc) - 1.0
    state["prices_live"] = True
    if not delta:
        return state
    new_nav = round(nav + delta, 2)
    if state.get("day_pnl") is not None:                     # basis = prior-close equity = nav − day_pnl
        basis = nav - state["day_pnl"]
        state["day_pnl"] = round(new_nav - basis, 2)
        state["day_pnl_pct"] = ((new_nav - basis) / basis) if basis else None
    if state.get("leverage") is not None:
        state["gross_exposure"] = round(new_nav * state["leverage"], 2)
    for row in positions:                                    # weights against the live NAV
        mv = row.get("market_value")
        if mv is not None and new_nav:
            row["weight"] = mv / new_nav
    state["nav"] = new_nav
    return state


def held_symbols(db_engine) -> list[str]:
    """Every equity symbol the dashboard might label: held positions, last target book,
    today's factor names, and covered-call underlyings. Option (OCC) symbols are excluded.

    Used to scope the instrument-reference lookup (name/sector) to the names actually on
    screen, rather than the whole universe.
    """
    with db_engine.connect() as conn:
        snap = _latest_snapshot(conn)
        reb = _latest_rebalance(conn)
        latest_fd = conn.execute(select(func.max(db.factor_scores.c.date))).scalar()
        fsyms = ([r[0] for r in conn.execute(
            select(db.factor_scores.c.symbol).where(db.factor_scores.c.date == latest_fd)).all()]
            if latest_fd is not None else [])
        unders = [r[0] for r in conn.execute(select(db.options_lifecycle.c.underlying)).all()]
    syms = set((snap or {}).get("positions") or {})
    syms |= set((reb or {}).get("target_weights") or {})
    syms |= set(fsyms)
    syms |= {u for u in unders if u}
    return sorted(s for s in syms if s and not _is_option(s))


def api_nav_history(db_engine, limit: int = 120, *, intraday_hours: int = 24,
                    account: str = PRIMARY_ACCOUNT) -> list[dict]:
    """NAV (and cash) curve, oldest-first: full 60s resolution for the trailing
    ``intraday_hours``, then **one snapshot per day** (each day's last) beyond that.

    The old query returned the raw last-``limit`` rows — at the monitor's 60s cadence,
    ``limit=1000`` is ≈17 hours, so the dashboard's 1W/1M/YTD/All range buttons could never
    show more than a day (audit B6). Daily-sampling the past keeps years of curve inside the
    same row budget while today stays live at full resolution. Each segment is capped at
    ``limit`` rows, so the payload is bounded at 2×``limit``.
    """
    cutoff = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=intraday_hours)
    cols = (db.snapshots.c.ts, db.snapshots.c.nav, db.snapshots.c.cash)
    acct = db.snapshots.c.account == account
    with db_engine.connect() as conn:
        recent = conn.execute(
            select(*cols).where(acct, db.snapshots.c.ts >= cutoff)
            .order_by(desc(db.snapshots.c.ts)).limit(limit)).all()
        # One row per calendar day before the cutoff: the day's final snapshot (its close).
        daily_last = (select(func.max(db.snapshots.c.ts))
                      .where(acct, db.snapshots.c.ts < cutoff)
                      .group_by(func.date(db.snapshots.c.ts))).scalar_subquery()
        older = conn.execute(
            select(*cols).where(acct, db.snapshots.c.ts.in_(daily_last))
            .order_by(desc(db.snapshots.c.ts)).limit(limit)).all()
    rows = list(reversed(older)) + list(reversed(recent))
    return [{"ts": str(ts), "nav": nav, "cash": cash} for ts, nav, cash in rows]


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
    """Currently-open covered calls, sourced from the **live snapshot's short-call positions**
    (Alpaca truth via the 60s monitor), enriched with strike/delta/premium from the
    options_lifecycle write log.

    Reading the actual position book — not the write log — is what guarantees the panel matches
    Alpaca: a write that never filled (or was cancelled) carries no position, so it never shows;
    a closed call disappears the moment the next snapshot lands. The lifecycle log is metadata
    only here (the contract terms + the premium collected when it was written). Contracts =
    ``|qty|``; strike/expiry fall back to the OCC symbol when no write row exists.
    """
    with db_engine.connect() as conn:
        snap = _latest_snapshot(conn)
        writes = conn.execute(
            select(db.options_lifecycle)
            .where(db.options_lifecycle.c.event_type == "write")
            .order_by(db.options_lifecycle.c.ts)).mappings().all()
    positions = (snap or {}).get("positions") or {}
    weights = (snap or {}).get("weights") or {}
    nav = (snap or {}).get("nav")
    # Latest write terms per option symbol (a later rewrite supersedes an earlier one).
    meta: dict[str, dict] = {}
    for r in writes:
        meta[r["option_symbol"]] = {
            "strike": r["strike"], "delta": r["delta"], "premium": r["premium"],
            "expiration": str(r["expiration"]) if r["expiration"] else None}
    out: list[dict] = []
    for sym, qty in positions.items():
        occ = _parse_occ(sym)
        if occ is None or occ["type"] != "call" or float(qty) >= 0:   # short calls only
            continue
        m = meta.get(sym, {})
        mv = (float(weights.get(sym, 0.0)) * nav) if nav is not None else None
        out.append({
            "option_symbol": sym, "underlying": occ["underlying"],
            "contracts": int(round(abs(float(qty)))),
            "strike": m.get("strike") if m.get("strike") is not None else occ["strike"],
            "expiration": m.get("expiration") or occ["expiration"],
            "delta": m.get("delta"), "premium": m.get("premium"),
            "market_value": round(mv, 2) if mv is not None else None})
    return out


def api_overlay(db_engine, *, market: str = "SPY") -> dict:
    """Structured state of the index (SPY) **call-spread overwrite** overlay.

    The low-beta book's single-name options are too illiquid to write, so the overlay replaces
    per-name covered calls with one portfolio-level SPY vertical call spread (short at the target
    delta, long a further-OTM wing at the same expiry) sized to the book's market beta. This reads
    the two SPY option legs straight out of the live snapshot (Alpaca truth) and enriches the short
    leg with its options_lifecycle write terms (strike, delta, net credit collected).

    Returns ``{"active": False, ...}`` when no SPY short call is currently open. ``spot`` /
    ``gross_equity`` / ``beta_overwritten`` need the live client + account and are filled in by the
    caller (``dashboard/app.py``); the DB layer supplies the contract terms and defined risk.
    """
    with db_engine.connect() as conn:
        snap = _latest_snapshot(conn)
        writes = conn.execute(
            select(db.options_lifecycle)
            .where(db.options_lifecycle.c.event_type == "write")
            .order_by(db.options_lifecycle.c.ts)).mappings().all()
    positions = (snap or {}).get("positions") or {}
    weights = (snap or {}).get("weights") or {}
    nav = (snap or {}).get("nav")
    # Latest write terms per option symbol (a later rewrite supersedes an earlier one).
    meta: dict[str, dict] = {}
    for r in writes:
        meta[r["option_symbol"]] = {"strike": r["strike"], "delta": r["delta"],
                                    "premium": r["premium"], "contracts": r["contracts"]}
    short = long_leg = None                                  # the two SPY call legs (short + wing)
    for sym, qty in positions.items():
        occ = _parse_occ(sym)
        if occ is None or occ["type"] != "call" or occ["underlying"] != market:
            continue
        q = float(qty)
        if q < 0 and short is None:
            short = (sym, q, occ)
        elif q > 0 and long_leg is None:
            long_leg = (sym, q, occ)
    if short is None:
        return {"active": False, "market": market}
    ssym, sqty, socc = short
    m = meta.get(ssym, {})
    contracts = int(round(abs(sqty)))
    short_strike = m["strike"] if m.get("strike") is not None else socc["strike"]
    long_strike = long_leg[2]["strike"] if long_leg else None
    premium_total = m.get("premium")                        # +cash collected (net credit, real fill)
    net_credit = round(premium_total / (contracts * 100), 2) if (premium_total and contracts) else None
    width = round(long_strike - short_strike, 2) if long_strike is not None else None
    # Defined risk of the vertical: (width − credit) per spread × 100 × N, less the credit kept.
    max_risk = (round((width - (net_credit or 0.0)) * 100 * contracts, 2)
                if (width is not None) else None)
    mv = (float(weights.get(ssym, 0.0)) * nav) if nav is not None else None
    long_mv = (float(weights.get(long_leg[0], 0.0)) * nav) if (long_leg and nav is not None) else None
    # The banked-premium vs liability-mark distinction (Diego, 2026-07-06): the short leg's
    # NEGATIVE market value is the cost of the obligation, not premium lost — the credit is
    # already cash. cost_to_close nets both legs (pay |short|, receive the wing); unrealized
    # P&L = credit collected − cost to close, realizing as theta grinds the mark to zero.
    cost_to_close = None
    if mv is not None:
        cost_to_close = round(abs(mv) - (long_mv or 0.0), 2)
    unrealized = (round(premium_total - cost_to_close, 2)
                  if (premium_total is not None and cost_to_close is not None) else None)
    return {"active": True, "market": market, "contracts": contracts,
            "short_symbol": ssym, "short_strike": short_strike, "long_strike": long_strike,
            "width": width, "expiration": socc["expiration"], "short_delta": m.get("delta"),
            "net_credit": net_credit, "premium_total": premium_total, "max_risk": max_risk,
            "short_market_value": round(mv, 2) if mv is not None else None,
            "long_market_value": round(long_mv, 2) if long_mv is not None else None,
            "cost_to_close": cost_to_close, "unrealized_pnl": unrealized}


def api_chase(db_engine, *, cycle_key: str | None = None) -> dict:
    """Per-symbol chase state for the execution visualizer's **chase board** (Phase 2).

    Replays ``order_events`` for one rebalance cycle (the most recent by default) and collapses it
    to, per name: the latest posted child limit and its bid/ask/mid (so the board can place the
    walking marker on the spread), the liquidity tier, cumulative fill vs target, broker status,
    and how many rounds it has taken. ``order_events`` is best-effort engine telemetry, so an
    absent/empty table simply yields ``{"orders": []}`` — the panel degrades to run-progress only.
    """
    try:
        with db_engine.connect() as conn:
            if cycle_key is None:
                cycle_key = conn.execute(
                    select(db.order_events.c.cycle_key)
                    .order_by(desc(db.order_events.c.ts)).limit(1)).scalar()
            if cycle_key is None:
                return {"cycle_key": None, "orders": []}
            rows = conn.execute(
                select(db.order_events).where(db.order_events.c.cycle_key == cycle_key)
                .order_by(db.order_events.c.ts)).mappings().all()
    except Exception:  # noqa: BLE001 — telemetry table may not exist yet; degrade quietly
        return {"cycle_key": None, "orders": []}
    by_sym: dict[str, dict] = {}
    for r in rows:
        s = by_sym.setdefault(r["symbol"], {
            "symbol": r["symbol"], "side": r["side"], "tier": r["tier"], "rounds": set(),
            "posts": 0, "target_qty": r["target_qty"], "filled_qty": 0, "status": None,
            "bid": None, "ask": None, "mid": None, "limit_price": None, "round": None})
        if r["side"]:
            s["side"] = r["side"]
        if r["tier"]:
            s["tier"] = r["tier"]
        if r["target_qty"] is not None:
            s["target_qty"] = r["target_qty"]
        if r["round"]:
            s["rounds"].add(r["round"])
        if r["filled_qty"] is not None:
            s["filled_qty"] = r["filled_qty"]
        if r["event"] == "post":
            s["posts"] += 1
            s.update(bid=r["bid"], ask=r["ask"], mid=r["mid"],
                     limit_price=r["limit_price"], round=r["round"])
        elif r["event"] == "settle" and r["status"]:
            s["status"] = r["status"]
        elif r["event"] == "reject":
            s["status"] = "rejected"
    out = []
    for s in by_sym.values():
        s["n_rounds"] = len(s.pop("rounds"))
        tw, fq = s.get("target_qty"), s.get("filled_qty")
        s["fill_pct"] = (min(1.0, fq / tw) if (tw and fq is not None) else (1.0 if fq else 0.0))
        out.append(s)
    # Working names (not yet filled) first, then most-worked, then by symbol — the board's priority.
    out.sort(key=lambda d: (d.get("status") == "filled", -(d.get("n_rounds") or 0), d["symbol"]))
    return {"cycle_key": cycle_key, "orders": out}


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
             "value": r["value_score"], "beta": r["beta_score"], "lowvol": r["lowvol_score"]}
            for r in rows if r["symbol"] in held]


def api_alerts(db_engine, limit: int = 50) -> list[dict]:
    """Most recent alerts (descending), each with a message-derived ``severity``.

    Severity comes from :func:`engine.alerts.severity` on the message text — the single source
    of truth the dashboard colours by — so a top-up that *filled* deferred names reads as info,
    not a red error (the old type-name regex mislabelled it)."""
    with db_engine.connect() as conn:
        rows = conn.execute(
            select(db.alerts).order_by(desc(db.alerts.c.ts)).limit(limit)).mappings().all()
    return [{"ts": str(r["ts"]), "type": r["alert_type"], "message": r["message"],
             "severity": alert_severity(r["message"]), "delivered": r["delivered"]} for r in rows]


def api_premium_today(db_engine) -> float:
    """Net option premium booked since ET midnight (+collected on writes, −paid on closes).

    The waterfall's Premium bar. Reads ``options_lifecycle`` — rows only land on real fills,
    so this is cash truth, not plan. ET day boundary (ts stored naive-UTC).
    """
    try:
        from zoneinfo import ZoneInfo
        start_et = datetime.now(ZoneInfo("America/New_York")).replace(
            hour=0, minute=0, second=0, microsecond=0)
        start_utc = start_et.astimezone(timezone.utc).replace(tzinfo=None)
        with db_engine.connect() as conn:
            v = conn.execute(
                select(func.coalesce(func.sum(db.options_lifecycle.c.premium), 0.0))
                .where(db.options_lifecycle.c.ts >= start_utc)).scalar()
        return round(float(v or 0.0), 2)
    except Exception:  # noqa: BLE001 — attribution is decoration; never break the overview
        return 0.0


def api_manual_actions(db_engine, limit: int = 20) -> list[dict]:
    """Most recent execution-console actions (the Activity tab's audit trail), descending.

    Defensive like the other telemetry reads — a missing table (fresh install) yields ``[]``.
    """
    try:
        with db_engine.connect() as conn:
            rows = conn.execute(
                select(db.manual_actions).order_by(desc(db.manual_actions.c.ts))
                .limit(limit)).mappings().all()
    except Exception:  # noqa: BLE001
        return []
    return [{"ts": str(r["ts"]), "action": r["action"], "mode": r["mode"],
             "params": r["params"], "status": r["status"], "cycle_key": r["cycle_key"],
             "result": r["result"]} for r in rows]


# ====================================================================== #
# Operational health — the live "is it alive" panel (Phase 5.7)
# ====================================================================== #
def _as_utc(ts) -> datetime | None:
    """Coerce a stored timestamp (naive-UTC datetime or ISO string) to an aware-UTC datetime.

    Writers stamp ``datetime.now(timezone.utc)`` and the Postgres session is pinned to UTC, so
    a naive value read back is UTC wall-clock — we just attach the tzinfo. Returns ``None`` for
    anything unparseable (so age math degrades to "unknown" rather than crashing the panel).
    """
    if ts is None:
        return None
    if isinstance(ts, str):
        try:
            ts = datetime.fromisoformat(ts)
        except ValueError:
            return None
    if not isinstance(ts, datetime):
        return None
    return ts.replace(tzinfo=timezone.utc) if ts.tzinfo is None else ts.astimezone(timezone.utc)


def _alert_severity(message: str) -> str:
    """Severity for the ribbon/health worst-of roll-up — message-derived (see api_alerts)."""
    return alert_severity(message)


def _first_weekday(year: int, month: int) -> date:
    """First Mon–Fri of ``year``-``month`` (weekend-rolled, holiday-agnostic)."""
    d = date(year, month, 1)
    while d.weekday() >= 5:          # Sat=5, Sun=6 → roll forward to Monday
        d += timedelta(days=1)
    return d


def _next_rebalance_estimate(today: date) -> date:
    """Deterministic estimate of the next monthly rebalance: first weekday of the coming month.

    The cadence is the first *trading* day of each month (DECISIONS D31). This is the
    Postgres-only fallback — holiday collisions (e.g. Jan 1 / Jul 4 landing on the first
    weekday) are corrected by the live NYSE-calendar refinement in the route.
    """
    this_month = _first_weekday(today.year, today.month)
    if this_month > today:
        return this_month
    ny, nm = (today.year + 1, 1) if today.month == 12 else (today.year, today.month + 1)
    return _first_weekday(ny, nm)


def api_health(db_engine, *, now: datetime | None = None) -> dict:
    """Operational health for the live dashboard's "System health" panel (Postgres-only).

    Reports the engine heartbeat (latest snapshot age — the monitor writes every ~60s, so a
    gap means the process is wedged), the last rebalance + risk-gate result, reconciliation
    drift vs the last target book, 24h alert counts by severity, data-freshness timestamps,
    and a deterministic *estimate* of the next rebalance. Market open/closed and a
    holiday-correct next-rebalance date need the Alpaca calendar/clock, so the route layers
    those on (``market`` is absent here) — keeping this pure and unit-testable on sqlite.
    """
    now = now or datetime.now(timezone.utc)
    today = now.date()
    with db_engine.connect() as conn:
        snap = _latest_snapshot(conn)
        reb = _latest_rebalance(conn)
        last_order_ts = conn.execute(select(func.max(db.orders.c.submitted_at))).scalar()
        last_factor_date = conn.execute(select(func.max(db.factor_scores.c.date))).scalar()
        alert_rows = conn.execute(
            select(db.alerts.c.message, db.alerts.c.ts).order_by(desc(db.alerts.c.ts))).all()

    # --- engine heartbeat (snapshot freshness) ---
    snap_ts = _as_utc(snap.get("ts")) if snap else None
    age = (now - snap_ts).total_seconds() if snap_ts else None
    if age is None:
        eng_status = "down"
    elif age < _HB_LIVE_S:
        eng_status = "live"
    elif age < _HB_STALE_S:
        eng_status = "stale"
    else:
        eng_status = "down"

    # --- reconciliation drift vs the last target book ---
    weights = (snap or {}).get("weights") or {}
    targets = (reb or {}).get("target_weights") or {}
    devs = {s: float(weights.get(s, 0.0)) - float(targets.get(s, 0.0))
            for s in (set(weights) | set(targets)) if not _is_option(s)}
    n_drifting = sum(1 for v in devs.values() if abs(v) > _DRIFT_NAME_EPS)
    max_name, max_dev = None, 0.0
    for s, v in devs.items():
        if abs(v) > abs(max_dev):
            max_name, max_dev = s, v

    # --- alerts in the last 24h, by severity (message-derived) ---
    since = now - timedelta(hours=24)
    recent = [(msg, ts) for msg, ts in alert_rows
              if (_as_utc(ts) is not None and _as_utc(ts) >= since)]
    sev = [_alert_severity(msg) for msg, _ts in recent]
    errs, warns = sev.count("error"), sev.count("warn")
    # The single most recent alert (any age) — surfaced in the header so "alert of what?" is
    # answered at a glance, not just a count.
    latest = None
    if alert_rows:
        lmsg, lts = alert_rows[0]
        latest = {"message": lmsg, "severity": _alert_severity(lmsg), "ts": str(lts)}

    reb_ts = _as_utc((reb or {}).get("ts")) if reb else None
    est = _next_rebalance_estimate(today)
    return {
        "now": now.isoformat(),
        "engine": {"status": eng_status,
                   "snapshot_ts": str(snap.get("ts")) if snap else None,
                   "age_s": round(age) if age is not None else None},
        "last_rebalance": None if not reb else {
            "ts": str(reb.get("ts")),
            "date": reb_ts.date().isoformat() if reb_ts else None,
            "trigger": reb.get("trigger_reason"),
            "gate_passed": reb.get("risk_gate_passed"),
            "gate_reason": reb.get("risk_gate_reason")},
        "next_rebalance": {"date": est.isoformat(), "source": "estimated",
                           "days_until": (est - today).days},
        "drift": {"l1": (snap or {}).get("drift"), "n_drifting": n_drifting,
                  "max_name": max_name, "max_dev": (max_dev if max_name else None)},
        "alerts_24h": {"total": len(sev), "errors": errs, "warnings": warns,
                       "worst": ("error" if errs else "warn" if warns else "ok"),
                       "latest": latest},
        "freshness": {"snapshot_ts": str(snap.get("ts")) if snap else None,
                      "last_order_ts": str(last_order_ts) if last_order_ts else None,
                      "factors_date": str(last_factor_date) if last_factor_date else None},
    }


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


def _monthly_returns(dates: list[str], navs: list[float]) -> list[dict]:
    """Month-over-month returns from the daily NAV curve (for the calendar heatmap).

    Each calendar month's return chains off the **prior month's last NAV** (the standard monthly
    return); the very first month is based off the first NAV in the series, so it reads as a
    partial-month return from inception. ``days`` (trading days seen in the month) lets the UI mark
    a one- or two-day partial month as provisional rather than a full month.
    """
    by_month: dict[str, list[float]] = {}
    order: list[str] = []
    for d, v in zip(dates, navs):
        ym = str(d)[:7]                       # YYYY-MM
        if ym not in by_month:
            by_month[ym] = []
            order.append(ym)
        by_month[ym].append(v)
    out: list[dict] = []
    prev_close: float | None = None
    for ym in order:
        vals = by_month[ym]
        base = prev_close if prev_close is not None else vals[0]
        ret = (vals[-1] / base - 1) if base else None
        out.append({"year": int(ym[:4]), "month": int(ym[5:7]), "ret": ret, "days": len(vals)})
        prev_close = vals[-1]
    return out


def api_track_record(db_engine, *, start: str | None = None,
                     account: str = PRIMARY_ACCOUNT) -> dict:
    """Realized paper performance since inception, from the ``snapshots`` equity curve.

    ``start`` (ISO ``YYYY-MM-DD``) windows the curve to snapshots on/after that date and re-bases
    the normalized series + all stats to it (the dashboard's start-date / period picker).

    Returns the daily NAV series + normalized curve + stats (return/vol/Sharpe/drawdown) and
    lifetime premium collected. ``available`` is False until the monitor has written snapshots;
    ``mature`` gates the annualized numbers (noisy with < ~10 days). Benchmark comparison is
    layered on in the route (needs Alpaca), so this stays Postgres-only + unit-testable.
    """
    with db_engine.connect() as conn:
        raw = conn.execute(
            select(db.snapshots.c.ts, db.snapshots.c.nav, db.snapshots.c.positions)
            .where(db.snapshots.c.account == account)
            .order_by(db.snapshots.c.ts)).all()
        premium = conn.execute(
            select(func.coalesce(func.sum(db.options_lifecycle.c.premium), 0.0))
        ).scalar() if account == PRIMARY_ACCOUNT else 0.0
    raw = [(ts, nav, pos) for ts, nav, pos in raw if nav is not None]
    # The comparison starts at EXPOSURE, not at the first snapshot: cash-only days before the
    # first rebalance are not the strategy (they'd dilute every stat and shift the benchmark
    # base). Default start = first snapshot actually holding something; an explicit ``start``
    # (the dashboard's date picker) overrides in either direction.
    exposure_start = next((str(ts)[:10] for ts, _n, pos in raw
                           if any(q for q in (pos or {}).values())), None)
    eff_start = start or exposure_start
    rows = ([(ts, nav) for ts, nav, _p in raw if str(ts)[:10] >= eff_start]
            if eff_start else [])
    if not rows:
        return {"available": False, "days": 0, "dates": [], "nav": [], "norm": [],
                "exposure_start": exposure_start,
                "premium_collected": float(premium or 0.0)}
    dates, navs = _daily_nav(rows)
    stats = series_stats(navs)
    norm = [v / navs[0] for v in navs] if navs[0] else navs
    # Action markers for the growth chart + the picker's preset chips: every landed
    # rebalance and every completed console action inside the window. Best-effort.
    events: list[dict] = []
    try:
        with db_engine.connect() as conn:
            rl = conn.execute(select(db.rebalance_log.c.ts, db.rebalance_log.c.trigger_reason)
                              .where(db.rebalance_log.c.risk_gate_passed.is_(True))).all()
            ma = conn.execute(select(db.manual_actions.c.ts, db.manual_actions.c.action,
                                     db.manual_actions.c.mode)
                              .where(db.manual_actions.c.status == "done")).all()
        events = ([{"date": str(ts)[:10], "type": "rebalance", "label": f"rebalance · {trg}"}
                   for ts, trg in rl]
                  + [{"date": str(ts)[:10], "type": str(act), "label": f"{act} · {mode}"}
                     for ts, act, mode in ma if act != "rebalance"])
        events = sorted((e for e in events if e["date"] >= dates[0]),
                        key=lambda e: e["date"])[:60]
    except Exception:  # noqa: BLE001 — markers are decoration
        events = []
    return {"available": True, "inception": dates[0], "days": len(dates),
            "mature": len(dates) >= 10, "nav0": navs[0], "nav_now": navs[-1],
            "exposure_start": exposure_start, "start": eff_start, "events": events,
            "premium_collected": float(premium or 0.0), "dates": dates, "nav": navs,
            "norm": norm, "monthly": _monthly_returns(dates, navs), **stats}


# ====================================================================== #
# Risk analytics — drawdown / volatility / VaR from the equity curve
# ====================================================================== #
_Z95 = 1.6448536269514722   # one-sided 95% standard-normal quantile (parametric VaR)


def _percentile(asc: list[float], q: float):
    """Linearly-interpolated quantile ``q`` ∈ [0,1] of an ascending list (``None`` if empty)."""
    if not asc:
        return None
    if len(asc) == 1:
        return asc[0]
    idx = q * (len(asc) - 1)
    lo, hi = math.floor(idx), math.ceil(idx)
    if lo == hi:
        return asc[int(idx)]
    return asc[lo] + (asc[hi] - asc[lo]) * (idx - lo)


def _drawdown_series(navs: list[float]) -> tuple[list[float], float, float, int, int]:
    """Underwater curve ``vᵢ/peakᵢ − 1`` (≤ 0). Returns (series, current, max, max_idx, days_in_dd)."""
    dd: list[float] = []
    peak = navs[0] if navs else 0.0
    mdd, mdd_idx = 0.0, 0
    for i, v in enumerate(navs):
        peak = max(peak, v)
        d = (v / peak - 1.0) if peak else 0.0
        dd.append(d)
        if d < mdd:
            mdd, mdd_idx = d, i
    cur = dd[-1] if dd else 0.0
    dci = 0
    for d in reversed(dd):           # consecutive trailing days below the high-water mark
        if d < -1e-9:
            dci += 1
        else:
            break
    return dd, cur, mdd, mdd_idx, dci


def _rolling_vol(rets: list[float], window: int) -> list[float | None]:
    """Annualized volatility over a trailing ``window`` of daily returns; ``None`` until it fills.

    Chosen over a rolling *Sharpe* for the risk view: annualized Sharpe on a short window is wildly
    unstable (a near-flat stretch sends it to ±∞), whereas rolling vol is bounded, always positive,
    and reads directly as the book's volatility regime — it lifts as the equity curve gets choppy.
    """
    out: list[float | None] = []
    for i in range(len(rets)):
        if i + 1 < window:
            out.append(None)
            continue
        w = rets[i + 1 - window: i + 1]
        mean = sum(w) / len(w)
        sd = math.sqrt(sum((r - mean) ** 2 for r in w) / (len(w) - 1)) if len(w) > 1 else 0.0
        out.append(sd * math.sqrt(_TRADING_DAYS))
    return out


def _rolling_sharpe(rets: list[float], window: int) -> list[float | None]:
    """Annualized rolling Sharpe over a trailing ``window`` of daily returns; ``None`` until it fills.

    Rolling Sharpe is inherently jumpy on short windows — a near-flat stretch sends the ratio toward
    ±∞ — so a near-zero window volatility yields ``None`` rather than a spike (the guard the plain
    rolling-vol chart avoids by construction). Shown alongside rolling vol so a rising Sharpe reads
    as improving risk-adjusted return, not just calmer markets.
    """
    out: list[float | None] = []
    for i in range(len(rets)):
        if i + 1 < window:
            out.append(None)
            continue
        w = rets[i + 1 - window: i + 1]
        mean = sum(w) / len(w)
        sd = math.sqrt(sum((r - mean) ** 2 for r in w) / (len(w) - 1)) if len(w) > 1 else 0.0
        out.append((mean / sd) * math.sqrt(_TRADING_DAYS) if sd > 1e-9 else None)
    return out


def api_risk(db_engine, *, window: int = 10, start: str | None = None,
             account: str = PRIMARY_ACCOUNT) -> dict:
    """Risk analytics from the ``snapshots`` equity curve (Postgres-only, unit-testable).

    Everything derives from the daily NAV series: an underwater (drawdown) curve with the current
    and worst peak-to-trough decline, annualized/daily volatility, a 1-day 95% VaR (both the
    parametric-normal estimate and the empirical 5th-percentile of daily returns) plus its CVaR
    tail average, a since-inception Sharpe, and a rolling Sharpe series. ``available`` is False
    until the monitor writes snapshots; ``mature`` gates the noisy annualized figures (< ~10 days).
    """
    with db_engine.connect() as conn:
        raw = conn.execute(
            select(db.snapshots.c.ts, db.snapshots.c.nav, db.snapshots.c.positions)
            .where(db.snapshots.c.account == account)
            .order_by(db.snapshots.c.ts)).all()
    raw = [(ts, nav, pos) for ts, nav, pos in raw if nav is not None]
    # Same window rule as the track record (Returns and Risk must describe the SAME period):
    # default start = first exposure; the dashboard's date picker overrides via ``start``.
    exposure_start = next((str(ts)[:10] for ts, _n, pos in raw
                           if any(q for q in (pos or {}).values())), None)
    eff_start = start or exposure_start
    rows = ([(ts, nav) for ts, nav, _p in raw if str(ts)[:10] >= eff_start]
            if eff_start else [])
    if not rows:
        return {"available": False, "days": 0}
    dates, navs = _daily_nav(rows)
    stats = series_stats(navs)
    dd, cur_dd, max_dd, mdd_idx, dci = _drawdown_series(navs)
    rets = [navs[i] / navs[i - 1] - 1 for i in range(1, len(navs)) if navs[i - 1]]
    nav_now = navs[-1]
    daily_vol = (stats["ann_vol"] / math.sqrt(_TRADING_DAYS)) if stats["ann_vol"] is not None else None
    var_param = (_Z95 * daily_vol) if daily_vol is not None else None
    asc = sorted(rets)
    q05 = _percentile(asc, 0.05) if asc else None        # 5th-pct daily return (≤ 0 in a loss)
    var_hist = (-q05) if (q05 is not None and q05 < 0) else 0.0
    tail = [r for r in asc if q05 is not None and r <= q05]
    cvar = (-sum(tail) / len(tail)) if tail else var_hist
    roll = [None] + _rolling_vol(rets, window)            # align to dates (first day has no return)
    roll_sharpe = [None] + _rolling_sharpe(rets, window)
    return {
        "available": True, "mature": len(dates) >= 10, "days": len(dates),
        "dates": dates, "drawdown": dd, "rolling_vol": roll, "rolling_sharpe": roll_sharpe,
        "rolling_window": window,
        "current_drawdown": cur_dd, "max_drawdown": max_dd, "max_drawdown_date": dates[mdd_idx],
        "days_in_drawdown": dci, "peak_nav": max(navs), "nav_now": nav_now,
        "ann_vol": stats["ann_vol"], "daily_vol": daily_vol, "ann_return": stats["ann_return"],
        "sharpe": stats["sharpe"], "var95_1d_pct": var_param,
        "var95_1d_usd": (var_param * nav_now) if var_param is not None else None,
        "hist_var95_1d_pct": var_hist, "cvar95_1d_pct": cvar,
        "returns": rets,   # daily-return series → distribution histogram
        "nav": navs,       # the route layers rolling realized beta on top (needs SPY closes)
    }


def rolling_beta(navs: list, bench: list, window: int = 20) -> list:
    """Rolling realized β of the NAV curve vs an aligned benchmark close series.

    Cov/var over ``window`` daily returns, aligned to the dates (leading Nones while the
    window fills). This is the live check on the low-beta thesis — and, once stable, the
    honest multiplier for a beta-matched benchmark line.
    """
    rs, rb = [], []
    for i in range(1, len(navs)):
        rs.append(navs[i] / navs[i - 1] - 1 if navs[i - 1] else 0.0)
        rb.append(bench[i] / bench[i - 1] - 1 if (bench[i] and bench[i - 1]) else 0.0)
    out: list = [None]
    for i in range(len(rs)):
        if i + 1 < window:
            out.append(None)
            continue
        a, b = rs[i + 1 - window:i + 1], rb[i + 1 - window:i + 1]
        ma, mb = sum(a) / window, sum(b) / window
        var = sum((x - mb) ** 2 for x in b) / window
        cov = sum((x - ma) * (y - mb) for x, y in zip(a, b)) / window
        out.append(round(cov / var, 3) if var > 1e-12 else None)
    return out


def api_premium_ledger(db_engine) -> dict:
    """Monthly option-premium ledger from ``options_lifecycle``: collected (+, writes),
    paid back (−, closes/rolls), net, and lifetime capture — the REALIZED answer to
    'what premium yield am I earning', replacing estimates once cycles land."""
    try:
        with db_engine.connect() as conn:
            rows = conn.execute(
                select(db.options_lifecycle.c.ts, db.options_lifecycle.c.premium)
                .order_by(db.options_lifecycle.c.ts)).all()
    except Exception:  # noqa: BLE001
        return {"available": False, "months": []}
    months: dict[str, dict] = {}
    for ts, prem in rows:
        if prem is None:
            continue
        d = months.setdefault(str(ts)[:7], {"month": str(ts)[:7], "collected": 0.0,
                                            "paid": 0.0, "net": 0.0})
        p = float(prem)
        d["collected" if p >= 0 else "paid"] += abs(p)
        d["net"] += p
    out = [{k: (round(v, 2) if isinstance(v, float) else v) for k, v in m.items()}
           for m in months.values()]
    coll = round(sum(m["collected"] for m in out), 2)
    paid = round(sum(m["paid"] for m in out), 2)
    return {"available": bool(out), "months": out, "collected": coll, "paid": paid,
            "net": round(coll - paid, 2),
            "capture": round((coll - paid) / coll, 4) if coll else None}


def api_tca(db_engine, limit: int = 12) -> dict:
    """Transaction-cost analysis over the chase telemetry: per cycle — style (normal vs
    express), names, fill rate, average ladder rounds, and average signed slippage vs the
    first-post mid (+bps = paid up). Express vs normal finally becomes a measured number."""
    try:
        with db_engine.connect() as conn:
            ev = conn.execute(select(db.order_events).order_by(db.order_events.c.ts)).mappings().all()
            od = conn.execute(select(db.orders.c.rebalance_cycle, db.orders.c.symbol,
                                     db.orders.c.side, db.orders.c.filled_qty,
                                     db.orders.c.filled_avg_price)).all()
    except Exception:  # noqa: BLE001
        return {"available": False, "cycles": []}
    if not ev:
        return {"available": False, "cycles": []}
    fills: dict = {}
    for cyc, sym, side, fq, fp in od:
        if fp and fq:
            fills[(cyc, sym)] = (str(side), float(fp))
    cycles: dict[str, dict] = {}
    for r in ev:
        c = cycles.setdefault(r["cycle_key"], {"date": str(r["ts"])[:10], "syms": {},
                                               "express": False})
        s = c["syms"].setdefault(r["symbol"], {"mid": None, "rounds": 0, "filled": 0, "target": 0})
        if r["event"] == "post":
            s["rounds"] += 1
            if s["mid"] is None and r["mid"]:
                s["mid"] = float(r["mid"])
        if r["tier"] == "express":
            c["express"] = True
        if r["filled_qty"]:
            s["filled"] = max(s["filled"], int(r["filled_qty"]))
        if r["target_qty"]:
            s["target"] = max(s["target"], int(r["target_qty"]))
    out = []
    for key, c in cycles.items():
        bps, rounds, n_filled = [], [], 0
        for sym, s in c["syms"].items():
            if s["target"] and s["filled"] >= s["target"]:
                n_filled += 1
            rounds.append(max(s["rounds"], 1))
            f = fills.get((key, sym))
            if f and s["mid"]:
                side, px = f
                bps.append((1 if side == "buy" else -1) * (px - s["mid"]) / s["mid"] * 1e4)
        out.append({"cycle": key, "date": c["date"],
                    "style": "express" if c["express"] else "normal",
                    "names": len(c["syms"]), "filled": n_filled,
                    "avg_rounds": round(sum(rounds) / len(rounds), 1) if rounds else None,
                    "avg_bps": round(sum(bps) / len(bps), 1) if bps else None})
    out.sort(key=lambda d: d["date"], reverse=True)
    return {"available": True, "cycles": out[:limit]}


def api_risk_contributions(db_engine) -> dict:
    """Per-name risk decomposition from the latest rebalance (engine-computed, Postgres-only).

    The engine persists an Euler risk decomposition into ``rebalance_log.risk_contributions`` at
    each rebalance (it has the covariance Σ there); the dashboard just reads the most recent one.
    Returns names sorted by risk share (descending) each carrying their ``rc_pct`` (fraction of
    total portfolio variance) and portfolio weight — so the chart can show which names carry more
    risk than their weight. ``available`` is False until a rebalance has written a decomposition.
    """
    with db_engine.connect() as conn:
        row = conn.execute(
            select(db.rebalance_log.c.ts, db.rebalance_log.c.risk_contributions)
            .where(db.rebalance_log.c.risk_contributions.isnot(None))
            .order_by(desc(db.rebalance_log.c.ts)).limit(1)).mappings().first()
    rcx = (row or {}).get("risk_contributions") if row else None
    contrib = (rcx or {}).get("contrib") or {}
    if not contrib:
        return {"available": False, "names": []}
    weight = (rcx or {}).get("weight") or {}
    names = sorted(({"symbol": s, "rc_pct": float(p), "weight": float(weight.get(s, 0.0))}
                    for s, p in contrib.items()),
                   key=lambda d: d["rc_pct"], reverse=True)
    return {"available": True, "ts": str(row["ts"]),
            "portfolio_vol": (rcx or {}).get("portfolio_vol"), "names": names}


def _pearson(x: list[float], y: list[float]) -> float | None:
    """Pearson correlation of two equal-length series (``None`` if either is flat / < 2 points)."""
    n = len(x)
    if n < 2 or len(y) != n:
        return None
    mx, my = sum(x) / n, sum(y) / n
    sxx = sum((a - mx) ** 2 for a in x)
    syy = sum((b - my) ** 2 for b in y)
    if sxx <= 1e-18 or syy <= 1e-18:
        return None
    sxy = sum((a - mx) * (b - my) for a, b in zip(x, y))
    return max(-1.0, min(1.0, sxy / math.sqrt(sxx * syy)))


def correlation_matrix(closes_by_sym: dict[str, dict[str, float]], symbols: list[str],
                       *, window: int = 60, min_obs: int = 5) -> dict:
    """Pairwise Pearson correlation of daily returns across ``symbols`` (trailing window).

    ``closes_by_sym`` maps each symbol to a ``{ISO-date: close}`` series. Dates common to **all**
    requested symbols are intersected (large-cap names share the exchange calendar, so this ≈ the
    full window), daily simple returns are taken over the most-recent ``window`` observations, and
    the full N×N Pearson matrix is built. The diagonal is ``1.0``; a pair with < ``min_obs``
    overlapping returns is ``None`` (insufficient data → the UI shows a dashed placeholder, §8).
    ``available`` is False until ≥ 2 symbols each carry a usable series.
    """
    syms = [s for s in symbols if closes_by_sym.get(s)]
    if len(syms) < 2:
        return {"available": False, "symbols": [], "matrix": [], "n_obs": 0}
    common = set.intersection(*(set(closes_by_sym[s]) for s in syms))
    dates = sorted(common)[-(window + 1):]          # +1 close → ``window`` returns
    rets: dict[str, list[float]] = {}
    for s in syms:
        c = closes_by_sym[s]
        series = [c[d] for d in dates]
        rets[s] = [series[i] / series[i - 1] - 1 for i in range(1, len(series)) if series[i - 1]]
    n_obs = len(dates) - 1 if dates else 0
    if n_obs < min_obs:
        return {"available": False, "symbols": syms, "matrix": [], "n_obs": n_obs}
    matrix = [[1.0 if i == j else _pearson(rets[a], rets[b])
               for j, b in enumerate(syms)] for i, a in enumerate(syms)]
    return {"available": True, "symbols": syms, "matrix": matrix, "n_obs": n_obs,
            "window": window, "start": dates[0], "end": dates[-1]}


def _slippage_core(records, arrival_mid) -> dict:
    """Execution quality (implementation shortfall): realized fill vs the **arrival** reference.

    The reference is the arrival price at submit — ``arrival_mid(symbol, submitted_at) -> float |
    None`` (a spread-guarded NBBO mid / last trade, see :func:`arrival_reference`) — for **every**
    order, falling back to the order's ``limit_price`` only when no arrival price resolves (and
    skipping when neither exists). Benchmarking against the order's own marketable limit is wrong:
    it's padded to the touch, so a fill inside it looks like a big fake "gain". ``slippage =
    (fill − intended)`` is signed so **positive = adverse** (paid more on a buy / received less on
    a sell), in bps of the intended price and in dollars; aggregates are notional-weighted.
    """
    fills: list[dict] = []
    tot_usd = tot_notional = wbps = 0.0
    for r in records:
        filled, fq = r.get("filled_avg_price"), (r.get("filled_qty") or 0)
        if not filled or not fq:
            continue
        # Benchmark against the ARRIVAL price (implementation shortfall) for every order — not the
        # order's own marketable limit, which is padded to the touch and produces fake gains (INBX
        # filled ~$95 vs a $108.87 crossed-to-the-ask limit → a bogus −$1,470 "gain"). Fall back to
        # the limit only when no arrival reference resolves.
        ref = arrival_mid(r.get("symbol"), r.get("submitted_at"))
        limit = r.get("limit_price")
        if ref:
            intended, basis = float(ref), "arrival"
        elif limit:
            intended, basis = float(limit), "limit"
        else:                                    # nothing to measure against → skip
            continue
        side = str(r.get("side")).lower()
        adverse = (filled - intended) if side == "buy" else (intended - filled)
        bps = adverse / intended * 1e4
        usd, notional = adverse * fq, filled * fq
        tot_usd += usd
        tot_notional += notional
        wbps += bps * notional
        fills.append({"symbol": r.get("symbol"), "side": side, "qty": fq,
                      "type": (str(r.get("order_type")).lower() if r.get("order_type")
                               else ("limit" if basis == "limit" else "market")),
                      "basis": basis,
                      "intended": round(float(intended), 2), "filled": round(float(filled), 2),
                      "slippage_bps": round(bps, 1), "slippage_usd": round(usd, 2),
                      "filled_at": str(r["filled_at"]) if r.get("filled_at") else None})
    avg_bps = (wbps / tot_notional) if tot_notional else None
    return {"n_fills": len(fills),
            "avg_slippage_bps": round(avg_bps, 1) if avg_bps is not None else None,
            "total_slippage_usd": round(tot_usd, 2), "fills": fills[:30]}


def slippage_from_orders(orders, arrival_mid) -> dict:
    """Execution quality from live Alpaca order dicts (shaped like ``AlpacaClient.get_orders``).

    Unlike :func:`api_slippage`, this **includes market orders** — their reference is the arrival
    NBBO mid via ``arrival_mid`` — so trades placed directly on Alpaca and the engine's own
    market-order legs are covered, not just engine limit orders. Callers pass only filled orders.
    """
    records = [{"symbol": o.get("symbol"), "side": o.get("side"), "order_type": o.get("type"),
                "filled_qty": o.get("filled_qty"), "limit_price": o.get("limit_price"),
                "filled_avg_price": o.get("filled_avg_price"),
                "submitted_at": o.get("submitted_at"), "filled_at": o.get("filled_at")}
               for o in orders]
    return _slippage_core(records, arrival_mid)


def fees_from_activities(activities) -> dict:
    """Aggregate Alpaca ``FEE`` activities into a total, a by-type breakdown, and recent line items.

    Alpaca reports each fee as a **negative** ``net_amount``; we surface the magnitude as a positive
    cost. ``activity_sub_type`` names the fee — CAT (Consolidated Audit Trail), TAF (FINRA Trading
    Activity Fee), REG/SEC (Section 31), etc. These are booked the morning after a trade, so they
    can lag the fills that caused them.
    """
    items: list[dict] = []
    by_type: dict[str, float] = {}
    total = 0.0
    for a in activities:
        if str(a.get("activity_type")) != "FEE":
            continue
        amt = abs(float(a.get("net_amount") or 0.0))
        if amt == 0:
            continue
        sub = a.get("activity_sub_type") or "FEE"
        by_type[sub] = round(by_type.get(sub, 0.0) + amt, 2)
        total += amt
        when = a.get("date") or (str(a.get("transaction_time"))[:10] if a.get("transaction_time") else None)
        items.append({"date": when, "type": sub, "amount": round(amt, 2),
                      "description": a.get("description") or ""})
    items.sort(key=lambda x: (x["date"] or ""), reverse=True)
    return {"total_usd": round(total, 2),
            "by_type": dict(sorted(by_type.items(), key=lambda kv: -kv[1])),
            "n": len(items), "items": items[:30]}


def api_fees(db_engine) -> dict:
    """No-broker fallback: the engine doesn't record broker fees, so there's nothing in Postgres —
    the live route in ``app.py`` supplies fees from Alpaca activities."""
    return {"total_usd": 0.0, "by_type": {}, "n": 0, "items": []}


def api_slippage(db_engine) -> dict:
    """Execution quality from the engine's filled orders in Postgres (the offline / no-broker path).

    Limit orders only: without a broker client there's no arrival quote to price a market order
    against, so market orders are excluded here — the live route in ``app.py`` covers them via
    :func:`slippage_from_orders`. See :func:`_slippage_core` for the sign convention.
    """
    with db_engine.connect() as conn:
        rows = conn.execute(
            select(db.orders).where(db.orders.c.status == "filled")
            .order_by(desc(db.orders.c.created_at))).mappings().all()
    records = [{"symbol": r["symbol"], "side": r["side"], "order_type": r["order_type"],
                "filled_qty": r["filled_qty"], "limit_price": r["limit_price"],
                "filled_avg_price": r["filled_avg_price"],
                "submitted_at": r["submitted_at"], "filled_at": r["filled_at"]}
               for r in rows]
    return _slippage_core(records, lambda *_: None)
