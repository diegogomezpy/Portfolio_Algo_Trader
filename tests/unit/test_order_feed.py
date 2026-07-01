"""Unit tests for engine.order_feed — the live order cache + REST fallback (no real websocket)."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from engine.order_feed import LiveOrderFeed


def _order(oid, status="filled", fq=10, px=100.0, symbol="AAPL", side="buy"):
    """A minimal SDK-order-like object (getattr-based, like alpaca-py's Order)."""
    return SimpleNamespace(id=oid, client_order_id=f"coid-{oid}", symbol=symbol, side=side,
                           qty=fq, order_type="limit", status=status, limit_price=px,
                           filled_qty=fq, filled_avg_price=px, submitted_at=None, filled_at=None)


def _event(order):
    return SimpleNamespace(order=order, event=str(getattr(order, "status", "")))


class _FakeStream:
    """A TradingStream stand-in: run() synchronously replays events through the async handler."""
    def __init__(self, events):
        self.events = events
        self.handler = None
        self.subscribed = 0
        self.ran = 0

    def subscribe_trade_updates(self, handler):
        self.handler = handler
        self.subscribed += 1

    def run(self):
        self.ran += 1
        for e in self.events:
            asyncio.run(self.handler(e))

    def stop(self):
        pass


def test_stream_events_populate_cache():
    fake = _FakeStream([_event(_order("o1", status="filled", fq=10, px=100.0))])
    feed = LiveOrderFeed(stream_factory=lambda: fake)
    feed._connect_once()                                  # replays the event synchronously
    assert fake.subscribed == 1 and fake.ran == 1
    od = feed.get("o1")
    assert od["status"] == "filled" and od["filled_qty"] == 10.0 and od["filled_avg_price"] == 100.0


def test_state_prefers_cache_over_rest():
    fake = _FakeStream([_event(_order("o1", status="filled"))])
    calls = []
    feed = LiveOrderFeed(stream_factory=lambda: fake, get_order=lambda oid: calls.append(oid) or {})
    feed._connect_once()
    assert feed.state("o1")["status"] == "filled"
    assert calls == []                                    # cache hit → no REST fallback


def test_state_falls_back_to_rest_and_caches():
    feed = LiveOrderFeed(stream_factory=lambda: _FakeStream([]),
                         get_order=lambda oid: {"id": oid, "status": "new", "filled_qty": 0})
    assert feed.get("x") is None                          # not streamed yet
    assert feed.state("x")["status"] == "new"             # REST fallback resolves it
    assert feed.get("x")["status"] == "new"               # and it's now cached


def test_state_resilient_to_fallback_failure():
    def boom(_oid):
        raise RuntimeError("data api down")
    feed = LiveOrderFeed(stream_factory=lambda: _FakeStream([]), get_order=boom)
    assert feed.state("y") is None                        # logged + swallowed, never raises


def test_state_none_when_no_cache_and_no_fallback():
    feed = LiveOrderFeed(stream_factory=lambda: _FakeStream([]))
    assert feed.state("z") is None


def test_reconnect_loop_survives_a_drop():
    # run() raises the first time (a drop), succeeds the second; the loop should reconnect once
    # then stop after we clear the running flag.
    attempts = {"n": 0}

    class _Flaky(_FakeStream):
        def run(self):
            attempts["n"] += 1
            if attempts["n"] == 1:
                raise ConnectionError("dropped")
            # 2nd connect: stop the loop so the test terminates
            feed._running = False

    feed = LiveOrderFeed(stream_factory=lambda: _Flaky([]), reconnect_backoff_s=0)
    feed._running = True
    feed._run_loop()
    assert attempts["n"] == 2                              # dropped once, reconnected once
