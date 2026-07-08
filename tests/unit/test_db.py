"""Unit tests for engine.db — the operational schema must materialize cleanly.

db.py claims portability (same definitions create on sqlite for tests and on
Postgres in prod). This exercises create_all on in-memory sqlite, which catches
dialect-specific DDL mistakes (e.g. a server_default that renders as an invalid
function call) before they reach a real database.
"""

from __future__ import annotations

from sqlalchemy import create_engine, inspect

from engine import db

EXPECTED_TABLES = {
    "orders",
    "fills",
    "snapshots",
    "rebalance_log",
    "pending_adjustments",
    "alerts",
    "options_lifecycle",
    "factor_scores",
    "order_events",
    "manual_actions",
    "overrides",
    "account_credentials",
}


def test_create_all_materializes_every_table_on_sqlite():
    engine = create_engine("sqlite://")  # in-memory
    names = db.create_all(engine)
    assert set(names) == EXPECTED_TABLES
    # The tables actually exist in the database, not just on the metadata object.
    assert set(inspect(engine).get_table_names()) == EXPECTED_TABLES


def test_create_all_is_idempotent():
    engine = create_engine("sqlite://")
    db.create_all(engine)
    db.create_all(engine)  # second run must not raise (checkfirst)
    assert set(inspect(engine).get_table_names()) == EXPECTED_TABLES
