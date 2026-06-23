"""End-of-day rebalance driver (BUILD_ORDER Phase 3).

Wires the equity pipeline into one cycle:

    reconcile → holiday gate → compute targets → risk gate → execute → monitor

Two modes:

* ``--once`` (3.6) — run exactly one cycle now (what the Phase 3 gate exercises:
  "first paper rebalance executes").
* ``--serve`` (3.7) — the continuous APScheduler process: a daily 16:10-ET job that
  gates on the market calendar, ingests, then **branches** — a full rebalance on the
  first trading day of the month, otherwise a lightweight reconcile+monitor pass — plus a
  60-second monitor loop, plus a SIGTERM handler that finishes the current stage and
  cancels open orders before exiting 0.

Design: the orchestrators (:func:`run_cycle`, :func:`daily_job`) take **injectable**
data/broker/calendar steps, so the integration tests drive the full wiring with a fake
client / broker / sqlite and stubbed producers — no network, no live data. The thin parts
that can't be unit-tested (APScheduler timing, OS signals) live in :func:`serve` and are
verified by Diego against the live paper account. Covered-call jobs are Phase 4.

Usage::

    python scripts/run_eod.py --once  --env paper
    python scripts/run_eod.py --once  --env paper --date 2026-07-01 --skip-ingest
    python scripts/run_eod.py --serve --env paper
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

from engine import (  # noqa: E402
    covariance, covered_calls, factors, ingest, monitor, optimize, reconcile, risk, sectors,
)
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
    panel: Optional[pd.DataFrame] = None        # close panel, for the overlay's spot/IV


@dataclass
class CycleResult:
    """Outcome of one rebalance cycle."""
    status: str  # "executed" | "blocked_risk" | "not_trading_day" | "no_targets"
    target_weights: pd.Series = field(default_factory=lambda: pd.Series(dtype=float))
    risk: Optional[RiskCheckResult] = None
    exec_report: Optional[ExecReport] = None
    monitor: Optional[monitor.MonitorResult] = None
    calls_closed: int = 0
    calls_written: int = 0


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
        return TargetPlan(pd.Series(dtype=float), {}, universe=set(), sector_map=sector_map, panel=panel)

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
    return TargetPlan(res.weights, prices, adv, spread, universe, sector_map, panel=panel)


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
    overlay: bool = False,
    close_calls_fn: Callable[..., object] = covered_calls.close_calls,
    write_calls_fn: Callable[..., object] = covered_calls.write_calls,
    alert: Callable[[str], None] | None = None,
) -> CycleResult:
    """Run one rebalance cycle. Returns a :class:`CycleResult`.

    Order: reconcile (block if Alpaca down) → holiday gate (unless ``force``) → compute
    targets → pre-trade risk gate (logged to ``rebalance_log``; blocks on failure) →
    [overlay: close existing calls] → plan + submit + track equity orders → [overlay:
    write fresh calls on the post-trade ≥100-share holdings] → monitor snapshot.

    ``overlay`` (Phase 4) turns on the covered-call legs (close-all before equity, rewrite
    after — DECISIONS D31); the writes are coverage-safe by construction (``covered_calls``
    only writes ``floor(shares/100)`` contracts on shares actually held). ``close_calls_fn``
    / ``write_calls_fn`` are injectable for testing the sequencing.
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

    leverage = float(getattr(settings.portfolio, "target_leverage", 1.0))
    rc = risk.check_pretrade(weights, settings=settings, sector_map=plan.sector_map,
                             universe=plan.universe, equity_positions=rec.live_positions,
                             as_of=as_of, leverage=leverage)
    _write_rebalance_log(db_engine, as_of, weights, rc, trigger)
    if not rc.approved:
        log.error("risk gate blocked the cycle; no orders submitted", extra={"reason": rc.reason})
        if alert:
            alert(f"risk gate blocked rebalance: {rc.reason}")
        return CycleResult("blocked_risk", weights, rc)

    # Overlay (D31): close all existing calls BEFORE equity trades.
    n_closed = 0
    if overlay:
        closed = close_calls_fn(client, broker, db_engine, as_of=as_of, alert=alert)
        n_closed = len(closed or [])

    # Deployable base = leverage × account equity (DECISIONS D32). Weights are fractions of
    # this base, so the optimizer/caps are unchanged; only the dollar base scales.
    equity = float(client.account().get("equity") or settings.portfolio.nav)
    nav = equity * leverage
    orders, pending = plan_orders(weights, rec.live_positions, plan.prices, nav=nav,
                                  settings=settings, adv=plan.adv, spread=plan.spread)
    report = submit_and_track(orders, broker=broker, db_engine=db_engine,
                              cycle_key=as_of.isoformat(), pending=pending, alert=alert)

    # Overlay (D31): write fresh calls AFTER equity settles, on the shares actually held.
    n_written = 0
    if overlay:
        held = _equity_shares(client)
        written, _skipped = write_calls_fn(client, broker, db_engine, held,
                                           settings=settings, as_of=as_of,
                                           price_panel=plan.panel, alert=alert)
        n_written = len(written or [])

    mon = monitor.monitor_once(client, db_engine, target_weights=weights.to_dict())
    log.info("cycle executed", extra={"date": as_of.isoformat(), "closed": n_closed,
                                      "written": n_written, **vars(report)})
    return CycleResult("executed", weights, rc, report, mon,
                       calls_closed=n_closed, calls_written=n_written)


