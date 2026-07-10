"""Unit tests for engine.reconcile — diff vs DB, correct to Alpaca, block if down.

Fake Alpaca client + in-memory sqlite. Guards the pure diff, the no-divergence path, the
divergence → corrective-snapshot + alert path, the first-run (no prior snapshot) case,
and the block-on-unreachable contract.
"""

from __future__ import annotations

from datetime import datetime

import pytest
from sqlalchemy import create_engine, desc, insert, select

from engine import db, reconcile
from engine.alpaca_client import AlpacaAPIError


class _FakeClient:
    def __init__(self, positions, account=None, raise_positions=False):
        self._positions = positions                  # {symbol: qty}
        self._account = account or {"equity": 100_000.0, "cash": 5_000.0}
        self._raise = raise_positions

    def all_positions(self):
        if self._raise:
            raise AlpacaAPIError("*", "all_positions", "503 unreachable")
        return [{"symbol": s, "qty": q} for s, q in self._positions.items()]

    def account(self):
        return self._account


def _engine():
    eng = create_engine("sqlite://")
    db.create_all(eng)
    return eng


def _seed_snapshot(eng, positions, ts=datetime(2026, 6, 1, 16, 0)):
    with eng.begin() as c:
        c.execute(insert(db.snapshots).values(ts=ts, nav=100_000.0, cash=5_000.0,
                                              positions=positions, weights={}, drift=None))


def _snapshots(eng):
    with eng.connect() as c:
        return [dict(r) for r in c.execute(select(db.snapshots).order_by(db.snapshots.c.ts)).mappings()]


# ------------------------------------------------------------ pure diff ----
def test_diff_positions_flags_only_beyond_threshold():
    live = {"A": 100, "B": 50}
    dbp = {"A": 100, "B": 40, "C": 10}
    d0 = reconcile.diff_positions(live, dbp, threshold=0.0)
    assert {x["symbol"] for x in d0} == {"B", "C"}        # A matches; B off 10, C missing
    assert reconcile.diff_positions(live, dbp, threshold=15) == []   # all within 15


def test_fetch_live_positions_normalizes():
    live = reconcile.fetch_live_positions(_FakeClient({"AAPL": 100, "MSFT": 25}))
    assert live == {"AAPL": 100.0, "MSFT": 25.0}


# --------------------------------------------------------- reconcile() ----
def test_no_divergence_writes_no_snapshot():
    eng = _engine()
    _seed_snapshot(eng, {"AAPL": 100})
    res = reconcile.reconcile(_FakeClient({"AAPL": 100}), eng)
    assert res.corrected is False and res.divergences == []
    assert len(_snapshots(eng)) == 1                     # nothing new written


def test_divergence_corrects_to_alpaca_and_alerts():
    eng = _engine()
    _seed_snapshot(eng, {"AAPL": 100})                   # DB thinks 100…
    alerts = []
    res = reconcile.reconcile(_FakeClient({"AAPL": 120}), eng, alert=alerts.append)  # …Alpaca says 120
    assert res.corrected is True
    assert res.divergences[0]["symbol"] == "AAPL" and res.divergences[0]["delta"] == 20.0
    assert len(alerts) == 1
    snaps = _snapshots(eng)
    assert len(snaps) == 2                               # a corrective snapshot was written
    assert snaps[-1]["positions"] == {"AAPL": 120.0}     # …matching Alpaca
    assert snaps[-1]["nav"] == 100_000.0                 # nav/cash pulled from account()


def test_correction_snapshot_carries_market_value_weights():
    # The corrective snapshot must not write weights={} (audit B7): an empty-weights row made
    # the dashboard's leverage / market values read 0 until the next monitor pass.
    class _MVClient(_FakeClient):
        def all_positions(self):
            return [{"symbol": "AAPL", "qty": 120, "market_value": 24_000.0},
                    {"symbol": "MSFT", "qty": 10, "market_value": 4_000.0}]

    eng = _engine()
    _seed_snapshot(eng, {"AAPL": 100})                   # diverges → correction path
    res = reconcile.reconcile(_MVClient({}), eng)
    assert res.corrected is True
    snaps = _snapshots(eng)
    assert snaps[-1]["weights"] == {"AAPL": 0.24, "MSFT": 0.04}   # mv / nav(100k), not {}


def test_first_run_with_no_prior_snapshot_corrects():
    eng = _engine()                                      # empty — no snapshots
    res = reconcile.reconcile(_FakeClient({"AAPL": 50}), eng)
    assert res.corrected is True
    assert _snapshots(eng)[-1]["positions"] == {"AAPL": 50.0}


def test_blocks_when_alpaca_unreachable():
    eng = _engine()
    alerts = []
    with pytest.raises(AlpacaAPIError):
        reconcile.reconcile(_FakeClient({}, raise_positions=True), eng, alert=alerts.append)
    assert len(alerts) == 1                              # alerted before blocking


