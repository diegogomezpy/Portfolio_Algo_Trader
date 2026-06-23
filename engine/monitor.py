"""Portfolio monitor — live NAV, drift, and snapshots (ARCHITECTURE; Phase 3).

Runs continuously (every 60s) between rebalances. Each pass:

* computes live NAV and cash from the Alpaca account, and current weights from position
  market values;
* computes L1 drift between current weights and the last target weights — if it exceeds
  ``rebalancing.drift_threshold_l1`` it logs a warning and flags an out-of-cycle
  rebalance (the daily ``drift_check_job`` acts on the flag);
* writes a portfolio snapshot to the ``snapshots`` table (dashboard + reconciliation).

The DTE check on held covered calls is a Phase 4 concern (no options are written until
then) and is intentionally omitted here.

Pure math (:func:`compute_weights`, :func:`l1_drift`) is separated from I/O
(:func:`monitor_once`, :func:`last_target_weights`); the I/O takes an injected client +
SQLAlchemy engine, so it tests on a fake client + in-memory sqlite.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Mapping, Optional

import pandas as pd

from engine.logger import get_logger

log = get_logger(__name__)


@dataclass
class MonitorResult:
    """One monitor pass: NAV/cash, current weights/positions, drift, and the flag."""
    nav: Optional[float]
    cash: Optional[float]
    weights: pd.Series
    positions: dict[str, float]
    drift: Optional[float]
    drift_breach: bool


# ====================================================================== #
# Pure math — unit-tested
# ====================================================================== #
def compute_weights(positions: list[dict], nav: float) -> pd.Series:
    """Current weights = each position's market value / NAV. Empty if NAV ≤ 0."""
    if not nav or nav <= 0:
        return pd.Series(dtype=float)
    return pd.Series({p["symbol"]: float(p["market_value"]) / nav for p in positions},
                     dtype=float)


def l1_drift(current: Mapping[str, float] | pd.Series,
             target: Mapping[str, float] | pd.Series) -> float:
    """L1 distance ``Σ |current − target|`` over the union of names (missing → 0)."""
    cur = pd.Series(current, dtype=float)
    tgt = pd.Series(target, dtype=float)
    names = cur.index.union(tgt.index)
    return float((cur.reindex(names, fill_value=0.0) - tgt.reindex(names, fill_value=0.0)).abs().sum())


# ====================================================================== #
# I/O
# ====================================================================== #
def last_target_weights(db_engine) -> Optional[dict[str, float]]:
    """The most recent rebalance's target weights, or ``None`` if none recorded yet."""
    from sqlalchemy import desc, select
    from engine.db import rebalance_log
    with db_engine.connect() as conn:
        row = conn.execute(
            select(rebalance_log.c.target_weights).order_by(desc(rebalance_log.c.ts)).limit(1)
        ).first()
    if row is None or row[0] is None:
        return None
    return {k: float(v) for k, v in row[0].items()}


def monitor_once(
    client,
    db_engine=None,
    *,
    target_weights: Mapping[str, float] | None = None,
    drift_threshold: float | None = None,
    alert=None,
) -> MonitorResult:
    """Read live state, compute NAV/weights/drift, write a snapshot, flag drift breach.

    ``target_weights`` is the book to measure drift against (e.g.
    :func:`last_target_weights`); when ``None`` (before the first rebalance) drift is
    ``None``. A breach logs a warning and alerts; acting on it is the scheduler's job.
    """
    acct = client.account()
    nav, cash = acct.get("equity"), acct.get("cash")
    positions = client.all_positions()
    weights = compute_weights(positions, nav or 0.0)
    pos_qty = {p["symbol"]: float(p["qty"]) for p in positions}

    drift: Optional[float] = None
    breach = False
    if target_weights is not None:
        drift = l1_drift(weights, target_weights)
        if drift_threshold is not None and drift > drift_threshold:
            breach = True
            log.warning("drift breach — flag out-of-cycle rebalance",
                        extra={"drift": drift, "threshold": drift_threshold})
            if alert:
                alert(f"L1 drift {drift:.3f} > {drift_threshold:.3f}; flag rebalance")

    if db_engine is not None:
        _write_snapshot(db_engine, nav, cash, weights, pos_qty, drift)
    log.info("monitor pass", extra={"nav": nav, "n_positions": len(pos_qty), "drift": drift})
    return MonitorResult(nav, cash, weights, pos_qty, drift, breach)


def _write_snapshot(db_engine, nav, cash, weights: pd.Series, positions: dict, drift) -> None:
    from sqlalchemy import insert
    from engine.db import snapshots
    with db_engine.begin() as conn:
        conn.execute(insert(snapshots).values(
            ts=datetime.now(timezone.utc), nav=nav, cash=cash,
            weights={k: float(v) for k, v in weights.items()},
            positions=positions, drift=drift))
