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
    ap.add_argument("--no-monitor", action="store_true",
                    help="disable the built-in Alpaca monitor + live orders (Postgres-only)")
    ap.add_argument("--monitor-interval", type=int, default=60,
                    help="seconds between background snapshot refreshes (default 60)")
    args = ap.parse_args()

    # load_env brings in DATABASE_URL *and* the Alpaca keys: the dashboard now self-updates
    # (background monitor → snapshots) + serves live Alpaca orders, so it stays live without run_eod.
    config.load_env(args.env)
    app = create_app(env=args.env, live=not args.no_monitor, monitor_interval=args.monitor_interval)
    live = "Postgres-only" if args.no_monitor else f"live (monitor every {args.monitor_interval}s + live orders)"
    print(f"sharpe-engine dashboard on http://{args.host}:{args.port} (env={args.env}, {live})")
    uvicorn.run(app, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
