"""Create the sharpe-engine PostgreSQL schema.

One-time (idempotent) migration: creates every operational table defined in
``engine/db.py`` if it does not already exist. Reads ``DATABASE_URL`` from the
chosen environment file, or from an explicit ``--database-url`` override.

    python scripts/init_db.py --env paper
    python scripts/init_db.py --database-url sqlite:///./dev.db   # local check

``--drop`` drops all tables first (destructive; for dev resets only).
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Allow running as a plain script (python scripts/init_db.py).
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from engine import config, db  # noqa: E402
from engine.logger import get_logger  # noqa: E402

log = get_logger("init_db")


def main() -> None:
    parser = argparse.ArgumentParser(description="Initialize sharpe-engine database")
    parser.add_argument("--env", choices=("paper", "live"), default="paper")
    parser.add_argument("--database-url", default=None, help="Override DATABASE_URL")
    parser.add_argument("--drop", action="store_true", help="DROP all tables first (destructive)")
    args = parser.parse_args()

    if args.database_url is None:
        config.load_env(args.env)
    engine = db.get_engine(args.database_url)

    if args.drop:
        log.warning("dropping all tables", extra={"url": str(engine.url)})
        db.metadata.drop_all(engine)

    created = db.create_all(engine)
    log.info("schema ready", extra={"tables": created, "url": str(engine.url)})
    print(f"Created/verified {len(created)} tables: {', '.join(sorted(created))}")


if __name__ == "__main__":
    main()
