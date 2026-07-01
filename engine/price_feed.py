"""Live price feed — a background market-data stream feeding an in-memory last-price cache.

The market-data sibling of :class:`engine.order_feed.LiveOrderFeed`. It powers the dashboard's
sub-second headline metrics: instead of the 60s monitor snapshot, the dashboard marks NAV and
per-position values to **live streamed trade prices** (docs/EXECUTION.md is the order-side analog).

Design mirrors the order feed: an always-on daemon thread runs an Alpaca ``StockDataStream``,
subscribing to trades for the held book and caching ``{symbol: last_price}``; it auto-reconnects
on a drop. ``set_symbols`` updates the subscription as the book changes (a change restarts the
stream so the reconnect resubscribes — holdings only change at the monthly rebalance). The stream
object is injected via ``stream_factory`` so the module unit-tests with a fake — no websocket.
"""

from __future__ import annotations

import threading
import time
from typing import Callable, Iterable, Optional

from engine.logger import get_logger

log = get_logger(__name__)


class LivePriceFeed:
    """Background trades stream + thread-safe ``{symbol: last_price}`` cache."""

    def __init__(self, *, stream_factory: Callable[[], object], symbols: Iterable[str] = (),
                 reconnect_backoff_s: float = 5.0) -> None:
        self._stream_factory = stream_factory
        self._backoff = reconnect_backoff_s
        self._prices: dict[str, float] = {}
        self._prev_close: dict[str, float] = {}     # prior-day close per symbol → live day %
        self._symbols: set[str] = {s for s in symbols if s}
        self._lock = threading.Lock()
        self._thread: Optional[threading.Thread] = None
        self._stream = None
        self._running = False
        self.connected = False

    async def _on_trade(self, trade) -> None:
        """Cache the last trade price (async handler for the stream)."""
        sym = getattr(trade, "symbol", None)
        px = getattr(trade, "price", None)
        if sym and px:
            with self._lock:
                self._prices[str(sym)] = float(px)

    def _connect_once(self) -> None:
        self._stream = self._stream_factory()
        with self._lock:
            syms = sorted(self._symbols)
        if syms:
            self._stream.subscribe_trades(self._on_trade, *syms)
        self.connected = True
        log.info("price feed connected", extra={"symbols": len(syms)})
        try:
            self._stream.run()
        finally:
            self.connected = False

    def _run_loop(self) -> None:
        while self._running:
            try:
                self._connect_once()
            except Exception as exc:  # noqa: BLE001 — keep the feed alive across any drop
                log.warning("price feed dropped; will reconnect", extra={"error": str(exc)})
            if self._running:
                time.sleep(self._backoff)

    def start(self) -> None:
        """Launch the always-on listener thread (idempotent)."""
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._run_loop, name="price-feed", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._running = False
        if self._stream is not None:
            try:
                self._stream.stop()
            except Exception as exc:  # noqa: BLE001
                log.warning("price feed stop failed", extra={"error": str(exc)})

    def set_symbols(self, symbols: Iterable[str]) -> None:
        """Update the subscribed set. A change restarts the stream so the reconnect resubscribes
        (holdings only change at the monthly rebalance, so this is rare)."""
        want = {s for s in symbols if s}
        with self._lock:
            changed = want != self._symbols
            self._symbols = want
        if changed and self._stream is not None:
            try:
                self._stream.stop()                       # the run loop reconnects with the new set
            except Exception as exc:  # noqa: BLE001
                log.warning("price feed resubscribe stop failed", extra={"error": str(exc)})

    def set_prev_close(self, prev_close) -> None:
        """Cache prior-day closes ``{symbol: price}`` (from Alpaca positions' ``lastday_px``), so the
        dashboard can compute a live day % = (live − prior close) / prior close."""
        with self._lock:
            self._prev_close = {k: float(v) for k, v in (prev_close or {}).items() if v}

    def get(self, symbol: str) -> Optional[float]:
        with self._lock:
            return self._prices.get(symbol)

    def snapshot(self) -> dict[str, float]:
        """A copy of the live price cache (for the NAV overlay)."""
        with self._lock:
            return dict(self._prices)

    def prev_close(self) -> dict[str, float]:
        """A copy of the prior-close cache (for live day %)."""
        with self._lock:
            return dict(self._prev_close)
