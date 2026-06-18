"""PostgreSQL operational store — schema and connection factory.

Defines the eight operational tables from ARCHITECTURE.md as SQLAlchemy Core
tables on a shared :data:`metadata` object. ``scripts/init_db.py`` calls
:func:`create_all` to materialize them; engine modules use :func:`get_engine`
to obtain a connection.

The Parquet store (prices + fundamentals) is the *research* data layer and is
intentionally NOT here — this module is only the operational state Postgres
holds (orders, fills, snapshots, lifecycle, audit). Alpaca remains source of
truth; these tables are reconciled to it at every startup (D13).

Schema is defined with portable column types (JSON, not JSONB) so the exact
same definitions create cleanly on sqlite for tests and on Postgres in prod.

The schema is **provisional** (decided): columns beyond the ARCHITECTURE prose
are filled in here as a v1 and refined when each consumer module is built —
orders/fills in Phase 3, options_lifecycle in Phase 4, etc. Re-creating is cheap
pre-go-live, so we don't over-specify ahead of the modules that use these tables.
"""

from __future__ import annotations

import os

from sqlalchemy import (
    JSON,
    Boolean,
    Column,
    Date,
    DateTime,
    Float,
    Integer,
    MetaData,
    String,
    Table,
    create_engine,
    false,
    func,
)
from sqlalchemy.engine import Engine

metadata = MetaData()


# Every submitted order (placement happens in execute.py; this is the ledger).
orders = Table(
    "orders",
    metadata,
    Column("id", String, primary_key=True),  # Alpaca order id
    Column("client_order_id", String, index=True),
    Column("rebalance_cycle", String, index=True),  # idempotency key per cycle
    Column("symbol", String, nullable=False, index=True),
    Column("side", String, nullable=False),
    Column("qty", Float),
    Column("order_type", String),
    Column("status", String, index=True),
    Column("limit_price", Float),
    Column("filled_qty", Float),
    Column("filled_avg_price", Float),
    Column("submitted_at", DateTime),
    Column("filled_at", DateTime),
    Column("created_at", DateTime, server_default=func.now()),
)

# Fill confirmations from Alpaca.
fills = Table(
    "fills",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("order_id", String, index=True),
    Column("symbol", String, nullable=False, index=True),
    Column("qty", Float),
    Column("price", Float),
    Column("filled_at", DateTime),
    Column("created_at", DateTime, server_default=func.now()),
)

# Portfolio snapshots written by monitor.py for dashboard + reconciliation.
snapshots = Table(
    "snapshots",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("ts", DateTime, nullable=False, index=True),
    Column("nav", Float),
    Column("cash", Float),
    Column("weights", JSON),       # {symbol: weight}
    Column("positions", JSON),     # {symbol: qty}
    Column("drift", Float),
    Column("created_at", DateTime, server_default=func.now()),
)

# One row per rebalance: why it fired, the target, and the risk-gate result.
rebalance_log = Table(
    "rebalance_log",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("ts", DateTime, nullable=False, index=True),
    Column("trigger_reason", String),   # "monthly" | "drift"
    Column("target_weights", JSON),
    Column("risk_gate_passed", Boolean),
    Column("risk_gate_reason", String),
    Column("created_at", DateTime, server_default=func.now()),
)

# Sub-threshold deltas that accumulate and roll into the next rebalance cycle.
pending_adjustments = Table(
    "pending_adjustments",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("symbol", String, nullable=False, index=True),
    Column("side", String),
    Column("delta_usd", Float),
    Column("qty", Float),
    Column("reason", String),
    Column("applied", Boolean, server_default=false()),
    Column("created_at", DateTime, server_default=func.now()),
)

# Alert history (what was sent and whether delivery succeeded).
alerts = Table(
    "alerts",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("ts", DateTime, nullable=False, index=True),
    Column("alert_type", String, nullable=False, index=True),
    Column("message", String),
    Column("delivered", Boolean, server_default=false()),
    Column("created_at", DateTime, server_default=func.now()),
)

# Covered call lifecycle events: write / roll / assignment / force-close / close.
options_lifecycle = Table(
    "options_lifecycle",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("ts", DateTime, nullable=False, index=True),
    Column("event_type", String, nullable=False, index=True),
    Column("underlying", String, index=True),
    Column("option_symbol", String),
    Column("strike", Float),
    Column("expiration", Date),
    Column("delta", Float),
    Column("contracts", Integer),
    Column("premium", Float),      # +collected on write, -paid on close
    Column("created_at", DateTime, server_default=func.now()),
)

# Daily composite + sub-scores per ticker (dashboard + audit trail).
factor_scores = Table(
    "factor_scores",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("date", Date, nullable=False, index=True),
    Column("symbol", String, nullable=False, index=True),
    Column("composite_score", Float),
    Column("quality_score", Float),
    Column("value_score", Float),
    Column("momentum_score", Float),
    Column("lowvol_score", Float),
    Column("stale", Boolean, server_default=false()),
    Column("created_at", DateTime, server_default=func.now()),
)


def get_engine(database_url: str | None = None) -> Engine:
    """Return a SQLAlchemy Engine for ``database_url`` (or ``DATABASE_URL`` env).

    Raises:
        RuntimeError: If no URL is provided and ``DATABASE_URL`` is unset.
    """
    url = database_url or os.environ.get("DATABASE_URL", "").strip()
    if not url:
        raise RuntimeError(
            "DATABASE_URL is not set (load .env.paper / .env.live first) "
            "and no database_url was passed"
        )
    return create_engine(url, future=True)


def create_all(engine: Engine) -> list[str]:
    """Create every table that does not already exist. Returns table names."""
    metadata.create_all(engine)
    return list(metadata.tables.keys())