def test_no_db_engine_is_fetch_and_report():
    res = reconcile.reconcile(_FakeClient({"AAPL": 10}), None)
    assert res.live_positions == {"AAPL": 10.0} and res.corrected is False


# ----------------------------------------------------- reconcile_orders() ----
class _OrdersClient:
    def __init__(self, orders, raise_get=False):
        self._orders = orders
        self._raise = raise_get

    def get_orders(self, status="all", limit=None, symbols=None):
        if self._raise:
            raise AlpacaAPIError("*", "get_orders", "503")
        return list(self._orders)


def _orders(eng):
    with eng.connect() as c:
        return {r["id"]: dict(r) for r in c.execute(select(db.orders)).mappings()}


def test_reconcile_orders_updates_stale_status_keeps_cycle():
    # The blotter bug: a row stuck at 'pending_cancel' must be refreshed to Alpaca's 'canceled'
    # — without clobbering the immutable order facts or the rebalance cycle key.
    eng = _engine()
    with eng.begin() as c:
        c.execute(insert(db.orders).values(
            id="inbx-1", client_order_id="2026-06-29:INBX:buy", rebalance_cycle="2026-06-29",
            symbol="INBX", side="buy", qty=108, order_type="limit", status="pending_cancel",
            filled_qty=0, filled_avg_price=None))
    client = _OrdersClient([{"id": "inbx-1", "symbol": "INBX", "side": "buy", "qty": 108,
                             "order_type": "limit", "status": "canceled", "filled_qty": 0,
                             "filled_avg_price": None}])
    n = reconcile.reconcile_orders(client, eng)
    assert n == 1
    row = _orders(eng)["inbx-1"]
    assert row["status"] == "canceled"                   # refreshed to Alpaca
    assert row["rebalance_cycle"] == "2026-06-29"        # cycle key preserved


def test_reconcile_orders_inserts_unknown_option_order():
    # An order the executor never persisted (e.g. an option fill) is inserted so the blotter
    # shows everything Alpaca has.
    eng = _engine()
    client = _OrdersClient([{"id": "opt-1", "symbol": "SLV260731C00057500", "side": "sell",
                             "qty": 1, "order_type": "limit", "status": "filled", "filled_qty": 1,
                             "filled_avg_price": 1.19}])
    n = reconcile.reconcile_orders(client, eng)
    assert n == 1
    row = _orders(eng)["opt-1"]
    assert row["status"] == "filled" and row["filled_qty"] == 1 and row["filled_avg_price"] == 1.19
    assert row["rebalance_cycle"] is None


def test_reconcile_orders_skips_multileg_combo_without_symbol():
    # The 2026-07-09 crash: a multi-leg combo (the SPY overwrite spread) comes back from Alpaca as a
    # PARENT order with symbol=None. orders.symbol is NOT NULL, so inserting it raised and aborted
    # the whole reconcile — which ran AFTER the cycle had already traded, killing the rebalance's
    # completion. It's recorded in options_lifecycle already; the equity blotter skips it.
    eng = _engine()
    client = _OrdersClient([
        {"id": "ovw-1", "client_order_id": "ovw:2026-07-09:SPY:r1", "symbol": None, "side": None,
         "qty": 3, "order_type": None, "status": "filled", "filled_qty": 3, "filled_avg_price": -5.42},
        {"id": "opt-1", "symbol": "SLV260731C00057500", "side": "sell", "qty": 1, "order_type": "limit",
         "status": "filled", "filled_qty": 1, "filled_avg_price": 1.19},   # single-leg still inserts
    ])
    n = reconcile.reconcile_orders(client, eng)          # must NOT raise
    assert n == 1                                        # the combo skipped, the single-leg inserted
    rows = _orders(eng)
    assert "ovw-1" not in rows and "opt-1" in rows


def test_reconcile_orders_one_bad_row_does_not_abort_the_sync(monkeypatch):
    # Defense-in-depth: even an unforeseen malformed row is logged and skipped, never crashing the
    # post-trade blotter sync (and thus the cycle).
    eng = _engine()
    good = {"id": "opt-2", "symbol": "IAU260731C00050000", "side": "sell", "qty": 1,
            "order_type": "limit", "status": "filled", "filled_qty": 1, "filled_avg_price": 0.8}
    real = reconcile._upsert_order_status
    def flaky(db_engine, od):
        if od.get("id") == "boom":
            raise RuntimeError("surprise")
        return real(db_engine, od)
    monkeypatch.setattr(reconcile, "_upsert_order_status", flaky)
    n = reconcile.reconcile_orders(_OrdersClient([{"id": "boom"}, good]), eng)
    assert n == 1 and "opt-2" in _orders(eng)


def test_reconcile_orders_resilient_to_get_orders_failure():
    eng = _engine()
    assert reconcile.reconcile_orders(_OrdersClient([], raise_get=True), eng) == 0   # logged, no raise
    assert reconcile.reconcile_orders(_OrdersClient([]), None) == 0                  # no DB → no-op
