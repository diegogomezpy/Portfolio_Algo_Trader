"""Data retention — the ``data_retention`` settings finally do something (audit F2).

Two growth sinks, both previously unbounded:

* **snapshots** — the 60s monitor writes ~1,440 rows/day of JSON-blob rows, 24/7
  (~500k rows / ~1GB a year). :func:`thin_snapshots` keeps full resolution for the
  recent window and exactly ONE row per day (each day's last snapshot — the daily
  close the NAV curve needs) beyond it. The dashboard's daily-sampled history
  (audit B6) reads identically before and after thinning.
* **data/raw/equities/*.parquet** — one file per trading day, forever.
  :func:`prune_equity_parquet` deletes files older than the configured window.
  The factor pipeline needs at most ``beta_window + buffer`` (~257 files); the
  default 730-day window leaves 2 full years for research/backtests. Fundamentals
  are never pruned (tiny, and point-in-time history is irreplaceable).

Both are idempotent, best-effort (callers wrap in try/except — retention must never
break a trading day), and unit-tested on sqlite + tmp dirs.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from engine.logger import get_logger

log = get_logger(__name__)


def thin_snapshots(db_engine, *, keep_intraday_days: int, now: datetime | None = None) -> int:
    """Delete intraday snapshot rows older than ``keep_intraday_days``, keeping each day's last.

    Returns the number of rows deleted. Rows newer than the cutoff are untouched (full 60s
    resolution); older days each retain exactly one row — their final snapshot.
    """
    from sqlalchemy import delete, func, select
    from engine.db import snapshots

    now = now or datetime.now(timezone.utc)
    cutoff = now.replace(tzinfo=None) - timedelta(days=int(keep_intraday_days))
    keep = (select(func.max(snapshots.c.ts))
            .where(snapshots.c.ts < cutoff)
            .group_by(func.date(snapshots.c.ts)))
    with db_engine.begin() as conn:
        result = conn.execute(
            delete(snapshots).where(snapshots.c.ts < cutoff,
                                    snapshots.c.ts.not_in(keep)))
    n = int(result.rowcount or 0)
    if n:
        log.info("snapshot retention: thinned intraday rows",
                 extra={"deleted": n, "cutoff": str(cutoff.date())})
    return n


def prune_equity_parquet(prices_dir, *, keep_days: int, today=None,
                         max_prune_frac: float = 0.10) -> int:
    """Delete per-date equity Parquet files older than ``keep_days``. Returns files removed.

    Only files named ``YYYY-MM-DD.parquet`` are considered (the store's own naming);
    anything else is left alone.

    **Tripwire:** refuses to act (returns 0, logs at ERROR) if the pass would delete more
    than ``max_prune_frac`` of the store. Steady-state retention removes ~1 file per
    trading day; wanting to delete a large fraction at once means either a first-run
    backlog (run manually with a higher ``max_prune_frac`` after checking) or a
    misconfiguration/accident — the 2026-07-03 incident, where a default-on retention
    call inside the test suite pruned ~⅔ of the local store.
    """
    from datetime import date as _date

    today = today or datetime.now(timezone.utc).date()
    cutoff = today - timedelta(days=int(keep_days))
    dated = []
    for f in sorted(Path(prices_dir).glob("*.parquet")):
        try:
            dated.append((f, _date.fromisoformat(f.stem)))
        except ValueError:
            continue                                     # not a dated snapshot — leave it
    doomed = [f for f, d in dated if d < cutoff]
    if not doomed:
        return 0
    if dated and len(doomed) / len(dated) > max_prune_frac:
        log.error("equity-parquet retention REFUSED: would delete %d/%d files (> %.0f%%) — "
                  "first-run backlog or misconfiguration; prune manually if intended",
                  len(doomed), len(dated), max_prune_frac * 100)
        return 0
    removed = 0
    for f in doomed:
        try:
            f.unlink()
            removed += 1
        except OSError as exc:
            log.warning("retention: could not remove %s: %s", f, exc)
    if removed:
        log.info("equity-parquet retention: pruned old files",
                 extra={"removed": removed, "cutoff": cutoff.isoformat()})
    return removed


def run_retention(db_engine, settings, *, prices_dir) -> dict:
    """Apply the configured ``data_retention`` policy (best-effort; returns counts)."""
    dr = getattr(settings, "data_retention", None)
    out = {"snapshots_thinned": 0, "parquet_pruned": 0}
    if dr is None:
        return out
    days = int(getattr(dr, "snapshots_intraday_days", 0) or 0)
    if days > 0 and db_engine is not None:
        out["snapshots_thinned"] = thin_snapshots(db_engine, keep_intraday_days=days)
    keep = int(getattr(dr, "raw_equities_days", 0) or 0)
    if keep > 0:
        out["parquet_pruned"] = prune_equity_parquet(prices_dir, keep_days=keep)
    return out
