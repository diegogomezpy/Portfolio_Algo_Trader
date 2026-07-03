"""Operator overrides — dashboard-set values the engine honors over settings.yaml.

The execution console's *sticky* controls land here (a tiny key/value table) so they
survive process restarts and deploys, unlike an edited YAML (which ``git reset --hard``
would revert). One key today:

* ``target_leverage`` — set by the dashboard's leverage rebalance; every subsequent
  cycle sizes the book at this gross instead of ``settings.portfolio.target_leverage``,
  until cleared. Clamped to ``max_leverage`` at read time so a stale override can never
  out-lever the risk cap.

Reads are fully defensive: any DB hiccup (or a missing table on a fresh install) falls
back to settings — an override outage must never block a rebalance.
"""

from __future__ import annotations

from datetime import datetime, timezone

from engine.logger import get_logger

log = get_logger(__name__)


def get(db_engine, key: str):
    """The override value for ``key``, or ``None`` (absent, cleared, or unreadable)."""
    if db_engine is None:
        return None
    try:
        from sqlalchemy import select
        from engine.db import overrides as t
        with db_engine.connect() as conn:
            row = conn.execute(select(t.c.value).where(t.c.key == key)).first()
        return row[0] if row else None
    except Exception as exc:  # noqa: BLE001 — overrides are advisory; never fail a cycle
        log.warning("override read failed; using settings", extra={"key": key, "error": str(exc)})
        return None


def set(db_engine, key: str, value) -> None:  # noqa: A001 — mirrors the store's get/set/clear
    from sqlalchemy import insert, update
    from engine.db import overrides as t
    now = datetime.now(timezone.utc)
    with db_engine.begin() as conn:
        if conn.execute(t.select().where(t.c.key == key)).first():
            conn.execute(update(t).where(t.c.key == key).values(value=value, updated_at=now))
        else:
            conn.execute(insert(t).values(key=key, value=value, updated_at=now))
    log.warning("override set", extra={"key": key, "value": value})


def clear(db_engine, key: str) -> None:
    from sqlalchemy import delete
    from engine.db import overrides as t
    with db_engine.begin() as conn:
        conn.execute(delete(t).where(t.c.key == key))
    log.warning("override cleared", extra={"key": key})


def effective_target_leverage(db_engine, settings, *, default: float = 1.0) -> float:
    """The gross leverage the book should run at: the sticky override when set (clamped to
    ``max_leverage``), else ``settings.portfolio.target_leverage`` (else ``default``)."""
    configured = float(getattr(settings.portfolio, "target_leverage", default))
    ov = get(db_engine, "target_leverage")
    if ov is None:
        return configured
    cap = float(getattr(settings.portfolio, "max_leverage", configured))
    lev = min(max(float(ov), 0.0), cap)
    if lev != float(ov):
        log.warning("leverage override clamped to max_leverage",
                    extra={"override": float(ov), "clamped": lev})
    return lev
