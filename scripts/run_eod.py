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
import math
import sys
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Callable, Optional

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from engine import (  # noqa: E402
    alerts, covariance, covered_calls, factors, ingest, monitor, optimize, overrides,
    reconcile, risk, sectors, symbols,
)
from engine.alpaca_client import AlpacaAPIError  # noqa: E402
from engine.config import load_settings  # noqa: E402
from engine import execute  # noqa: E402
from engine import strategy as strategy_mod  # noqa: E402
from engine import strategies as strat_pkg  # noqa: E402
from engine.execute import ExecReport, PlannedOrder, plan_orders, submit_and_track  # noqa: E402
from engine.logger import get_logger  # noqa: E402
from engine.risk import RiskCheckResult  # noqa: E402

log = get_logger(__name__)

# Canonical Parquet store locations (shared with engine.ingest — the live driver no longer
# imports scripts.backtest for them, audit E2).
PRICES_DIR = str(ingest.DEFAULT_PRICES_DIR)
FUNDAMENTALS_DIR = str(ingest.DEFAULT_FUNDAMENTALS_DIR)

# The strategy the live book runs (ADR-001 / D37 A3). One strategy today; config-driven
# naming per strategy arrives with the account-per-strategy plumbing (Phase B).
_STRATEGY_NAME = "low_beta_overwrite"


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
    sigma: Optional[pd.DataFrame] = None        # covariance Σ, for the risk-contribution decomposition


