"""End-of-day rebalance driver (BUILD_ORDER Phase 3).

Wires the equity pipeline into one cycle:

    reconcile → holiday gate → compute targets → risk gate → execute → monitor

Two modes:

* ``--once`` (3.6) — run exactly one cycle now (what the Phase 3 gate exercises:
  "first paper rebalance executes").
* ``--serve`` (3.7) — the continuous APScheduler process: a daily 15:00-ET job that
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
    alerts, covariance, covered_calls, factors, ingest, monitor, optimize, reconcile, risk, sectors,
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
    if alert:
        r = report
        alert(f"rebalance {as_of.isoformat()} complete — equity: {r.filled}/{r.submitted} orders "
              f"filled ({r.partial} partial, {r.rejected} rejected, {r.deferred} deferred); "
              f"covered calls: {n_closed} closed, {n_written} written")
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
    status: str  # "rebalanced" | "monitored" | "not_trading_day" | "rebalance_failed"
    cycle: Optional[CycleResult] = None
    monitor: Optional[monitor.MonitorResult] = None


def is_first_trading_day_of_month(client, as_of: date) -> bool:
    """Whether ``as_of`` is the first NYSE trading day of its month (Alpaca calendar)."""
    start = as_of.replace(day=1)
    cal = client.market_calendar(start.isoformat(), as_of.isoformat())
    days = sorted(str(d.get("date"))[:10] for d in cal)
    return bool(days) and days[0] == as_of.isoformat()


# The catch-up keeps rebalancing until the book holds at least this fraction of the latest
# target's equity names. Keying on coverage of the target — not merely "any position exists" —
# stops a rebalance that filled only a name or two (passive limits that mostly didn't fill, an
# outage mid-fill) from prematurely counting as done and stranding the rest in
# ``pending_adjustments`` until next month. The fraction (not 100%) keeps a couple of
# genuinely-unfillable names from forcing perpetual retries; a persistent shortfall still surfaces
# via the per-rebalance fill alert.
_CATCHUP_MIN_FILL_FRAC = 0.8


def _equity_names(d: dict | None) -> set[str]:
    """Nonzero **equity** legs of a {symbol: qty/weight} map (short tickers; OCC options are long)."""
    return {str(s) for s, q in (d or {}).items() if q and len(str(s)) <= 5}


def _latest_positions(db_engine) -> dict:
    from sqlalchemy import desc, select
    from engine.db import snapshots
    with db_engine.connect() as conn:
        row = conn.execute(
            select(snapshots.c.positions).order_by(desc(snapshots.c.ts)).limit(1)).first()
    return (row[0] if row else None) or {}


def _book_substantially_filled(db_engine) -> bool:
    """True once the book holds ``_CATCHUP_MIN_FILL_FRAC`` of the latest target's equity names.

    A gate pass whose orders never filled (submitted into a closed market and cancelled, an outage
    mid-fill) — or filled only partially — must not count as 'rebalanced', or the catch-up stops
    with a thin book and the unfilled names wait a month. With no target recorded yet this falls
    back to 'any equity position held'.
    """
    if db_engine is None:
        return False
    target = _equity_names(monitor.last_target_weights(db_engine))
    held = _equity_names(_latest_positions(db_engine))
    if not target:
        return bool(held)
    import math
    return len(held & target) >= max(1, math.ceil(_CATCHUP_MIN_FILL_FRAC * len(target)))


def rebalance_established_this_month(db_engine, as_of: date) -> bool:
    """True only if this calendar month's rebalance has actually *landed*: an approved
    (risk-gate-passed) ``rebalance_log`` row **and** the book currently holds equity positions.

    Drives the catch-up. Keying on real positions — not merely the gate pass — means a rebalance
    whose orders were submitted but never filled (cancelled into a closed market, an outage
    mid-fill, …) does **not** falsely count as done; the catch-up keeps retrying until fills land,
    so the book can't sit empty for a month. The month check compares the latest passed row's
    ``ts`` to ``as_of`` in Python (ts is stamped UTC, the run is afternoon-ET ⇒ same calendar
    month) to dodge any SQL timezone footgun. A blocked-only or last-month row returns False.
    """
    if db_engine is None:
        return False
    from sqlalchemy import desc, select
    from engine.db import rebalance_log
    with db_engine.connect() as conn:
        row = conn.execute(
            select(rebalance_log.c.ts).where(rebalance_log.c.risk_gate_passed.is_(True))
            .order_by(desc(rebalance_log.c.ts)).limit(1)).first()
    if not row or row[0] is None:
        return False
    ts = row[0]
    if isinstance(ts, str):                                  # sqlite may hand back a string
        ts = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    if (ts.year, ts.month) != (as_of.year, as_of.month):
        return False
    return _book_substantially_filled(db_engine)             # …and the orders substantially filled


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
    rebalanced_this_month_fn: Callable[[object, date], bool] | None = None,
    overlay: bool = False,
    options_check_fn: Callable[..., object] = covered_calls.options_daily_check,
    alert: Callable[[str], None] | None = None,
) -> DailyResult:
    """The once-a-day scheduled job: gate on the calendar, ingest, then branch.

    Runs the full rebalance cycle (:func:`run_cycle`, with the covered-call ``overlay`` when
    enabled) on the **first trading day of the month** — or, if that run was missed/blocked
    (process down, Alpaca/data hiccup, or a risk-gate block), on the **next trading day(s) until
    one lands** (a *catch-up*, so a single bad day can't skip a whole month's rebalance). The
    trigger is anchored on "this month's rebalance hasn't established positions yet"
    (:func:`rebalance_established_this_month`) — keyed on real fills, not just a gate pass — so it
    never rebalances twice in a month yet won't be fooled by a pass whose orders never filled. Any
    other trading day does a lightweight reconcile +
    monitor pass; non-trading days are skipped. A rebalance that raises is caught, alerted, and
    retried next trading day rather than killing the scheduler. ``ingest_fn`` (default ``None`` =
    skip, so tests never hit the network) refreshes the day's data first.
    """
    if not trading_day_fn(client, as_of.isoformat()):
        log.info("not a trading day; daily job skipped", extra={"date": as_of.isoformat()})
        return DailyResult("not_trading_day")

    if ingest_fn is not None:
        ingest_fn(as_of.isoformat())

    first_of_month = first_trading_day_fn or is_first_trading_day_of_month
    done_this_month = rebalanced_this_month_fn or rebalance_established_this_month

    if not done_this_month(db_engine, as_of):
        trigger = "monthly" if first_of_month(client, as_of) else "monthly_catchup"
        try:
            cyc = run_cycle(client=client, broker=broker, db_engine=db_engine, settings=settings,
                            as_of=as_of, force=True, trigger=trigger, targets_fn=targets_fn,
                            trading_day_fn=trading_day_fn, overlay=overlay, alert=alert)
        except Exception as exc:  # noqa: BLE001 — one bad day must not kill the scheduler; alert + retry next day
            log.error("monthly rebalance raised; will retry next trading day",
                      extra={"date": as_of.isoformat(), "trigger": trigger, "error": str(exc)})
            if alert:
                alert(f"system error: monthly rebalance {as_of.isoformat()} failed to complete "
                      f"({exc}); will retry next trading day")
            return DailyResult("rebalance_failed")
        if cyc.status != "executed":
            # blocked_risk already alerted inside run_cycle; surface no_targets here. Either way
            # no approved row is written, so the catch-up retries on the next trading day.
            log.warning("monthly rebalance did not execute; will retry next trading day",
                        extra={"date": as_of.isoformat(), "status": cyc.status, "trigger": trigger})
            if alert and cyc.status == "no_targets":
                alert(f"monthly rebalance {as_of.isoformat()}: no target weights produced; "
                      f"will retry next trading day")
        return DailyResult("rebalanced", cycle=cyc)

    # This month's rebalance already landed: keep the DB honest, run the overlay safety pass, snapshot.
    reconcile.reconcile(client, db_engine, alert=alert)
    if overlay:
        panel = factors.load_close_panel(PRICES_DIR, end=as_of, lookback=10**9)
        options_check_fn(client, broker, db_engine, settings=settings,
                         as_of=as_of, price_panel=panel, alert=alert)
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


def serve(*, env: str, settings, client, broker, db_engine, alert=None,
          hour: int | None = None, minute: int | None = None) -> None:
    """Run the continuous APScheduler process (blocks until SIGTERM/SIGINT).

    Two jobs: a weekday EOD job at ``hour:minute`` America/New_York (the holiday gate +
    rebalance/monitor branch live inside :func:`daily_job`), and a 60s monitor tick.

    Fires at ``settings.execution.rebalance_hour_et`` (default **13:00 ET**) — mid-session so DAY
    orders fill *in-session* (submitting after the 16:00 close would queue them for the next open
    and they'd be cancelled), inside the intraday spread trough (U-shaped: widest at the 9:30 open
    and the close auction, tightest mid-session), with a long runway before the close for fills to
    land. ``hour``/``minute`` override the settings value (tests).
    """
    import signal

    import pytz
    from apscheduler.schedulers.blocking import BlockingScheduler

    ex = settings.execution
    hour = int(getattr(ex, "rebalance_hour_et", 13)) if hour is None else hour
    minute = int(getattr(ex, "rebalance_minute_et", 0)) if minute is None else minute

    sched = BlockingScheduler(timezone=pytz.timezone("America/New_York"))
    sched.add_job(
        lambda: daily_job(client=client, broker=broker, db_engine=db_engine, settings=settings,
                          as_of=date.today(), overlay=True, alert=alert,
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
    _a = settings.alerts
    _smtp = str(getattr(_a, "smtp_host", "") or "").strip()
    _send_on = bool(getattr(_a, "send_enabled", False)) and _smtp not in ("", "TBD")
    alerter = alerts.make_alerter(db_engine, settings, dry_run=not _send_on)

    if args.serve:
        serve(env=args.env, settings=settings, client=client, broker=broker,
              db_engine=db_engine, alert=alerter)
        return

    if not args.skip_ingest:
        ingest.run_daily_ingest(env=args.env, as_of=args.date.isoformat())

    result = run_cycle(client=client, broker=broker, db_engine=db_engine,
                       settings=settings, as_of=args.date, force=args.force,
                       overlay=not args.no_overlay, alert=alerter)
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
