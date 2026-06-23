"""Live dashboard process (Phase 5.2) — the second terminal alongside run_eod.

Postgres-only: needs ``DATABASE_URL`` (loaded from ``.env.<env>``); it does not use Alpaca
credentials. Serves the dashboard at ``http://host:port``.

Usage::

    python scripts/run_dashboard.py --env paper
    python scripts/run_dashboard.py --env paper --host 0.0.0.0 --port 8000
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def main() -> None:
    import uvicorn

    from dashboard.app import create_app
    from engine import config

    ap = argparse.ArgumentParser(description="sharpe-engine live dashboard")
    ap.add_argument("--env", choices=("paper", "live"), default="paper")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8000)
    args = ap.parse_args()

    config.load_env(args.env)          # loads DATABASE_URL (no Alpaca creds needed)
    app = create_app(env=args.env)     # env drives the PAPER/LIVE badge + config meta
    print(f"sharpe-engine dashboard on http://{args.host}:{args.port} (env={args.env})")
    uvicorn.run(app, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