@dataclass
class CycleResult:
    """Outcome of one rebalance cycle."""
    status: str  # "executed" | "blocked_risk" | "not_trading_day" | "no_targets" | "already_running"
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

    The close panel is capped at the longest window any consumer needs (β/vol 252d, Σ 60d,
    overlay IV 60d — all ⊆ beta/vol + buffer). The old ``lookback=10**9`` loaded the entire
    multi-year store (~1,500 parquet files → a ~1500×11k frame) every rebalance — the load
    that OOM-wedged the e2-small VM (audit F1).
    """
    fac = settings.factors
    lookback = max(int(fac.beta_window), int(fac.vol_window)) + 5
    panel = factors.load_close_panel(prices_dir, end=as_of, lookback=lookback)
    allf, eligible = factors.load_scored_fundamentals(fundamentals_dir, settings)
    ff5 = covariance.load_ff5_daily(
        max_stale_days=int(getattr(settings.covariance, "ff5_max_stale_days", 7)))
    sector_map = sectors.load_sector_map()["sector"]

    scores = factors.score_date(as_of, settings=settings, price_panel=panel,
                                all_fundamentals=allf, eligible_symbols=eligible)
    if db_engine is not None:                       # persist for the dashboard's factor panel
        try:
            factors.write_factor_scores(scores, db_engine)
        except Exception as exc:  # noqa: BLE001 — telemetry only; never block the rebalance
            log.warning("factor-score persist failed (non-fatal)", extra={"error": str(exc)})
    sc = scores.set_index("symbol")
    composite = sc["composite_score"].dropna()
    if composite.empty:
        return TargetPlan(pd.Series(dtype=float), {}, universe=set(), sector_map=sector_map, panel=panel)

    top = composite.sort_values(ascending=False).head(settings.optimizer.preselect_top_k).index
    sigma = covariance.safe_covariance(panel[top], ff5, as_of=as_of,
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
    return TargetPlan(res.weights, prices, adv, spread, universe, sector_map, panel=panel, sigma=sigma)


# ====================================================================== #
# Orchestrator
# ====================================================================== #
# Cross-process rebalance mutex (2026-07-06, Diego): ONE rebalance at a time, across every
# entry path — the 13:00 scheduler, the dashboard's `--once` child, and a terminal `--once`
# all funnel through run_cycle, which takes this lease first. A Postgres session advisory
# lock is used because it is atomic AND crash-safe: it dies with the holder's connection,
# so there is no stale-flag failure mode to clean up.
_REBALANCE_LOCK_KEY = 0x5EB10001            # arbitrary stable 64-bit key for the advisory lock


@contextmanager
def rebalance_lease(db_engine):
    """Yield ``True`` while this process holds the exclusive rebalance lease, ``False`` if
    another rebalance holds it. Held on a dedicated connection for the whole ``with`` block
    (hours, for a full chase). Non-Postgres engines (the sqlite test suites) skip locking."""
    if db_engine is None or db_engine.dialect.name != "postgresql":
        yield True
        return
    from sqlalchemy import text
    conn = db_engine.connect()
    got = False
    try:
        got = bool(conn.execute(text("select pg_try_advisory_lock(:k)"),
                                {"k": _REBALANCE_LOCK_KEY}).scalar())
        yield got
    finally:
        try:
            if got:
                conn.execute(text("select pg_advisory_unlock(:k)"), {"k": _REBALANCE_LOCK_KEY})
        finally:
            conn.close()


def _quote_fns(client, settings):
    """``(quote_fn, quote_batch)`` for the tiered executor — the shared builders in
    :mod:`engine.execute` (raw NBBO, stale-print guard, one batched sweep per round)."""
    stale = float(getattr(settings.execution, "stale_trade_max_s", 900) or 0)
    return (execute.quote_fn_for(client, stale_trade_max_s=stale),
            execute.batch_quote_fn(client, stale_trade_max_s=stale))


def _option_touch(client, option_symbol: str, side: str):
    """The touch for chasing an option order: hit the bid to sell-to-open, lift the ask to
    buy-to-close (the option-NBBO touch)."""
    bid, ask = client.latest_option_quote(option_symbol)
    return ask if side == "buy" else bid


def _market_close(client):
    """Today's session close as an aware datetime, or ``None`` if the clock read fails (the
    chaser then runs without a close guarantee rather than blocking)."""
    try:
        nc = client.market_clock().get("next_close")
        return datetime.fromisoformat(nc) if nc else None
    except Exception as exc:  # noqa: BLE001 — a clock-read failure must not block the rebalance
        log.warning("market clock read failed; chasing without a close guarantee", extra={"error": str(exc)})
        return None


# The always-on live order feed (engine.order_feed.LiveOrderFeed), started in serve(); stays None
# outside the scheduler process (tests, --once) so the executor falls back to broker.get_order.
_FEED = None


def _order_state():
    """The live-feed fill reader when the feed is running, else ``None`` (executor → REST fallback)."""
    return _FEED.state if _FEED is not None else None


def _option_chase(client, market_close, settings=None):
    """An :class:`OptionChase` that crosses option orders to the touch until they fill (or the
    session nears ``market_close``). ``None`` when the client can't quote options — the overlay
    then falls back to a single passive pass at the mid. ``min_bid_frac`` (the junk-bid write
    guard) comes from ``settings.covered_calls`` when provided."""
    if not hasattr(client, "latest_option_quote"):
        return None
    kw = {}
    if settings is not None:
        mbf = getattr(settings.covered_calls, "min_bid_frac", None)
        if mbf is not None:
            kw["min_bid_frac"] = float(mbf)
        kw["ladder_rounds"] = int(getattr(settings.covered_calls, "chase_ladder_rounds", 0) or 0)
    return covered_calls.OptionChase(touch=(lambda sym, side: _option_touch(client, sym, side)),
                                     market_close=market_close, order_state=_order_state(), **kw)


def _format_breakdown(report, written, skipped) -> str:
    """Per-asset / per-call summary of what traded and why — for the rebalance alert + log."""
    rows = ["Equities (filled / target):"]
    for ln in sorted(report.lines, key=lambda x: (x["status"] != "filled", x["symbol"])):
        note = f"  — {ln['reason']}" if ln.get("reason") and ln["status"] != "filled" else ""
        rows.append(f"  {ln['symbol']:<6} {ln['side']:<4} {ln['filled']}/{ln['qty']}  "
                    f"{ln['status'].upper()}{note}")
    rows.append("Covered calls:")
    for w in (written or []):
        u = getattr(w, "underlying", None)
        if u is None:
            rows.append(f"  {w}")
        else:
            rows.append(f"  {u:<6} WROTE {getattr(w, 'contracts', '?')}x {getattr(w, 'strike', '?')}C "
                        f"{getattr(w, 'expiration', '?')} Δ{getattr(w, 'delta', '?')} (${getattr(w, 'premium', '?')})")
    for s in (skipped or []):
        sym = s.get("symbol", "?") if isinstance(s, dict) else getattr(s, "symbol", "?")
        reason = s.get("reason", "") if isinstance(s, dict) else getattr(s, "reason", "")
        rows.append(f"  {sym:<6} skipped — {reason}")
    if not (written or skipped):
        rows.append("  (none)")
    return "\n".join(rows)


def run_cycle(
    *,
    client,
    broker,
    db_engine,
    settings,
    as_of: date,
    alert: Callable[[str], None] | None = None,
    lease: Callable = rebalance_lease,
    **kw,
) -> CycleResult:
    """Run one rebalance cycle under the exclusive rebalance lease.

    THE two-rebalances guard: a second trigger — the dashboard button while the 13:00
    scheduler is mid-chase, a double click, a terminal ``--once`` alongside either — finds
    the lease held, trades **nothing**, and returns ``already_running`` (surfaced in the
    console as ``Cycle <date> → already_running`` and alerted). ``lease`` is injectable for
    tests; all other arguments are :func:`_run_cycle_locked`'s.
    """
    with lease(db_engine) as got:
        if not got:
            log.warning("rebalance lease held elsewhere; refusing a concurrent cycle",
                        extra={"date": as_of.isoformat()})
            if alert:
                alert(f"rebalance {as_of.isoformat()} not started: another rebalance is "
                      f"already running (lease held) — refusing to run two at once")
            return CycleResult("already_running")
        return _run_cycle_locked(client=client, broker=broker, db_engine=db_engine,
                                 settings=settings, as_of=as_of, alert=alert, **kw)


def _run_cycle_locked(
    *,
    client,
    broker,
    db_engine,
    settings,
    as_of: date,
    force: bool = False,
    trigger: str = "monthly",
    targets_fn: Callable[..., TargetPlan] | None = None,
    strategy: strategy_mod.Strategy | None = None,
    trading_day_fn: Callable[[object, str], bool] = ingest.is_trading_day,
    overlay: bool = False,
    close_calls_fn: Callable[..., object] = covered_calls.close_calls,
    write_calls_fn: Callable[..., object] = covered_calls.write_calls,
    close_index_fn: Callable[..., object] = covered_calls.close_index_overwrite,
    write_index_fn: Callable[..., object] = covered_calls.write_index_overwrite,
    express: bool = False,
    alert: Callable[[str], None] | None = None,
) -> CycleResult:
    """Run one rebalance cycle (the body; callers use :func:`run_cycle`, which holds the
    lease). Returns a :class:`CycleResult`.

    Targets come from the **Strategy plugin** (ADR-001 / D37): the registered
    ``low_beta_overwrite`` book by default, or ``strategy`` when injected. ``targets_fn`` (tests /
    overrides) is threaded *into* the default strategy so the legacy injection seam still works —
    the plugin wraps the same ``compute_targets``, so targets are identical to the pre-A3 path.

    Order: reconcile (block if Alpaca down) → holiday gate (unless ``force``) → strategy targets
    → pre-trade risk gate (logged to ``rebalance_log``; blocks on failure) →
    [overlay: close the existing option leg] → plan + submit + track equity orders →
    [overlay: write the fresh option leg] → monitor snapshot.

    ``overlay`` (Phase 4) turns on the option legs, dispatched by
    :func:`covered_calls.overlay_mode`: **index** closes/writes the SPY beta-overwrite spread
    (close = buy back the short leg FIRST, then sell the wing — order matters, Alpaca has no
    naked tier); **per_name** is the classic covered-call close-all + rewrite (D31). All four
    leg functions are injectable for testing the sequencing.

    ``express`` (the console's impatient mode) sends the equity legs as **market orders**
    through the single-pass path instead of the tiered chase; option closes still sweep at
    market via ``final_market`` and writes fall back to one passive pass at the mid.
    """
    execute.clear_express_finish(db_engine)   # a stale press must never express a FRESH run
    rec = reconcile.reconcile(client, db_engine, alert=alert)            # raises if Alpaca down

    if not force and not trading_day_fn(client, as_of.isoformat()):
        log.info("not a trading day; skipping cycle", extra={"date": as_of.isoformat()})
        return CycleResult("not_trading_day")

    # Targets via the Strategy plugin. `strategy` injected → use it; else the registered book,
    # with `targets_fn` (tests/overrides) threaded into it. The context carries no dirs, so the
    # plugin calls the producer exactly as before (`targets_fn(settings, as_of, db_engine=...)`)
    # and `compute_targets` uses its own PRICES_DIR/FUNDAMENTALS_DIR defaults.
    strat = strategy
    if strat is None:
        strat_pkg.register_all()
        strat = (strat_pkg.LowBetaOverwrite(targets_fn=targets_fn) if targets_fn is not None
                 else strategy_mod.get(_STRATEGY_NAME))
    ctx = strategy_mod.StrategyContext(settings=settings, db_engine=db_engine,
                                       positions=rec.live_positions)
    book = strat.generate(ctx, as_of)
    weights = book.weights
    if book.is_empty:
        log.warning("no target weights produced; nothing to do")
        return CycleResult("no_targets")
    plan = book.inputs          # PlanInputs — same fields as TargetPlan; downstream plan.* unchanged

    # Sticky dashboard override first (clamped to max_leverage), else settings.yaml.
    leverage = overrides.effective_target_leverage(db_engine, settings)
    rc = risk.check_pretrade(weights, settings=settings, sector_map=plan.sector_map,
                             universe=plan.universe, equity_positions=rec.live_positions,
                             as_of=as_of, leverage=leverage)
    # Euler risk decomposition for the dashboard (engine persists it — dashboard stays Postgres-
    # only). Fully defensive: any failure logs and stores NULL, never blocking the rebalance.
    rc_contrib = None
    try:
        rc_contrib = optimize.risk_contributions(weights, plan.sigma)
    except Exception as exc:  # noqa: BLE001
        log.warning("risk-contribution computation failed (non-fatal)", extra={"error": str(exc)})
    _write_rebalance_log(db_engine, as_of, weights, rc, trigger, risk_contributions=rc_contrib)
    if not rc.approved:
        log.error("risk gate blocked the cycle; no orders submitted", extra={"reason": rc.reason})
        if alert:
            alert(f"risk gate blocked rebalance: {rc.reason}")
        return CycleResult("blocked_risk", weights, rc)

    # Session close + the option chaser, built once and reused for the close-all, the equity
    # chase, and the fresh writes — so every option order crosses to the touch until it fills.
    mkt_close = _market_close(client)
    opt_chase = None if express else _option_chase(client, mkt_close, settings)
    index_mode = covered_calls.overlay_mode(settings) == "index"
    beta_market = str(getattr(getattr(settings, "factors", None), "beta_market", "SPY"))

    # Overlay (D31): close the existing option leg BEFORE equity trades. Index mode closes the
    # SPY spread (short leg first, then the wing); per-name closes every open short call.
    n_closed = 0
    if overlay:
        if index_mode:
            closed = close_index_fn(client, broker, db_engine, as_of=as_of, market=beta_market,
                                    chase=opt_chase, alert=alert)
        else:
            closed = close_calls_fn(client, broker, db_engine, as_of=as_of, chase=opt_chase, alert=alert)
        n_closed = len(closed or [])

    # Deployable base = leverage × account equity (DECISIONS D32). Weights are fractions of
    # this base, so the optimizer/caps are unchanged; only the dollar base scales. A missing
    # equity is a hard error (audit B5): the old settings.portfolio.nav fallback would have
    # silently sized a $1M book at $100k — better to fail the cycle and retry next day.
    equity = _account_equity(client)
    nav = equity * leverage
    orders, pending = plan_orders(weights, rec.live_positions, plan.prices, nav=nav,
                                  settings=settings, adv=plan.adv, spread=plan.spread)

    # Tiered execution (docs/EXECUTION.md §5–§8): each name priced by liquidity tier — deep = fill
    # now, moderate/thin = patient mid→touch ladder, pathological spread = post-and-defer, thin =
    # sliced; residual at the close → closing auction or cross-day. Fills read from the live feed.
    if express:                     # console express mode: collared/queued market sweeps, no ladder
        orders = [PlannedOrder(o.symbol, o.side, o.qty, "market", None, o.notional)
                  for o in orders]
        quote_fn, _batch = _quote_fns(client, settings)
        report = execute.submit_express(
            orders, broker=broker, db_engine=db_engine, cycle_key=as_of.isoformat(),
            pending=pending, quote=quote_fn,
            clock=(client.market_clock if hasattr(client, "market_clock") else None),
            ex=settings.execution, order_state=_order_state(), alert=alert)
    else:
        quote_fn, quote_batch = _quote_fns(client, settings)
        report = submit_and_track(orders, broker=broker, db_engine=db_engine,
                                  cycle_key=as_of.isoformat(), pending=pending,
                                  quote=quote_fn, adv=plan.adv, ex=settings.execution,
                                  market_close=mkt_close, order_state=_order_state(), alert=alert,
                                  quote_batch=quote_batch)

    # Overlay (D31): write fresh calls AFTER equity settles, on the shares actually held. The
    # writes chase to the bid so they actually fill, and the lifecycle row lands only on a fill.
    n_written, written, skipped = 0, [], []
    if overlay:
        held = _equity_shares(client)
        # Index mode: one portfolio-level SPY call-spread overwrite sized to the book's market
        # beta — the low-beta book's single-name options are too illiquid to write. "per_name"
        # keeps the classic covered-call path.
        if index_mode:
            written, skipped, ov = write_index_fn(
                client, broker, db_engine, held, settings=settings, as_of=as_of,
                price_panel=plan.panel, chase=opt_chase, alert=alert)
            log.info("index overwrite result", extra=ov)
        else:
            written, skipped = write_calls_fn(client, broker, db_engine, held,
                                              settings=settings, as_of=as_of,
                                              price_panel=plan.panel, chase=opt_chase, alert=alert)
        n_written = len(written or [])

    mon = monitor.monitor_once(client, db_engine, target_weights=weights.to_dict())
    reconcile.reconcile_orders(client, db_engine, alert=alert)   # blotter ← Alpaca order statuses
    try:                       # reporting must never crash a cycle that already traded
        breakdown = _format_breakdown(report, written, skipped)   # per-asset / per-call detail
        if alert:
            alert(f"rebalance {as_of.isoformat()} complete — equity {report.filled}/{report.submitted} "
                  f"filled ({report.partial} partial, {report.rejected} rejected, {report.deferred} deferred); "
                  f"covered calls {n_closed} closed, {n_written} written.\n\n{breakdown}")
        log.info("rebalance breakdown", extra={"date": as_of.isoformat(), "breakdown": breakdown})
    except Exception as exc:   # noqa: BLE001
        log.error("rebalance breakdown/alert failed (cycle already executed)", extra={"error": str(exc)})
    log.info("cycle executed",
             extra={"date": as_of.isoformat(), "closed": n_closed, "written": n_written,
                    **{k: v for k, v in vars(report).items() if k != "lines"}})
    return CycleResult("executed", weights, rc, report, mon,
                       calls_closed=n_closed, calls_written=n_written)


def _account_equity(client) -> float:
    """Live account equity, or raise — sizing the book off anything else is a silent error.

    The pre-audit fallback (``or settings.portfolio.nav``) would have quietly sized a $1M
    account as $100k if Alpaca ever omitted ``equity``; failing loudly lets ``daily_job``'s
    catch-alert-retry handle it instead (audit B5).
    """
    eq = client.account().get("equity")
    if eq is None or float(eq) <= 0:
        raise RuntimeError(f"account equity unavailable/invalid ({eq!r}) — refusing to size the book")
    return float(eq)


def _equity_shares(client) -> dict[str, float]:
    """Current equity positions ``{symbol: qty}`` (excludes options) — what calls cover."""
    out = {}
    for p in client.all_positions():
        if str(p.get("asset_class") or "us_equity").endswith("equity"):
            out[str(p["symbol"])] = float(p["qty"])
    return out


def _write_rebalance_log(db_engine, as_of: date, weights: pd.Series, rc: RiskCheckResult,
                         trigger: str, risk_contributions: dict | None = None) -> None:
    if db_engine is None:
        return
    from sqlalchemy import insert
    from engine.db import rebalance_log
    with db_engine.begin() as conn:
        conn.execute(insert(rebalance_log).values(
            ts=datetime.now(timezone.utc), trigger_reason=trigger,
            target_weights={k: float(v) for k, v in weights.items()},
            risk_gate_passed=rc.approved, risk_gate_reason=rc.reason,
            risk_contributions=risk_contributions))


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
    """Nonzero **equity** legs of a {symbol: qty/weight} map (OCC option symbols excluded)."""
    return {str(s) for s, q in (d or {}).items() if q and symbols.is_equity(s)}


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


def _open_pending(db_engine) -> list[dict]:
    """Unapplied deferred residuals ({id, symbol, side, qty}) — the cross-day work queue."""
    from sqlalchemy import select
    from engine.db import pending_adjustments as pa
    with db_engine.connect() as conn:
        rows = conn.execute(select(pa.c.id, pa.c.symbol, pa.c.side, pa.c.qty)
                            .where(pa.c.applied.is_(False))).mappings().all()
    return [dict(r) for r in rows]


def _mark_applied(db_engine, ids) -> None:
    if not ids:
        return
    from sqlalchemy import update
    from engine.db import pending_adjustments as pa
    with db_engine.begin() as conn:
        conn.execute(update(pa).where(pa.c.id.in_(list(ids))).values(applied=True))


def work_pending_adjustments(client, broker, db_engine, *, settings, as_of, alert=None) -> int:
    """Cross-day catch-up: re-attempt still-open deferred residuals for names **still in the
    current target**, via the fill chaser.

    A name that dropped out of the target — or is already at its target weight — is just marked
    applied (no trade). A residual that still can't fill writes a fresh pending row, so the next
    trading day re-works it: it keeps trying until the name is held or leaves the target. Drift on
    non-deferred names is never touched (monthly-only cadence, DECISIONS D31).
    """
    open_rows = _open_pending(db_engine)
    if not open_rows:
        return 0
    target = monitor.last_target_weights(db_engine) or {}
    held = {str(p["symbol"]): float(p["qty"]) for p in client.all_positions()
            if symbols.is_equity(p["symbol"])}                    # equities only
    equity = _account_equity(client)                              # raise > silently mis-size (B5)
    nav = equity * overrides.effective_target_leverage(db_engine, settings)
    ex = settings.execution

    ids_by_sym: dict[str, list] = {}
    for r in open_rows:
        ids_by_sym.setdefault(r["symbol"], []).append(r["id"])

    orders, resolve = [], []
    for sym, ids in ids_by_sym.items():
        tgt_w = float(target.get(sym, 0.0))
        if tgt_w <= 0:                                            # dropped from target → done
            resolve += ids; continue
        try:
            price = float(client.latest_trade(sym))
        except Exception:                                        # noqa: BLE001 — can't price → retry next day
            continue
        tgt_shares = int(math.floor(tgt_w * nav / price)) if price > 0 else 0
        delta = tgt_shares - int(round(held.get(sym, 0.0)))
        if delta == 0 or abs(delta) * price < ex.min_trade_usd:  # already at target / sub-min → done
            resolve += ids; continue
        orders.append(PlannedOrder(sym, "buy" if delta > 0 else "sell", abs(delta), "limit",
                                   None, round(abs(delta) * price, 2)))
        resolve += ids                                           # re-attempted; a fresh pending rolls if still short

    _mark_applied(db_engine, resolve)
    if not orders:
        return 0
    # Cross-day top-up runs the same tiered executor as the rebalance. ADV isn't recomputed here,
    # so names fall to the "thin" tactic (patient ladder + phantom guard) — the safe default for
    # completing a residual.
    quote_fn, quote_batch = _quote_fns(client, settings)
    rep = submit_and_track(orders, broker=broker, db_engine=db_engine,
                           cycle_key=f"{as_of.isoformat()}-topup", pending=[],
                           quote=quote_fn, adv={}, ex=ex, market_close=_market_close(client),
                           order_state=_order_state(), alert=alert, quote_batch=quote_batch)
    log.info("cross-day top-up", extra={"date": as_of.isoformat(), "names": len(orders), "filled": rep.filled})
    if alert and rep.filled:
        alert(f"cross-day top-up {as_of.isoformat()}: filled {rep.filled}/{rep.submitted} "
              f"previously-deferred name(s)")
    return rep.filled


def daily_job(
    *,
    client,
    broker,
    db_engine,
    settings,
    as_of: date,
    ingest_fn: Callable[[str], None] | None = None,
    targets_fn: Callable[..., TargetPlan] | None = None,   # None → the registered strategy (A3)
    trading_day_fn: Callable[[object, str], bool] = ingest.is_trading_day,
    first_trading_day_fn: Callable[[object, date], bool] | None = None,
    rebalanced_this_month_fn: Callable[[object, date], bool] | None = None,
    overlay: bool = False,
    options_check_fn: Callable[..., object] = covered_calls.options_daily_check,
    pending_work_fn: Callable[..., int] = work_pending_adjustments,
    retention_fn: Callable[..., dict] | None = None,
    alert: Callable[[str], None] | None = None,
) -> DailyResult:
    """The once-a-day scheduled job: gate on the calendar, ingest, then branch.

    ``retention_fn`` (the data_retention pass) is **None by default — retention only runs
    when the production entrypoint (serve/--once) passes it explicitly**. It deletes real
    files, and defaulting it on burned us once: the integration suite calls ``daily_job``
    against the repo working dir, and the default-on retention pruned ~1,000 historical
    parquet files from the local store mid-test-run (2026-07-03; restored from the VM).
    Destructive side effects must be opt-in at the call site, like ``ingest_fn``.

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
        try:
            ingest_fn(as_of.isoformat())
        except Exception as exc:  # noqa: BLE001 — a failed data refresh must NOT kill the trading
            # day: factors tolerate a day-stale panel and fundamentals lag quarters anyway. The
            # 2026-07-06 rebalance died pre-trade on one malformed EDGAR record in the ingest.
            log.error("daily ingest failed; continuing on existing data",
                      extra={"date": as_of.isoformat(), "error": str(exc)})
            if alert:
                alert(f"system error: daily ingest {as_of.isoformat()} failed ({exc}); "
                      f"continuing on existing data")

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

    # This month's rebalance already landed: keep the DB honest, finish any still-deferred residual,
    # run the overlay safety pass, snapshot.
    reconcile.reconcile(client, db_engine, alert=alert)
    reconcile.reconcile_orders(client, db_engine, alert=alert)   # blotter ← Alpaca order statuses
    try:                       # cross-day: complete still-deferred names that remain in the target
        pending_work_fn(client, broker, db_engine, settings=settings, as_of=as_of, alert=alert)
    except Exception as exc:   # noqa: BLE001 — a top-up failure must not crash the daily job
        log.error("cross-day top-up failed", extra={"date": as_of.isoformat(), "error": str(exc)})
    if overlay:
        # The daily pass needs at most an IV window of closes (per-name rewrites); index mode
        # ignores the panel entirely. The old lookback=10**9 loaded the whole multi-year store
        # EVERY trading day for a 60-day IV estimate (audit F1).
        if covered_calls.overlay_mode(settings) == "index":
            panel = pd.DataFrame()
        else:
            lookback = int(settings.covariance.estimation_window_days) + 5
            panel = factors.load_close_panel(PRICES_DIR, end=as_of, lookback=lookback)
        opt_chase = _option_chase(client, _market_close(client), settings)
        options_check_fn(client, broker, db_engine, settings=settings,
                         as_of=as_of, price_panel=panel, chase=opt_chase, alert=alert)
    tgt = monitor.last_target_weights(db_engine)
    mon = monitor.monitor_once(client, db_engine, target_weights=tgt)
    if retention_fn is not None:   # opt-in only (see docstring) — best-effort, never breaks a day
        try:
            retention_fn(db_engine, settings, prices_dir=PRICES_DIR)
        except Exception as exc:   # noqa: BLE001
            log.warning("retention pass failed (non-fatal)", extra={"error": str(exc)})
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
    if _FEED is not None:
        try:
            _FEED.stop()
        except Exception as exc:  # noqa: BLE001
            log.error("order feed stop failed", extra={"error": str(exc)})


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

    # Start the always-on live order feed (instant fills, no REST polling). If the websocket can't
    # connect it self-reconnects and the executor transparently falls back to broker.get_order.
    global _FEED
    try:
        from engine import config as _config
        from engine.order_feed import LiveOrderFeed
        _FEED = LiveOrderFeed(stream_factory=_config.get_trading_stream, get_order=broker.get_order)
        _FEED.start()
        log.info("live order feed started")
    except Exception as exc:  # noqa: BLE001 — the feed is an optimization; never block startup
        log.error("live order feed failed to start; using REST fallback", extra={"error": str(exc)})
        _FEED = None

    from engine import retention
    sched = BlockingScheduler(timezone=pytz.timezone("America/New_York"))
    sched.add_job(
        lambda: daily_job(client=client, broker=broker, db_engine=db_engine, settings=settings,
                          as_of=date.today(), overlay=True, alert=alert,
                          ingest_fn=lambda d: ingest.run_daily_ingest(env=env, as_of=d),
                          retention_fn=retention.run_retention),   # opt-in: production only
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
    ap.add_argument("--express", action="store_true",
                    help="market orders instead of the tiered chase (--once; console express mode)")
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
        try:
            ingest.run_daily_ingest(env=args.env, as_of=args.date.isoformat())
        except Exception as exc:  # noqa: BLE001 — same contract as daily_job: trade on existing
            # data rather than not at all (and say so, instead of dying before run_cycle).
            log.error("ingest failed; continuing on existing data", extra={"error": str(exc)})
            print(f"ingest failed ({exc}); continuing on existing data")

    result = run_cycle(client=client, broker=broker, db_engine=db_engine,
                       settings=settings, as_of=args.date, force=args.force,
                       overlay=not args.no_overlay, express=args.express, alert=alerter)
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
