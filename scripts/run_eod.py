"""End-of-day rebalance driver (BUILD_ORDER Phase 3).

Wires the equity pipeline into one cycle:

    reconcile → holiday gate → compute targets → risk gate → execute → monitor

This is increment 3.6 — the **single-cycle** driver (``--once``), the thing the Phase 3
gate exercises ("first paper rebalance executes"). The continuous APScheduler wrapper
(timed jobs, SIGTERM, the 60s monitor loop) is 3.7. Covered-call jobs are Phase 4.

Design: :func:`run_cycle` is a thin orchestrator whose data-heavy and broker steps are
**injectable** (``targets_fn``, ``trading_day_fn``), so the integration test drives the
full wiring with a fake client / broker / sqlite and a stubbed target producer — no
network, no live data. :func:`main` builds the real dependencies and runs one cycle.

Usage::

    python scripts/run_eod.py --once --env paper
    python scripts/run_eod.py --once --env paper --date 2026-07-01 --skip-ingest
"""

from __future__ import annotations

import argparse
import sys
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Callable, Optional

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from engine import covariance, factors, ingest, monitor, optimize, reconcile, risk, sectors  # noqa: E402
from engine.config import load_settings  # noqa: E402
from engine.execute import ExecReport, plan_orders, submit_and_track  # noqa: E402
from engine.logger import get_logger  # noqa: E402
from engine.risk import RiskCheckResult  # noqa: E402
from scripts import backtest as bt  # safe_covariance + dir constants  # noqa: E402

log = get_logger(__name__)

PRICES_DIR = bt.PRICES_DIR
FUNDAMENTALS_DIR = bt.FUNDAMENTALS_DIR


@dataclass
class TargetPlan:
    """Optimizer output plus the per-name inputs the rest of the cycle needs."""
    weights: pd.Series
    prices: dict[str, float]
    adv: dict[str, float] = field(default_factory=dict)
    spread: dict[str, float] = field(default_factory=dict)
    universe: set = field(default_factory=set)
    sector_map: pd.Series = field(default_factory=lambda: pd.Series(dtype=object))


@dataclass
class CycleResult:
    """Outcome of one rebalance cycle."""
    status: str  # "executed" | "blocked_risk" | "not_trading_day" | "no_targets"
    target_weights: pd.Series = field(default_factory=lambda: pd.Series(dtype=float))
    risk: Optional[RiskCheckResult] = None
    exec_report: Optional[ExecReport] = None
    monitor: Optional[monitor.MonitorResult] = None


# ====================================================================== #
# Target production (the score → covariance → optimize path, at as_of=today)
# ====================================================================== #
def compute_targets(settings, as_of: date, *, db_engine=None,
                    prices_dir: str = PRICES_DIR, fundamentals_dir: str = FUNDAMENTALS_DIR) -> TargetPlan:
    """Build target weights for ``as_of`` exactly like a backtest rebalance, on live data.

    Uses the FF5 staleness guard (``max_stale_days``) so a publication lag refreshes the
    cache rather than starving the covariance window (audit fix). Per-name execution
    inputs (price / ADV / spread) come from the ``as_of`` equities snapshot.
    """
    panel = factors.load_close_panel(prices_dir, end=as_of, lookback=10**9)
    allf, eligible = factors.load_scored_fundamentals(fundamentals_dir, settings)
    ff5 = covariance.load_ff5_daily(max_stale_days=7)
    sector_map = sectors.load_sector_map()["sector"]

    sc = factors.score_date(as_of, settings=settings, price_panel=panel,
                            all_fundamentals=allf, eligible_symbols=eligible).set_index("symbol")
    composite = sc["composite_score"].dropna()
    if composite.empty:
        return TargetPlan(pd.Series(dtype=float), {}, universe=set(), sector_map=sector_map)

    top = composite.sort_values(ascending=False).head(settings.optimizer.preselect_top_k).index
    sigma = bt.safe_covariance(panel[top], ff5, as_of=as_of,
                               window=settings.covariance.estimation_window_days,
                               min_obs=settings.covariance.min_regression_obs)
    prev = monitor.last_target_weights(db_engine) if db_engine is not None else None
    prev_w = pd.Series(prev, dtype=float) if prev else None
    res = optimize.optimize_portfolio(composite, sigma, sector_map, settings=settings, prev_weights=prev_w)

    snap = ingest.load_equities(as_of.isoformat(), Path(prices_dir))
    prices = snap["close"].to_dict() if "close" in snap else {}
    adv = snap["adv_20d"].to_dict() if "adv_20d" in snap else {}
    spread = snap["spread"].to_dict() if "spread" in snap else {}
    universe = set(eligible) if eligible is not None else set(composite.index)
    return TargetPlan(res.weights, prices, adv, spread, universe, sector_map)


