"""Unit tests for engine.specstore — per-account strategy-spec persistence (the builder's store)."""

from __future__ import annotations

from datetime import date

from engine import config_strategy as CS, db, specstore


def _engine(tmp_path):
    return db.get_engine(f"sqlite:///{tmp_path}/specs.sqlite")


def test_save_get_roundtrip_and_spec_rebuild(tmp_path):
    eng = _engine(tmp_path)
    spec = CS.StrategySpec(name="Trend", signals={"low_vol": 1.0}, max_weight=0.06,
                           rebalance_frequency="weekly")
    specstore.save_spec(eng, "trend", spec, rebalance_frequency="weekly", mode="express",
                        auto_enabled=True)
    got = specstore.get_spec(eng, "trend")
    assert got["name"] == "Trend" and got["rebalance_frequency"] == "weekly"
    assert got["mode"] == "express" and got["auto_enabled"] is True
    assert CS.spec_from_dict(got["spec"]).max_weight == 0.06     # rebuilds the object


def test_upsert_preserves_auto_enabled_and_supports_toggles(tmp_path):
    eng = _engine(tmp_path)
    specstore.save_spec(eng, "trend", CS.StrategySpec(name="A", signals={"low_vol": 1.0}),
                        auto_enabled=True)
    # A later save that doesn't pass auto_enabled must not silently disable the schedule.
    specstore.save_spec(eng, "trend", CS.StrategySpec(name="B", signals={"quality": 1.0}))
    got = specstore.get_spec(eng, "trend")
    assert got["name"] == "B" and got["auto_enabled"] is True
    specstore.set_auto_enabled(eng, "trend", False)
    assert specstore.get_spec(eng, "trend")["auto_enabled"] is False
    specstore.mark_run(eng, "trend", date(2026, 7, 9))
    assert specstore.get_spec(eng, "trend")["last_run"] == "2026-07-09"


def test_list_and_delete(tmp_path):
    eng = _engine(tmp_path)
    specstore.save_spec(eng, "a", CS.StrategySpec(name="A", signals={"low_vol": 1.0}))
    specstore.save_spec(eng, "b", CS.StrategySpec(name="B", signals={"quality": 1.0}))
    assert {r["account"] for r in specstore.list_specs(eng)} == {"a", "b"}
    assert specstore.delete_spec(eng, "a") is True
    assert specstore.get_spec(eng, "a") is None
    assert specstore.delete_spec(eng, "a") is False             # already gone
