"""Autonomous per-account strategy scheduler — the "let it run by itself" leg (ADR-001 / D37).

For each account with a saved, **auto-enabled** strategy spec, this fires that sleeve's rebalance on
its cadence. The dashboard's monitor loop calls :func:`run_scheduled` each pass. Guardrails, in
layers:

* **Never the primary.** ``account_runner`` refuses the primary account; the scheduler only iterates
  credstore sleeves. The live book keeps rebalancing through ``run_eod`` unchanged.
* **Opt-in.** ``auto_enabled`` defaults OFF in ``strategy_specs`` — nothing trades automatically
  until the operator flips it on in the builder.
* **Market + timing gate.** Only when the market is open and at/after the configured rebalance hour
  (mirrors the primary book's patient mid-session timing — never the opening auction).
* **Once per period.** A ``last_run`` date lease on the spec row means a monthly strategy fires on
  the first open day of the month and not again; a transient failure leaves the lease unset so the
  next pass retries.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Callable, Optional

from engine.logger import get_logger

log = get_logger(__name__)


def _et_now() -> datetime:
    from zoneinfo import ZoneInfo
    return datetime.now(ZoneInfo("America/New_York"))


def _parse_date(s) -> Optional[date]:
    if not s:
        return None
    if isinstance(s, date):
        return s
    try:
        return date.fromisoformat(str(s)[:10])
    except ValueError:
        return None


def is_due(frequency: str, as_of: date, last_run: Optional[date]) -> bool:
    """Whether a spec on ``frequency`` is due on ``as_of`` given its ``last_run`` — "once per period,
    on the first eligible day". Because the caller only invokes the run when the market is open, the
    first eligible day of the period IS its first trading day (holiday-robust)."""
    if last_run is not None and last_run >= as_of:
        return False                                       # already ran today (or a future stamp)
    freq = (frequency or "monthly").lower()
    if freq == "daily":
        return True
    if freq == "weekly":
        return last_run is None or last_run.isocalendar()[:2] != as_of.isocalendar()[:2]
    return last_run is None or (last_run.year, last_run.month) != (as_of.year, as_of.month)  # monthly


def _market_open(client) -> bool:
    if client is None:
        return True                                        # no clock to consult → don't block (tests)
    try:
        return bool(client.market_clock().get("is_open"))
    except Exception:  # noqa: BLE001 — can't confirm the market is open → do NOT trade
        return False


def run_scheduled(db_engine, settings, *, client=None, alert: Optional[Callable[[str], None]] = None,
                  runner: Optional[Callable] = None, now: Optional[datetime] = None) -> list:
    """Run every due, auto-enabled account sleeve once. Returns the run summaries. ``last_run`` is
    stamped only after a run RETURNS (a transient exception leaves it unset so the next pass retries).
    ``runner`` / ``now`` are injectable for tests."""
    from engine import account_runner, config_strategy, specstore
    runner = runner or account_runner.run_strategy_on_account
    et = now or _et_now()
    as_of = et.date()
    rebal_hour = int(getattr(getattr(settings, "execution", None), "rebalance_hour_et", 13))
    out: list = []
    try:
        rows = specstore.list_specs(db_engine)
    except Exception as exc:  # noqa: BLE001 — store unreadable → nothing to schedule this pass
        log.warning("scheduler: spec list failed: %s", exc)
        return out
    for row in rows:
        acct = row.get("account")
        if not acct or not row.get("auto_enabled") or not row.get("spec"):
            continue
        if not is_due(row.get("rebalance_frequency"), as_of, _parse_date(row.get("last_run"))):
            continue
        if et.hour < rebal_hour:
            continue                                       # wait for the mid-session window
        if not _market_open(client):
            continue
        try:
            spec = config_strategy.spec_from_dict(row["spec"])
            strat = config_strategy.ConfigStrategy(spec)
            res = runner(acct, strat, db_engine=db_engine, settings=settings,
                         mode=row.get("mode") or "normal", as_of=as_of, alert=alert)
            specstore.mark_run(db_engine, acct, as_of)     # lease: don't re-fire this period
            out.append(res)
            log.warning("scheduler ran account sleeve",
                        extra={"account": acct, "status": (res or {}).get("status")})
            if alert:
                alert(f"[{acct}] auto-rebalance ({row.get('rebalance_frequency')}): "
                      f"{(res or {}).get('status')}")
        except Exception as exc:  # noqa: BLE001 — leave last_run unset so the next pass retries
            log.error("scheduler run failed", extra={"account": acct, "error": str(exc)})
            if alert:
                alert(f"[{acct}] auto-rebalance FAILED: {exc}")
    return out