# ====================================================================== #
# Orchestrator
# ====================================================================== #
def run_cycle(
    *,
    client,
    broker,
    db_engine,
    settings,
    as_of: date,
    force: bool = False,
    trigger: str = "monthly",
    targets_fn: Callable[..., TargetPlan] = compute_targets,
    trading_day_fn: Callable[[object, str], bool] = ingest.is_trading_day,
    alert: Callable[[str], None] | None = None,
) -> CycleResult:
    """Run one equity rebalance cycle. Returns a :class:`CycleResult`.

    Order: reconcile (block if Alpaca down) → holiday gate (unless ``force``) → compute
    targets → pre-trade risk gate (logged to ``rebalance_log``; blocks submission on
    failure) → plan + submit + track orders → monitor snapshot.
    """
    rec = reconcile.reconcile(client, db_engine, alert=alert)            # raises if Alpaca down

    if not force and not trading_day_fn(client, as_of.isoformat()):
        log.info("not a trading day; skipping cycle", extra={"date": as_of.isoformat()})
        return CycleResult("not_trading_day")

    plan = targets_fn(settings, as_of, db_engine=db_engine)
    weights = plan.weights
    if weights is None or weights.empty:
        log.warning("no target weights produced; nothing to do")
        return CycleResult("no_targets")

    rc = risk.check_pretrade(weights, settings=settings, sector_map=plan.sector_map,
                             universe=plan.universe, equity_positions=rec.live_positions, as_of=as_of)
    _write_rebalance_log(db_engine, as_of, weights, rc, trigger)
    if not rc.approved:
        log.error("risk gate blocked the cycle; no orders submitted", extra={"reason": rc.reason})
        if alert:
            alert(f"risk gate blocked rebalance: {rc.reason}")
        return CycleResult("blocked_risk", weights, rc)

    nav = float(client.account().get("equity") or settings.portfolio.nav)
    orders, pending = plan_orders(weights, rec.live_positions, plan.prices, nav=nav,
                                  settings=settings, adv=plan.adv, spread=plan.spread)
    report = submit_and_track(orders, broker=broker, db_engine=db_engine,
                              cycle_key=as_of.isoformat(), pending=pending, alert=alert)

    mon = monitor.monitor_once(client, db_engine, target_weights=weights.to_dict())
    log.info("cycle executed", extra={"date": as_of.isoformat(), **vars(report)})
    return CycleResult("executed", weights, rc, report, mon)


def _write_rebalance_log(db_engine, as_of: date, weights: pd.Series, rc: RiskCheckResult,
                         trigger: str) -> None:
    if db_engine is None:
        return
    from sqlalchemy import insert
    from engine.db import rebalance_log
    with db_engine.begin() as conn:
        conn.execute(insert(rebalance_log).values(
            ts=datetime.now(timezone.utc), trigger_reason=trigger,
            target_weights={k: float(v) for k, v in weights.items()},
            risk_gate_passed=rc.approved, risk_gate_reason=rc.reason))


# ====================================================================== #
# CLI
# ====================================================================== #
def _countdown(seconds: int) -> None:
    print(f"LIVE trading in {seconds}s — Ctrl-C to abort", file=sys.stderr)
    for s in range(seconds, 0, -1):
        print(f"  {s}…", file=sys.stderr)
        time.sleep(1)


def main() -> None:
    from engine import config, db
    from engine.broker import Broker
    from engine.config import require_env

    ap = argparse.ArgumentParser(description="End-of-day rebalance driver (Phase 3, single cycle)")
    ap.add_argument("--once", action="store_true", required=True,
                    help="run exactly one rebalance cycle (the only mode in 3.6)")
    ap.add_argument("--env", choices=("paper", "live"), required=True)
    ap.add_argument("--date", type=lambda s: date.fromisoformat(s), default=date.today())
    ap.add_argument("--force", action="store_true", help="run even if not a trading day")
    ap.add_argument("--skip-ingest", action="store_true", help="reuse existing snapshot data")
    args = ap.parse_args()

    config.load_env(args.env)
    if args.env == "live":
        _countdown(5)
    settings = load_settings()
    client = config.get_alpaca_client()
    broker = Broker(require_env("ALPACA_API_KEY"), require_env("ALPACA_SECRET_KEY"))
    db_engine = db.get_engine()

    if not args.skip_ingest:
        ingest.run_daily_ingest(env=args.env, as_of=args.date.isoformat())

    result = run_cycle(client=client, broker=broker, db_engine=db_engine,
                       settings=settings, as_of=args.date, force=args.force)
    print(f"\nCycle {args.date} → {result.status}")
    if result.exec_report:
        r = result.exec_report
        print(f"  submitted {r.submitted}  filled {r.filled}  partial {r.partial}  "
              f"rejected {r.rejected}  deferred {r.deferred}")
    if result.monitor and result.monitor.drift is not None:
        print(f"  NAV {result.monitor.nav}  drift {result.monitor.drift:.3f}")


if __name__ == "__main__":
    main()
