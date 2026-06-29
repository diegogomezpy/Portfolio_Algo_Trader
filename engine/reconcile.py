"""Position reconciliation against Alpaca (ARCHITECTURE; Phase 3 completion).

Runs at every pipeline startup, before any other job: PostgreSQL is reconciled to
Alpaca's live positions, and **Alpaca is always source of truth** (D13). If Alpaca is
unreachable the pipeline is *blocked* entirely rather than proceeding on stale state.

Flow:

1. Fetch live positions from Alpaca (``client.all_positions()``).
2. Read the last known positions from the most recent ``snapshots`` row.
3. Diff per symbol (:func:`diff_positions`, pure + unit-tested).
4. On any divergence beyond ``divergence_threshold``: write a corrective snapshot whose
   positions equal Alpaca's, log at WARNING, and alert. The DB now matches Alpaca.
5. If Alpaca is unreachable: alert and re-raise so the caller halts the pipeline.

The diff is a pure function; the I/O (fetch, snapshot read/write) is thin and takes an
injected client + SQLAlchemy engine, so it tests on a fake client + in-memory sqlite.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

from engine.alpaca_client import AlpacaError
from engine.logger import get_logger

log = get_logger(__name__)


@dataclass
class ReconcileResult:
    """Outcome of one reconciliation pass."""
    live_positions: dict[str, float]
    divergences: list[dict] = field(default_factory=list)
    corrected: bool = False


def fetch_live_positions(client) -> dict[str, float]:
    """Return ``{symbol: qty}`` of current Alpaca positions (signed; long-only ⇒ ≥0).

    Raises:
        AlpacaError: If Alpaca is unreachable — the caller must block the pipeline
            rather than proceed on stale state (D13).
    """
    positions = client.all_positions()
    return {p["symbol"]: float(p["qty"]) for p in positions}


def last_snapshot_positions(db_engine) -> dict[str, float]:
    """Positions from the most recent ``snapshots`` row, or ``{}`` if none exist."""
    from sqlalchemy import desc, select
    from engine.db import snapshots
    with db_engine.connect() as conn:
        row = conn.execute(
            select(snapshots.c.positions).order_by(desc(snapshots.c.ts)).limit(1)
        ).first()
    if row is None or row[0] is None:
        return {}
    return {k: float(v) for k, v in row[0].items()}


def diff_positions(live: dict, db: dict, threshold: float = 0.0) -> list[dict]:
    """Per-symbol divergences where ``|alpaca_qty − db_qty| > threshold`` (pure).

    Covers symbols present in either side (a position missing from one is a divergence
    against 0). Returns ``[{symbol, alpaca_qty, db_qty, delta}]`` sorted by symbol.
    """
    out = []
    for sym in sorted(set(live) | set(db)):
        a = float(live.get(sym, 0.0))
        d = float(db.get(sym, 0.0))
        if abs(a - d) > threshold:
            out.append({"symbol": sym, "alpaca_qty": a, "db_qty": d, "delta": a - d})
    return out


def reconcile(client, db_engine=None, *, divergence_threshold: float = 0.0, alert=None) -> ReconcileResult:
    """Reconcile the DB's position view to Alpaca; block if Alpaca is unreachable.

    Args:
        client: an :class:`AlpacaClient` (read).
        db_engine: SQLAlchemy engine; ``None`` skips the DB compare (fetch-and-report).
        divergence_threshold: ignore qty differences at or below this (0 = exact).
        alert: optional ``callable(str)`` for divergence / unreachable notifications.

    Returns:
        :class:`ReconcileResult`.

    Raises:
        AlpacaError: If Alpaca is unreachable (after alerting) — the pipeline must halt.
    """
    try:
        live = fetch_live_positions(client)
    except AlpacaError as exc:
        log.error("reconcile: Alpaca unreachable — blocking pipeline", extra={"error": str(exc)})
        if alert:
            alert(f"reconcile blocked: Alpaca unreachable: {exc}")
        raise

    if db_engine is None:
        log.info("reconcile (no DB): fetched live positions", extra={"positions": len(live)})
        return ReconcileResult(live)

    db_pos = last_snapshot_positions(db_engine)
    divergences = diff_positions(live, db_pos, divergence_threshold)
    if not divergences:
        log.info("reconcile: DB positions match Alpaca", extra={"positions": len(live)})
        return ReconcileResult(live, [], False)

    log.warning("reconcile: position divergence vs DB; correcting to Alpaca",
                extra={"divergences": divergences})
    if alert:
        alert(f"position divergence on {len(divergences)} name(s); DB corrected to Alpaca")
    _write_correction_snapshot(client, db_engine, live)
    return ReconcileResult(live, divergences, True)


def reconcile_orders(client, db_engine, *, status: str = "all", limit: int = 200,
                     alert=None) -> int:
    """Reconcile the ``orders`` table to Alpaca's current order statuses — the blotter's truth.

    Reads recent Alpaca orders and upserts each by id: an existing row's *status* fields
    (status / filled_qty / filled_avg_price / timestamps) are refreshed in place (the immutable
    order facts and the cycle key are left untouched), and any order the DB doesn't have yet
    (e.g. option orders, which the executor doesn't persist) is inserted. This keeps the
    dashboard's activity blotter exactly in step with Alpaca — a stale ``pending_cancel`` becomes
    ``canceled``, a late fill is reflected. Best-effort: an Alpaca read failure logs and returns
    0 rather than crashing the daily job.

    Returns the number of rows updated or inserted.
    """
    if db_engine is None:
        return 0
    try:
        live = client.get_orders(status=status, limit=limit)
    except Exception as exc:  # noqa: BLE001 — blotter parity must never crash the job
        log.warning("order reconcile skipped; get_orders failed", extra={"error": str(exc)})
        return 0
    n = sum(1 for od in live if _upsert_order_status(db_engine, od))
    if n:
        log.info("order reconcile: blotter synced to Alpaca", extra={"reconciled": n, "fetched": len(live)})
    return n


def _upsert_order_status(db_engine, od: dict) -> bool:
    """Refresh one order row to Alpaca's state (status fields only on update; full insert if new)."""
    from sqlalchemy import insert, select, update
    from engine.db import orders as orders_t
    oid = str(od.get("id") or "")
    if not oid:
        return False
    mutable = {
        "status": od.get("status"),
        "filled_qty": od.get("filled_qty"),
        "filled_avg_price": od.get("filled_avg_price"),
        "limit_price": od.get("limit_price"),
        "submitted_at": _dt(od.get("submitted_at")),
        "filled_at": _dt(od.get("filled_at")),
    }
    with db_engine.begin() as conn:
        exists = conn.execute(select(orders_t.c.id).where(orders_t.c.id == oid)).first()
        if exists:
            conn.execute(update(orders_t).where(orders_t.c.id == oid).values(**mutable))
        else:                                            # order the executor never persisted (e.g. options)
            conn.execute(insert(orders_t).values(
                id=oid, client_order_id=od.get("client_order_id"), rebalance_cycle=None,
                symbol=od.get("symbol"), side=od.get("side"), qty=od.get("qty"),
                order_type=od.get("order_type"), **mutable))
    return True


def _dt(value):
    """Parse an ISO timestamp string (or pass through a datetime / None) for a DateTime column."""
    if value is None or isinstance(value, datetime):
        return value
    try:
        return datetime.fromisoformat(str(value))
    except ValueError:
        return None


def _write_correction_snapshot(client, db_engine, live: dict[str, float]) -> None:
    """Write a snapshot whose positions equal Alpaca's (the 'DB matches Alpaca' step)."""
    from sqlalchemy import insert
    from engine.db import snapshots
    nav = cash = last_equity = None
    try:
        acct = client.account()
        nav, cash, last_equity = acct.get("equity"), acct.get("cash"), acct.get("last_equity")
    except AlpacaError as exc:                       # positions read fine; account hiccup
        log.warning("reconcile: account read failed; correction snapshot without nav/cash",
                    extra={"error": str(exc)})
    with db_engine.begin() as conn:
        conn.execute(insert(snapshots).values(
            ts=datetime.now(timezone.utc), nav=nav, cash=cash, last_equity=last_equity,
            positions=live, weights={}, drift=None))
