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
    LargeBinary,
    MetaData,
    String,
    Table,
    create_engine,
    false,
    func,
    true,
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
    # Which account this snapshot belongs to (tracked-sleeves, ADR-001 Phase B). The engine-traded
    # book is "primary"; other accounts use their credstore short-id. server_default makes existing
    # rows AND untagged writers (eod's monitor tick) fall to "primary" automatically — so no eod
    # code change or restart is needed to keep the primary book correct.
    Column("account", String, nullable=False, server_default="primary", index=True),
    Column("nav", Float),
    Column("cash", Float),
    Column("last_equity", Float),  # Alpaca prior-trading-day close equity → true intraday P&L basis
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
    # {"portfolio_vol": float, "contrib": {sym: pct}, "weight": {sym: w}} — Euler risk
    # decomposition at rebalance (engine.optimize.risk_contributions). Nullable: pre-migration
    # rows and cycles where Σ was unavailable carry NULL, and the dashboard degrades gracefully.
    Column("risk_contributions", JSON),
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

# Per-order chase telemetry (execution visualizer Phase 2). One row each time the tiered executor
# posts a child limit for a name in a round, and one at round-end with the settle outcome — so the
# dashboard can replay each limit walking the bid→ask spread. Pure telemetry: writes are best-effort
# and failure-isolated in engine.execute (they must never affect order placement or fills).
order_events = Table(
    "order_events",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("ts", DateTime, nullable=False, index=True),
    Column("cycle_key", String, index=True),       # the rebalance cycle (== orders.rebalance_cycle)
    Column("round", String),                       # "r1", "r2", … (or "auction")
    Column("symbol", String, index=True),
    Column("side", String),                        # buy / sell
    Column("event", String),                       # post | settle | reject
    Column("tier", String),                        # deep | moderate | thin | pathological
    Column("bid", Float),
    Column("ask", Float),
    Column("mid", Float),
    Column("limit_price", Float),                  # where the child limit sat on the spread
    Column("qty", Integer),                        # shares posted this round (child slice for thin)
    Column("filled_qty", Integer),                 # cumulative filled for the name after this event
    Column("target_qty", Integer),                 # the name's full order size (for progress %)
    Column("status", String),                      # broker status at settle (filled/canceled/…)
    Column("order_id", String),
    Column("created_at", DateTime, server_default=func.now()),
)

# Manual actions fired from the dashboard's execution console (audit trail). One row per
# button press that reached the engine: what was asked, how it ran, and how it ended.
manual_actions = Table(
    "manual_actions",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("ts", DateTime, nullable=False, index=True),
    Column("action", String, nullable=False, index=True),  # rebalance|liquidate|trade|leverage|cancel_all
    Column("mode", String),                                # normal | express
    Column("params", JSON),                                # {"pct":25} / {"symbol","side","usd","pct"} / {"target":1.5}
    Column("status", String, index=True),                  # started | done | failed
    Column("cycle_key", String, index=True),               # == orders.rebalance_cycle / order_events.cycle_key
    Column("result", JSON),                                # summary written on completion (fills, notional, error)
    Column("created_at", DateTime, server_default=func.now()),
)

# Operator overrides — a tiny key/value store the engine consults before settings.yaml.
# Currently: "target_leverage" (the dashboard's sticky leverage rebalance).
overrides = Table(
    "overrides",
    metadata,
    Column("key", String, primary_key=True),
    Column("value", JSON),
    Column("updated_at", DateTime),
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
    Column("beta_score", Float),
    Column("lowvol_score", Float),
    Column("stale", Boolean, server_default=false()),
    Column("created_at", DateTime, server_default=func.now()),
)


# Per-account broker credentials, dashboard-managed (ADR-001 / D37 Phase C). The key + secret
# are Fernet-encrypted into ``ciphertext`` (engine.credstore) — NEVER stored in cleartext, and
# the encryption key lives in a file/env off the DB, so a leaked pg_dump reveals nothing. Only
# non-secret metadata (label, base_url, a masked key fingerprint, sizing) is in the clear.
account_credentials = Table(
    "account_credentials",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("slug", String, nullable=False, unique=True, index=True),   # short id, e.g. "trend"
    Column("label", String),                                           # human label for the UI
    Column("base_url", String),                                        # paper/live API base
    Column("ciphertext", LargeBinary, nullable=False),                 # Fernet({api_key, api_secret})
    Column("key_fingerprint", String),                                 # masked api-key id, safe to show
    Column("capital", Float),                                          # deployable base for this account
    Column("leverage", Float),
    Column("enabled", Boolean, server_default=true()),
    Column("created_at", DateTime, server_default=func.now()),
    Column("updated_at", DateTime),
)


# Per-account on-the-fly strategy spec (ADR-001 / D37 north star — the dashboard builder). One
# active spec per account (slug): the serialized StrategySpec (signals/formula + all construction,
# universe, factor and overlay params) plus its rebalance cadence + auto-run flag the scheduler
# reads. No secrets here — creds stay in account_credentials.
strategy_specs = Table(
    "strategy_specs",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("account", String, nullable=False, unique=True, index=True),   # credstore slug
    Column("name", String),
    Column("spec", JSON, nullable=False),                  # serialized StrategySpec (config_strategy)
    Column("rebalance_frequency", String),                 # monthly | weekly | daily
    Column("mode", String),                                # normal | express
    Column("auto_enabled", Boolean, server_default=false()),   # scheduler trades it when true
    Column("last_run", Date),                              # last date the scheduler ran it (idempotency)
    Column("created_at", DateTime, server_default=func.now()),
    Column("updated_at", DateTime),
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
    connect_args: dict = {}
    if url.startswith("postgresql"):
        # Pin the session to UTC. Every writer uses datetime.now(timezone.utc); without this,
        # Postgres converts those aware-UTC values to the server's local timezone when storing
        # them in the naive TIMESTAMP columns, so they come back shifted. Readers (the dashboard
        # age/staleness math, reconciliation against Alpaca's UTC timestamps) treat stored values
        # as UTC, so the session must be UTC for stored == UTC wall-clock.
        connect_args["options"] = "-c timezone=utc"
    return create_engine(url, future=True, connect_args=connect_args)


def create_all(engine: Engine) -> list[str]:
    """Create every table that does not already exist. Returns table names."""
    metadata.create_all(engine)
    return list(metadata.tables.keys())