def _equity_shares(client) -> dict[str, float]:
    """Current equity positions ``{symbol: qty}`` (excludes options) — what calls cover."""
    out = {}
    for p in client.all_positions():
        if str(p.get("asset_class") or "us_equity").endswith("equity"):
            out[str(p["symbol"])] = float(p["qty"])
    return out


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
# Scheduler (3.7) — daily dispatch, continuous monitor, graceful shutdown
# ====================================================================== #
@dataclass
class DailyResult:
    """Outcome of one scheduled daily job."""
    status: str  # "rebalanced" | "monitored" | "not_trading_day"
    cycle: Optional[CycleResult] = None
    monitor: Optional[monitor.MonitorResult] = None


def is_first_trading_day_of_month(client, as_of: date) -> bool:
    """Whether ``as_of`` is the first NYSE trading day of its month (Alpaca calendar)."""
    start = as_of.replace(day=1)
    cal = client.market_calendar(start.isoformat(), as_of.isoformat())
    days = sorted(str(d.get("date"))[:10] for d in cal)
    return bool(days) and days[0] == as_of.isoformat()


def daily_job(
    *,
    client,
    broker,
    db_engine,
    settings,
    as_of: date,
    ingest_fn: Callable[[str], None] | None = None,
    targets_fn: Callable[..., TargetPlan] = compute_targets,
    trading_day_fn: Callable[[object, str], bool] = ingest.is_trading_day,
    first_trading_day_fn: Callable[[object, date], bool] | None = None,
    overlay: bool = False,
    alert: Callable[[str], None] | None = None,
) -> DailyResult:
    """The once-a-day scheduled job: gate on the calendar, ingest, then branch.

    On the **first trading day of the month** it runs the full rebalance cycle
    (:func:`run_cycle`, with the covered-call ``overlay`` when enabled); on any other
    trading day it does a lightweight reconcile + monitor pass (no trading). Non-trading
    days are skipped. ``ingest_fn`` (default ``None`` = skip, so tests never hit the
    network) refreshes the day's data first.
    """
    if not trading_day_fn(client, as_of.isoformat()):
        log.info("not a trading day; daily job skipped", extra={"date": as_of.isoformat()})
        return DailyResult("not_trading_day")

    if ingest_fn is not None:
        ingest_fn(as_of.isoformat())

    first_of_month = first_trading_day_fn or is_first_trading_day_of_month
    if first_of_month(client, as_of):
        cyc = run_cycle(client=client, broker=broker, db_engine=db_engine, settings=settings,
                        as_of=as_of, force=True, trigger="monthly", targets_fn=targets_fn,
                        trading_day_fn=trading_day_fn, overlay=overlay, alert=alert)
        return DailyResult("rebalanced", cycle=cyc)

    # Non-rebalance trading day: keep the DB honest and snapshot, but don't trade.
    reconcile.reconcile(client, db_engine, alert=alert)
    tgt = monitor.last_target_weights(db_engine)
    mon = monitor.monitor_once(client, db_engine, target_weights=tgt)
    log.info("daily monitor pass (no rebalance)", extra={"date": as_of.isoformat()})
    return DailyResult("monitored", monitor=mon)


def continuous_monitor_job(client, db_engine) -> None:
    """The 60s monitor tick: snapshot NAV/drift. A transient error must not kill the loop."""
    try:
        tgt = monitor.last_target_weights(db_engine)
        monitor.monitor_once(client, db_engine, target_weights=tgt)
    except Exception as exc:  # noqa: BLE001 — keep the scheduler alive across read hiccups
        log.error("monitor job error", extra={"error": str(exc)})


