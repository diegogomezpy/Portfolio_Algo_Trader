"""Unit tests for engine.retention — the data_retention policy (audit F2)."""

from __future__ import annotations

from datetime import date, datetime, timedelta
from types import SimpleNamespace

from sqlalchemy import create_engine, insert, select

from engine import db, retention


def _engine():
    eng = create_engine("sqlite://")
    db.create_all(eng)
    return eng


_NOW = datetime(2026, 7, 3, 12, 0)


def _snap(eng, ts, nav):
    with eng.begin() as c:
        c.execute(insert(db.snapshots).values(ts=ts, nav=nav, cash=0.0, last_equity=nav,
                                              weights={}, positions={}, drift=None))


def _navs(eng):
    with eng.connect() as c:
        return [r[1] for r in c.execute(
            select(db.snapshots.c.ts, db.snapshots.c.nav).order_by(db.snapshots.c.ts)).all()]


def test_thin_snapshots_keeps_day_last_and_recent_full_res():
    eng = _engine()
    old = _NOW - timedelta(days=40)                          # far past the 30d window
    for hour, nav in ((10, 1.0), (12, 2.0), (15, 3.0)):      # 3 intraday rows on one old day
        _snap(eng, old.replace(hour=hour), nav)
    for mins, nav in ((90, 10.0), (5, 11.0)):                # recent rows stay untouched
        _snap(eng, _NOW - timedelta(minutes=mins), nav)
    deleted = retention.thin_snapshots(eng, keep_intraday_days=30, now=_NOW)
    assert deleted == 2                                      # the old day's 10:00 + 12:00 rows
    assert _navs(eng) == [3.0, 10.0, 11.0]                   # day close kept; recent full res


def test_thin_snapshots_idempotent():
    eng = _engine()
    _snap(eng, _NOW - timedelta(days=40), 1.0)
    assert retention.thin_snapshots(eng, keep_intraday_days=30, now=_NOW) == 0
    assert retention.thin_snapshots(eng, keep_intraday_days=30, now=_NOW) == 0
    assert _navs(eng) == [1.0]                               # a single old row is that day's last


def test_prune_equity_parquet_removes_only_old_dated_files(tmp_path):
    today = date(2026, 7, 3)
    old = (tmp_path / "2024-01-02.parquet"); old.write_bytes(b"x")
    new = (tmp_path / "2026-06-30.parquet"); new.write_bytes(b"x")
    stray = (tmp_path / "notes.parquet"); stray.write_bytes(b"x")   # non-dated → untouched
    removed = retention.prune_equity_parquet(tmp_path, keep_days=730, today=today,
                                             max_prune_frac=1.0)
    assert removed == 1
    assert not old.exists() and new.exists() and stray.exists()


def test_prune_tripwire_refuses_mass_deletion(tmp_path):
    # The 2026-07-03 incident guard: steady-state retention deletes ~1 file/day, so a pass
    # that would remove more than max_prune_frac of the store refuses and deletes NOTHING.
    today = date(2026, 7, 3)
    for stem in ("2020-01-02", "2020-01-03", "2020-01-06", "2026-06-30"):
        (tmp_path / f"{stem}.parquet").write_bytes(b"x")
    removed = retention.prune_equity_parquet(tmp_path, keep_days=730, today=today)  # 3/4 doomed
    assert removed == 0
    assert len(list(tmp_path.glob("*.parquet"))) == 4        # nothing touched


def test_run_retention_reads_settings_and_degrades_when_absent(tmp_path):
    eng = _engine()
    _snap(eng, _NOW - timedelta(days=40), 1.0)
    _snap(eng, _NOW - timedelta(days=40, hours=-2), 2.0)     # same old day, later → kept
    (tmp_path / "2020-01-02.parquet").write_bytes(b"x")      # 1 old …
    for i in range(1, 21):                                   # … among 20 recent (5% < tripwire)
        (tmp_path / f"2026-06-{i:02d}.parquet").write_bytes(b"x")
    s = SimpleNamespace(data_retention=SimpleNamespace(
        raw_equities_days=730, snapshots_intraday_days=30))
    out = retention.run_retention(eng, s, prices_dir=tmp_path)
    assert out == {"snapshots_thinned": 1, "parquet_pruned": 1}
    # No data_retention block at all → no-op, not a crash.
    assert retention.run_retention(eng, SimpleNamespace(), prices_dir=tmp_path) == {
        "snapshots_thinned": 0, "parquet_pruned": 0}


def test_daily_job_retention_is_opt_in_only():
    # Regression lock for the incident: daily_job must NEVER run retention unless the
    # production entrypoint passes it explicitly — the default-on version pruned the real
    # local store when the integration suite exercised daily_job in the repo working dir.
    import inspect
    from scripts import run_eod
    assert inspect.signature(run_eod.daily_job).parameters["retention_fn"].default is None
    src = inspect.getsource(run_eod.serve)
    assert "retention_fn=retention.run_retention" in src     # …and production DOES opt in
