"""Unit tests for engine.alerts — classification, DB recording, dry-run vs send.

Fake SMTP transport + in-memory sqlite; no network.
"""

from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace

from sqlalchemy import create_engine, insert, select

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


def _settings():
    return SimpleNamespace(dashboard=SimpleNamespace(
        public_url="https://sharpe-engine.example.ts.net", public_user="viewer"))


def test_email_body_adds_portfolio_snapshot_and_dashboard_link(monkeypatch):
    eng = _engine()
    with eng.begin() as c:
        c.execute(insert(db.snapshots).values(
            ts=datetime(2026, 7, 1, 20, 11), nav=206_540.0, cash=11_230.0, last_equity=204_900.0,
            weights={"AAPL": 0.25, "MSFT": 0.25}, positions={"AAPL": 100, "MSFT": 50}, drift=0.04))
        c.execute(insert(db.rebalance_log).values(
            ts=datetime(2026, 7, 1, 20, 10), trigger_reason="monthly",
            target_weights={"AAPL": 0.25}, risk_gate_passed=True, risk_gate_reason="ok"))
    monkeypatch.setenv("DASHBOARD_PASSWORD", "s3cret")
    sent = []
    alert = alerts.make_alerter(eng, _settings(), send=lambda body, *_: sent.append(body), dry_run=False)
    alert("rebalance 2026-07-01 complete — equity: 2/2 orders filled")
    body = sent[0]
    assert "rebalance 2026-07-01 complete" in body                       # the event message
    assert "NAV" in body and "$206,540" in body                          # portfolio snapshot
    assert "0.50x" in body and "Positions   2" in body and "4.0%" in body
    assert "Last rebal  2026-07-01" in body
    assert "https://sharpe-engine.example.ts.net" in body                # dashboard link…
    assert "viewer / s3cret" in body                                     # …with the login
    # the DB still stores the terse original, not the enriched email body
    assert _rows(eng)[0]["message"] == "rebalance 2026-07-01 complete — equity: 2/2 orders filled"


def test_dashboard_footer_omits_password_when_unset(monkeypatch):
    monkeypatch.delenv("DASHBOARD_PASSWORD", raising=False)
    foot = alerts._dashboard_footer(_settings())
    assert "https://sharpe-engine.example.ts.net" in foot and "login: viewer" in foot
    assert " / " not in foot                                             # no password leaked
    assert alerts._dashboard_footer(SimpleNamespace()) == ""             # no public_url → no footer
