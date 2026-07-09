"""Run a config-defined strategy on a NON-primary account — the on-the-fly-strategy testbed CLI.

Authors a :class:`~engine.config_strategy.StrategySpec` from the command line, wraps it in a
``ConfigStrategy``, and runs it on the given account via :func:`engine.account_runner
.run_strategy_on_account` (isolated to that account's creds; the primary book is never touched).

    # preview what it WOULD trade — no orders sent:
    python scripts/run_config_strategy.py --env paper --account trend \
        --signals low_vol=0.5,low_beta=0.5 --max-names 15 --leverage 1.0 --dry-run

    # actually trade the test account (places paper orders on THAT account):
    python scripts/run_config_strategy.py --env paper --account trend \
        --signals low_vol=0.5,low_beta=0.5 --max-names 15 --leverage 1.0

``--signals`` is a comma list of ``name=weight`` from the signal library (quality, value, low_beta,
low_vol). Start with ``--dry-run`` to see the sized order plan before committing.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from engine import account_runner, config_strategy, signals  # noqa: E402
from engine.config import load_settings  # noqa: E402
from engine.logger import get_logger  # noqa: E402

log = get_logger(__name__)


def _parse_signals(spec: str) -> dict[str, float]:
    out: dict[str, float] = {}
    for part in (spec or "").split(","):
        part = part.strip()
        if not part:
            continue
        name, _, weight = part.partition("=")
        name = name.strip()
        signals.register_builtins()
        if name not in signals.all_signals():
            raise SystemExit(f"unknown signal {name!r}; available: {', '.join(sorted(signals.all_signals()))}")
        out[name] = float(weight) if weight.strip() else 1.0
    if not out:
        raise SystemExit("--signals is required, e.g. --signals low_vol=0.5,low_beta=0.5")
    return out


def main() -> None:
    from engine import config, db

    ap = argparse.ArgumentParser(description="Run a config strategy on a non-primary account")
    ap.add_argument("--env", choices=("paper", "live"), required=True)
    ap.add_argument("--account", required=True, help="credstore account slug (NOT 'primary')")
    ap.add_argument("--signals", required=True, help="comma list name=weight (e.g. low_vol=0.5,low_beta=0.5)")
    ap.add_argument("--name", default=None, help="strategy name (default: cfg-<account>)")
    ap.add_argument("--max-names", type=int, default=20)
    ap.add_argument("--max-weight", type=float, default=0.05)
    ap.add_argument("--leverage", type=float, default=1.0)
    ap.add_argument("--min-score", type=float, default=0.0)
    ap.add_argument("--mode", choices=("normal", "express"), default="normal")
    ap.add_argument("--dry-run", action="store_true", help="print the sized plan; trade nothing")
    args = ap.parse_args()

    signals.register_builtins()
    sig_weights = _parse_signals(args.signals)

    config.load_env(args.env)
    settings = load_settings()
    db_engine = db.get_engine()

    spec = config_strategy.StrategySpec(
        name=args.name or f"cfg-{args.account}", signals=sig_weights, max_names=args.max_names,
        max_weight=args.max_weight, leverage=args.leverage, min_score=args.min_score)
    strat = config_strategy.ConfigStrategy(spec)

    result = account_runner.run_strategy_on_account(
        args.account, strat, db_engine=db_engine, settings=settings,
        mode=args.mode, dry_run=args.dry_run)
    print(json.dumps(result, default=str, indent=2))


if __name__ == "__main__":
    main()
