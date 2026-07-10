"""Per-account strategy-spec store — the persistence behind the dashboard builder (ADR-001 / D37).

One active :class:`~engine.config_strategy.StrategySpec` per account (credstore slug), saved as JSON
in the ``strategy_specs`` table together with its rebalance cadence, execution mode, and an
``auto_enabled`` flag the scheduler (FB5) reads. No secrets here — credentials stay in
:mod:`engine.credstore`. The table is created on demand so no manual migration is needed on the VM.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Optional

from engine.logger import get_logger

log = get_logger(__name__)


def _ensure(db_engine) -> None:
    """Create the ``strategy_specs`` table if it doesn't exist yet (idempotent)."""
    from engine.db import strategy_specs
    strategy_specs.create(db_engine, checkfirst=True)


def save_spec(db_engine, account: str, spec, *, rebalance_frequency: str = "monthly",
              mode: str = "normal", auto_enabled: Optional[bool] = None) -> dict:
    """Upsert the account's strategy spec. ``spec`` is a
    :class:`~engine.config_strategy.StrategySpec`. Returns the stored summary. Preserves the
    existing ``auto_enabled`` / ``last_run`` when not overridden."""
    from sqlalchemy import insert, update
    from engine import config_strategy
    from engine.db import strategy_specs as t
    _ensure(db_engine)
    account = str(account or "").strip().lower()
    if not account:
        raise ValueError("account is required")
    payload = config_strategy.spec_to_dict(spec)
    now = datetime.now(timezone.utc)
    with db_engine.begin() as conn:
        row = conn.execute(t.select().where(t.c.account == account)).mappings().first()
        vals = dict(name=spec.name, spec=payload, rebalance_frequency=rebalance_frequency,
                    mode=mode, updated_at=now)
        if auto_enabled is not None:
            vals["auto_enabled"] = bool(auto_enabled)
        if row:
            conn.execute(update(t).where(t.c.account == account).values(**vals))
        else:
            vals.setdefault("auto_enabled", False)         # new row default (upsert preserves it later)
            conn.execute(insert(t).values(account=account, **vals))
    log.warning("strategy spec saved", extra={"account": account, "strategy": spec.name,
                                              "frequency": rebalance_frequency})
    return get_spec(db_engine, account) or {}


def get_spec(db_engine, account: str) -> Optional[dict]:
    """The account's stored spec row as a dict (or ``None``). ``spec`` is the serialized StrategySpec;
    :func:`engine.config_strategy.spec_from_dict` rebuilds the object."""
    from engine.db import strategy_specs as t
    _ensure(db_engine)
    with db_engine.connect() as conn:
        row = conn.execute(
            t.select().where(t.c.account == str(account).strip().lower())).mappings().first()
    if row is None:
        return None
    return {"account": row["account"], "name": row["name"], "spec": row["spec"],
            "rebalance_frequency": row["rebalance_frequency"], "mode": row["mode"],
            "auto_enabled": bool(row["auto_enabled"]),
            "last_run": str(row["last_run"]) if row["last_run"] else None,
            "updated_at": str(row["updated_at"]) if row["updated_at"] else None}


def list_specs(db_engine) -> list[dict]:
    from engine.db import strategy_specs as t
    _ensure(db_engine)
    with db_engine.connect() as conn:
        rows = conn.execute(t.select().order_by(t.c.account)).mappings().all()
    return [{"account": r["account"], "name": r["name"], "spec": r["spec"],
             "rebalance_frequency": r["rebalance_frequency"], "mode": r["mode"],
             "auto_enabled": bool(r["auto_enabled"]),
             "last_run": str(r["last_run"]) if r["last_run"] else None} for r in rows]


def set_auto_enabled(db_engine, account: str, enabled: bool) -> None:
    from sqlalchemy import update
    from engine.db import strategy_specs as t
    _ensure(db_engine)
    with db_engine.begin() as conn:
        conn.execute(update(t).where(t.c.account == str(account).strip().lower())
                     .values(auto_enabled=bool(enabled), updated_at=datetime.now(timezone.utc)))


def mark_run(db_engine, account: str, run_date: date) -> None:
    """Record the last date the scheduler traded this account (FB5 idempotency guard)."""
    from sqlalchemy import update
    from engine.db import strategy_specs as t
    _ensure(db_engine)
    with db_engine.begin() as conn:
        conn.execute(update(t).where(t.c.account == str(account).strip().lower())
                     .values(last_run=run_date, updated_at=datetime.now(timezone.utc)))


def delete_spec(db_engine, account: str) -> bool:
    from sqlalchemy import delete
    from engine.db import strategy_specs as t
    _ensure(db_engine)
    with db_engine.begin() as conn:
        res = conn.execute(delete(t).where(t.c.account == str(account).strip().lower()))
    return bool(res.rowcount)
