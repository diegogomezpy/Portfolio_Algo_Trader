"""Position reconciliation against Alpaca (Phase 0 stub).

At every pipeline startup, PostgreSQL is reconciled to Alpaca's live positions —
Alpaca is always source of truth (D13), and the pipeline blocks entirely if
Alpaca is unreachable.

This is the Phase 0 placeholder: the module must exist and the read path must
work, but there are no positions yet, so it only fetches and reports. The full
implementation — diffing Alpaca qty vs the PostgreSQL ``snapshots`` table,
writing corrections, and alerting on divergence — lands in Phase 3 (see
BUILD_ORDER), at which point ``divergence_threshold`` and DB writes are wired in.
"""

from __future__ import annotations

from engine.logger import get_logger

log = get_logger(__name__)


def fetch_live_positions(client) -> dict[str, float]:
    """Return ``{symbol: qty}`` of current Alpaca positions (signed; long-only ⇒ ≥0).

    Raises:
        AlpacaError: If Alpaca is unreachable — the caller must block the
            pipeline rather than proceed on stale state (D13).
    """
    positions = client.all_positions()
    return {p["symbol"]: p["qty"] for p in positions}


def reconcile(client, db_engine=None, divergence_threshold: float = 0.0) -> dict[str, float]:
    """Phase 0 reconciliation: fetch and log live positions.

    Args:
        client: An :class:`AlpacaClient`.
        db_engine: SQLAlchemy engine (unused until Phase 3).
        divergence_threshold: Reserved for Phase 3 qty-divergence comparison.

    Returns:
        The live ``{symbol: qty}`` map.
    """
    live = fetch_live_positions(client)
    log.info("reconcile (stub): fetched live positions", extra={"positions": len(live)})
    # Phase 3: compare to snapshots, write corrections to DB, alert on divergence.
    return live
