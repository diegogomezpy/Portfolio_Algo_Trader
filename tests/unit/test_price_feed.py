"""Unit tests for engine.price_feed — the live last-price cache (no real websocket)."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from engine.price_feed import LivePriceFeed


def _trade(sym, px):
    return SimpleNamespace(symbol=sym, price=px)


class _FakeStream:
    """A StockDataStream stand-in: run() replays trades through the async handler."""
    def __init__(self, trades):
        self.trades = trades
        self.handler = None
        self.subscribed = []
        self.stopped = 0

    def subscribe_trades(self, handler, *symbols):
        self.handler = handler
        self.subscribed.extend(symbols)

    def run(self):
        for t in self.trades:
            asyncio.run(self.handler(t))

    def stop(self):
        self.stopped += 1


def test_trades_populate_price_cache():
    fake = _FakeStream([_trade("AAPL", 200.0), _trade("MSFT", 400.0), _trade("AAPL", 201.5)])
    feed = LivePriceFeed(stream_factory=lambda: fake, symbols=["AAPL", "MSFT"])
    feed._connect_once()
    assert sorted(fake.subscribed) == ["AAPL", "MSFT"]        # subscribed the held book
    assert feed.get("AAPL") == 201.5 and feed.get("MSFT") == 400.0   # last price wins
    assert feed.snapshot() == {"AAPL": 201.5, "MSFT": 400.0}


def test_get_unknown_symbol_is_none():
    feed = LivePriceFeed(stream_factory=lambda: _FakeStream([]), symbols=["AAPL"])
    assert feed.get("NVDA") is None


def test_set_symbols_change_restarts_stream_to_resubscribe():
    fake = _FakeStream([])
    feed = LivePriceFeed(stream_factory=lambda: fake, symbols=["AAPL"])
    feed._stream = fake                                       # pretend connected
    feed.set_symbols(["AAPL", "NVDA"])                        # changed → stop() so the loop reconnects
    assert fake.stopped == 1
    feed.set_symbols(["AAPL", "NVDA"])                        # unchanged → no extra restart
    assert fake.stopped == 1


def test_reconnect_loop_survives_a_drop():
    attempts = {"n": 0}

    class _Flaky(_FakeStream):
        def run(self):
            attempts["n"] += 1
            if attempts["n"] == 1:
                raise ConnectionError("dropped")
            feed._running = False                             # 2nd connect: end the loop

    feed = LivePriceFeed(stream_factory=lambda: _Flaky([]), reconnect_backoff_s=0)
    feed._running = True
    feed._run_loop()
    assert attempts["n"] == 2