def graceful_shutdown(scheduler, broker) -> None:
    """SIGTERM contract (ARCHITECTURE): finish the current stage, then cancel open orders.

    Shuts the scheduler down with ``wait=True`` so an in-flight rebalance stage completes,
    then cancels any working orders so nothing is left live overnight. Both steps are
    guarded so a failure in one still attempts the other.
    """
    log.warning("shutting down — finishing current stage, then cancelling open orders")
    try:
        scheduler.shutdown(wait=True)
    except Exception as exc:  # noqa: BLE001
        log.error("scheduler shutdown error", extra={"error": str(exc)})
    try:
        n = broker.cancel_all_orders()
        log.info("cancelled open orders on shutdown", extra={"count": n})
    except Exception as exc:  # noqa: BLE001
        log.error("cancel-all on shutdown failed", extra={"error": str(exc)})


def serve(*, env: str, settings, client, broker, db_engine, hour: int = 16, minute: int = 10) -> None:
    """Run the continuous APScheduler process (blocks until SIGTERM/SIGINT).

    Two jobs: a weekday EOD job at ``hour:minute`` America/New_York (the holiday gate +
    rebalance/monitor branch live inside :func:`daily_job`), and a 60s monitor tick.
    """
    import signal

    import pytz
    from apscheduler.schedulers.blocking import BlockingScheduler

    sched = BlockingScheduler(timezone=pytz.timezone("America/New_York"))
    sched.add_job(
        lambda: daily_job(client=client, broker=broker, db_engine=db_engine, settings=settings,
                          as_of=date.today(), overlay=True,
                          ingest_fn=lambda d: ingest.run_daily_ingest(env=env, as_of=d)),
        "cron", day_of_week="mon-fri", hour=hour, minute=minute, id="eod")
    sched.add_job(lambda: continuous_monitor_job(client, db_engine),
                  "interval", seconds=60, id="monitor")

    def _handler(signum, _frame):
        log.warning("signal received; shutting down", extra={"signal": int(signum)})
        graceful_shutdown(sched, broker)
        sys.exit(0)

    signal.signal(signal.SIGTERM, _handler)
    signal.signal(signal.SIGINT, _handler)
    log.info("scheduler started", extra={"env": env, "eod": f"{hour:02d}:{minute:02d} ET"})
    print(f"sharpe-engine scheduler running (env={env}, EOD {hour:02d}:{minute:02d} ET). "
          f"SIGTERM/Ctrl-C to stop.")
    sched.start()


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

    ap = argparse.ArgumentParser(description="End-of-day rebalance driver (Phase 3)")
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument("--once", action="store_true", help="run exactly one rebalance cycle now")
    mode.add_argument("--serve", action="store_true", help="run the continuous scheduler (APScheduler)")
    ap.add_argument("--env", choices=("paper", "live"), required=True)
    ap.add_argument("--date", type=lambda s: date.fromisoformat(s), default=date.today())
    ap.add_argument("--force", action="store_true", help="run even if not a trading day (--once)")
    ap.add_argument("--skip-ingest", action="store_true", help="reuse existing snapshot data (--once)")
    ap.add_argument("--no-overlay", action="store_true", help="equity only; skip the covered-call legs")
    args = ap.parse_args()

    config.load_env(args.env)
    if args.env == "live":
        _countdown(5)
    settings = load_settings()
    client = config.get_alpaca_client()
    broker = Broker(require_env("ALPACA_API_KEY"), require_env("ALPACA_SECRET_KEY"))
    db_engine = db.get_engine()

    if args.serve:
        serve(env=args.env, settings=settings, client=client, broker=broker, db_engine=db_engine)
        return

    if not args.skip_ingest:
        ingest.run_daily_ingest(env=args.env, as_of=args.date.isoformat())

    result = run_cycle(client=client, broker=broker, db_engine=db_engine,
                       settings=settings, as_of=args.date, force=args.force,
                       overlay=not args.no_overlay)
    print(f"\nCycle {args.date} → {result.status}")
    if result.exec_report:
        r = result.exec_report
        print(f"  equity: submitted {r.submitted}  filled {r.filled}  partial {r.partial}  "
              f"rejected {r.rejected}  deferred {r.deferred}")
    if not args.no_overlay:
        print(f"  calls: closed {result.calls_closed}  written {result.calls_written}")
    if result.monitor and result.monitor.drift is not None:
        print(f"  NAV {result.monitor.nav}  drift {result.monitor.drift:.3f}")


if __name__ == "__main__":
    main()
