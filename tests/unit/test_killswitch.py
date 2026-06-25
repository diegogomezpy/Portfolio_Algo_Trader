"""Unit tests for scripts.killswitch.run_killswitch — orchestration only (no network/systemd).

The broker, the systemd stop, and the alerter are all injected, so we verify the *decisions*:
halt cancels but never flattens, flatten requires confirmation and liquidates, and an Alpaca
failure is reported rather than crashing.
"""

from __future__ import annotations

from scripts import killswitch


class _FakeBroker:
    def __init__(self, *, cancel=2, closed=3, fail_cancel=False):
        self.cancel, self.closed, self.fail_cancel = cancel, closed, fail_cancel
        self.calls: list = []

    def cancel_all_orders(self):
        self.calls.append("cancel")
        if self.fail_cancel:
            raise RuntimeError("alpaca unreachable")
        return self.cancel

    def close_all_positions(self, cancel_orders=True):
        self.calls.append(("close", cancel_orders))
        return self.closed


def test_halt_cancels_orders_and_never_flattens():
    b, sent = _FakeBroker(), []
    res = killswitch.run_killswitch(flatten=False, env="paper", assume_yes=False,
                                    broker=b, services_fn=lambda: ["stopped sharpe-eod"],
                                    alerter=sent.append)
    assert res["aborted"] is False and res["cancelled"] == 2 and res["flattened"] is None
    assert b.calls == ["cancel"]                              # no liquidation on a halt
    assert sent and "HALT" in sent[0] and "stopped sharpe-eod" in sent[0]


def test_flatten_liquidates_when_confirmed():
    b, sent = _FakeBroker(), []
    res = killswitch.run_killswitch(flatten=True, env="paper", assume_yes=True,   # --yes skips prompt
                                    broker=b, services_fn=lambda: [], alerter=sent.append)
    assert res["cancelled"] == 2 and res["flattened"] == 3
    assert ("close", True) in b.calls                         # positions closed, orders cancelled first
    assert "FLATTEN" in sent[0]


def test_flatten_aborts_without_confirmation(monkeypatch):
    b, sent = _FakeBroker(), []
    monkeypatch.setattr("builtins.input", lambda *_: "nope")  # operator did not type FLATTEN
    res = killswitch.run_killswitch(flatten=True, env="paper", assume_yes=False,
                                    broker=b, services_fn=lambda: [], alerter=sent.append)
    assert res["aborted"] is True and b.calls == [] and sent == []   # nothing touched


def test_reports_alpaca_failure_without_crashing():
    b, sent = _FakeBroker(fail_cancel=True), []
    res = killswitch.run_killswitch(flatten=False, env="paper", assume_yes=False,
                                    broker=b, services_fn=lambda: [], alerter=sent.append)
    assert res["aborted"] is False and res["cancelled"] is None      # cancel failed, no exception
    assert "FAILED" in res["summary"] and sent                       # still recorded the attempt
