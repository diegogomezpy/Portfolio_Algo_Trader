"""CLI for the dashboard's manual execution actions (engine.manual_exec).

Spawned by the dashboard's execution console; also usable from a terminal:

    python scripts/manual_exec.py --env paper --action liquidate --pct 25 --mode normal
    python scripts/manual_exec.py --env paper --action trade --symbol XOM --side sell --pct 50
    python scripts/manual_exec.py --env paper --action trade --symbol AAPL --side buy --usd 5000
    python scripts/manual_exec.py --env paper --action leverage --target 1.5 --mode express
    python scripts/manual_exec.py --env paper --action liquidate --pct 10 --preview

``--preview`` prints the order plan as JSON and exits without trading. Exit code 0 on a
completed action, 1 on refusal/failure (message on stderr) — the dashboard reads both.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from engine import alerts, manual_exec  # noqa: E402
from engine.config import load_settings, require_env  # noqa: E402
from engine.logger import get_logger  # noqa: E402

log = get_logger(__name__)


def main() -> None:
    from engine import config, db
    from engine.broker import Broker

    ap = argparse.ArgumentParser(description="Manual execution console actions")
    ap.add_argument("--env", choices=("paper", "live"), required=True)
    ap.add_argument("--action", choices=manual_exec.ACTIONS, required=True)
    ap.add_argument("--mode", choices=manual_exec.MODES, default="normal")
    ap.add_argument("--pct", type=float, help="liquidate: %% of book; trade sell: %% of position")
    ap.add_argument("--symbol", help="trade: the equity symbol")
    ap.add_argument("--side", choices=("buy", "sell"), help="trade: buy or sell")
    ap.add_argument("--usd", type=float, help="trade buy: dollar notional")
    ap.add_argument("--target", type=float, help="leverage: target gross (e.g. 1.5)")
    ap.add_argument("--cycle-key", help="override the run's cycle key (the dashboard sets this)")
    ap.add_argument("--preview", action="store_true", help="print the plan as JSON; trade nothing")
    ap.add_argument("--force", action="store_true",
                    help="override the market-closed and execution-in-flight guards")
    args = ap.parse_args()

    config.load_env(args.env)
    settings = load_settings()
    client = config.get_alpaca_client()
    db_engine = db.get_engine()
    params = {k: v for k, v in (("pct", args.pct), ("symbol", args.symbol), ("side", args.side),
                                ("usd", args.usd), ("target", args.target)) if v is not None}

    if args.preview:
        plan = manual_exec.build_plan(args.action, client, db_engine, settings, **params)
        print(json.dumps(plan, default=str))
        return

    broker = Broker(require_env("ALPACA_API_KEY"), require_env("ALPACA_SECRET_KEY"))
    _a = settings.alerts
    _smtp = str(getattr(_a, "smtp_host", "") or "").strip()
    alerter = alerts.make_alerter(db_engine, settings,
                                  dry_run=not (bool(getattr(_a, "send_enabled", False))
                                               and _smtp not in ("", "TBD")))
    try:
        result = manual_exec.run_action(args.action, mode=args.mode, client=client, broker=broker,
                                        db_engine=db_engine, settings=settings,
                                        cycle_key=args.cycle_key, alert=alerter,
                                        force=args.force, **params)
    except (ValueError, RuntimeError) as exc:
        print(json.dumps({"error": str(exc)}), file=sys.stderr)
        sys.exit(1)
    print(json.dumps(result, default=str))


if __name__ == "__main__":
    main()
