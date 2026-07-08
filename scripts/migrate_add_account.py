"""Add the tracked-sleeves ``account`` column to existing tables (ADR-001 Phase B).

``create_all`` only creates missing TABLES, never new COLUMNS on an existing table — so a live
Postgres needs this one-time ALTER. Idempotent (skips columns that already exist) and safe: adding
a NOT NULL column with a server default backfills every existing row to ``primary`` (the
engine-traded book) in one statement, so no data moves and trading is untouched.

    ./.venv/bin/python scripts/migrate_add_account.py --env paper

sqlite (the test DBs) already gets the column from ``create_all`` — this script is Postgres-only
and no-ops elsewhere.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from engine import config, db  # noqa: E402
from engine.logger import get_logger  # noqa: E402

log = get_logger(__name__)

# (table, column, DDL type + default) — the tracked-sleeves account tag. snapshots first (it drives
# the NAV curve → Overview / track record / risk). Others follow when trading is per-account.
_ADDITIONS = [
    ("snapshots", "account", "VARCHAR NOT NULL DEFAULT 'primary'"),
]


def _column_exists(conn, table: str, column: str) -> bool:
    from sqlalchemy import text
    row = conn.execute(text(
        "SELECT 1 FROM information_schema.columns WHERE table_name = :t AND column_name = :c"
    ), {"t": table, "c": column}).first()
    return row is not None


def main() -> None:
    ap = argparse.ArgumentParser(description="Add the tracked-sleeves account column")
    ap.add_argument("--env", choices=("paper", "live"), default="paper")
    args = ap.parse_args()
    config.load_env(args.env)
    engine = db.get_engine()

    if engine.dialect.name != "postgresql":
        print(f"dialect {engine.dialect.name!r} — create_all already includes the column; nothing to do")
        return

    from sqlalchemy import text
    added, skipped = [], []
    with engine.begin() as conn:
        for table, column, ddl in _ADDITIONS:
            if _column_exists(conn, table, column):
                skipped.append(f"{table}.{column}")
                continue
            conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}"))
            added.append(f"{table}.{column}")
    if added:
        print(f"added: {', '.join(added)} (existing rows backfilled to 'primary')")
        log.warning("account-column migration applied", extra={"added": added})
    if skipped:
        print(f"already present (skipped): {', '.join(skipped)}")
    print("migration complete")


if __name__ == "__main__":
    main()
