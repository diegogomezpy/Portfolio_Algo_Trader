"""Unit tests for engine.alerts — classification, DB recording, dry-run vs send.

Fake SMTP transport + in-memory sqlite; no network.
"""

from __future__ import annotations

from sqlalchemy import create_engine, select

from engine import alerts, db


def _engine():
    eng = create_engine("sqlite://")
    db.create_all(eng)
    return eng


def _rows(eng):
    with eng.connect() as c:
        return [dict(r) for r in c.execute(select(db.alerts)).mappings()]


def test_classify_covers_the_six_types():
    cases = {
        "rebalance 2026-07-01 complete: 19 submitted": "rebalance_completed",
        "risk gate blocked rebalance: sector cap breached": "risk_gate_block",
        "order rejected AAPL buy 50: 422": "fill_failure",
        "covered-call write rejected NVDA: 403": "fill_failure",
        "assignment of NVDA call": "assignment",
        "position divergence on 2 name(s); DB corrected to Alpaca": "data_staleness",
        "ingest produced stale prices": "data_staleness",
        "reconcile blocked: Alpaca unreachable": "system_error",
    }
    for msg, expected in cases.items():
        assert alerts.classify(msg) == expected
    assert set(cases.values()) <= set(alerts.ALERT_TYPES)


def test_dry_run_records_without_sending():
    eng = _engine()
    sent = []
    alert = alerts.make_alerter(eng, send=lambda *a: sent.append(a), dry_run=True)
    alert("risk gate blocked rebalance: leverage 3.00x exceeds cap")
    rows = _rows(eng)
    assert len(rows) == 1 and rows[0]["alert_type"] == "risk_gate_block"
    assert rows[0]["delivered"] is False and sent == []          # recorded, not emailed


def test_send_when_not_dry_run_marks_delivered():
    eng = _engine()
    sent = []
    alert = alerts.make_alerter(eng, send=lambda msg, atype, settings: sent.append((msg, atype)),
                                dry_run=False)
    alert("rebalance 2026-07-01 complete: 19 submitted, 19 filled")
    rows = _rows(eng)
    assert rows[0]["alert_type"] == "rebalance_completed" and rows[0]["delivered"] is True
    assert len(sent) == 1 and sent[0][1] == "rebalance_completed"


def test_send_failure_records_undelivered_and_does_not_raise():
    eng = _engine()

    def _boom(message, atype, settings):
        raise RuntimeError("smtp down")

    alert = alerts.make_alerter(eng, send=_boom, dry_run=False)
    alert("reconcile blocked: Alpaca unreachable")              # must not raise
    rows = _rows(eng)
    assert rows[0]["alert_type"] == "system_error" and rows[0]["delivered"] is False
