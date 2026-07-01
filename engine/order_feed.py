"""Live order feed — a background trade-updates stream feeding an in-memory order cache.

Replaces REST fill-polling in the execution loop (docs/EXECUTION.md §7, §10). Instead of asking
Alpaca "did it fill?" every couple seconds — rate-limited (~200 req/min) and laggy — we keep an
**always-current** view of every order the broker pushes over its websocket, and the executor
reads that view for free (local memory, no REST, instant).

Design:

* **Always-on:** :meth:`start` launches a daemon thread that runs the stream for the life of the
  process and **auto-reconnects** with a backoff if it drops (so a blip never leaves us blind).
* **REST fallback:** :meth:`state` returns the cached order, or — for an order not yet streamed
  (just submitted) or while the stream is down — falls back to a one-shot ``get_order`` and caches
  it. Combined with idempotent client_order_ids, we can never lose track or double-trade.
* **Shape parity:** cached orders are normalized with :func:`engine.broker._normalize_order`, so
  feed state and ``broker.get_order`` state are byte-for-byte interchangeable to the executor.

The stream object is injected via ``stream_factory`` so the whole module unit-tests with a fake
feed — no websocket, no network.
"""

from __future__ import annotations

import threading
import time
from typing import Callable, Optional

from engine.broker import _normalize_order
from engine.logger import get_logger

log = get_logger(__name__)


class LiveOrderFeed:
    """Background trade-updates stream + thread-safe ``{order_id: order_dict}`` cache."""

    def __init__(self, *, stream_factory: Callable[[], object],
                 get_order: Optional[Callable[[str], dict]] = None,
                 reconnect_backoff_s: float = 5.0) -> None:
        """``stream_factory()`` returns a stream with ``subscribe_trade_updates(handler)`` / ``run()``
        / ``stop()`` (an Alpaca ``TradingStream``). ``get_order`` is the REST fallback."""
        self._stream_factory = stream_factory
        self._get_order = get_order
        self._backoff = reconnect_backoff_s
        self._cache: dict[str, dict] = {}
        self._lock = threading.Lock()
        self._thread: Optional[threading.Thread] = None
        self._stream = None
        self._running = False
        self.connected = False

    # -- websocket side -------------------------------------------------- #
    def _apply(self, order) -> None:
        """Update the cache from one streamed (or REST) order object/dict. Thread-safe."""
        od = order if isinstance(order, dict) else _normalize_order(order)
        oid = od.get("id")
        if oid:
            with self._lock:
                self._cache[oid] = od

    async def _handler(self, data) -> None:
        """Async trade-update handler: cache the order carried by each event."""
        order = getattr(data, "order", None)
        if order is not None:
            self._apply(order)

    def _connect_once(self) -> None:
        """One connect → subscribe → run (blocks until the stream stops or drops)."""
        self._stream = self._stream_factory()
        self._stream.subscribe_trade_updates(self._handler)
        self.connected = True
        log.info("order feed connected")
        try:
            self._stream.run()
        finally:
            self.connected = False

    def _run_loop(self) -> None:
        while self._running:
            try:
                self._connect_once()
            except Exception as exc:  # noqa: BLE001 — keep the feed alive across any drop
                log.warning("order feed dropped; will reconnect", extra={"error": str(exc)})
            if self._running:
                time.sleep(self._backoff)

    def start(self) -> None:
        """Launch the always-on listener thread (idempotent)."""
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._run_loop, name="order-feed", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        """Signal the loop to stop and close the stream (best-effort)."""
        self._running = False
        if self._stream is not None:
            try:
                self._stream.stop()
            except Exception as exc:  # noqa: BLE001
                log.warning("order feed stop failed", extra={"error": str(exc)})

    # -- reader side (what the executor calls) --------------------------- #
    def get(self, order_id: str) -> Optional[dict]:
        """The cached order state, or ``None`` if the stream hasn't seen it (no REST)."""
        with self._lock:
            return self._cache.get(order_id)

    def state(self, order_id: str) -> Optional[dict]:
        """Cached state, else a one-shot REST fallback (then cache it). ``None`` if unresolved.

        This is the drop-in replacement for ``broker.get_order`` in the execution loop: it's free
        and instant on a cache hit (the common case once the stream is warm), and degrades to a
        single REST read for an order not yet streamed or while the stream is reconnecting."""
        od = self.get(order_id)
        if od is not None:
            return od
        if self._get_order is not None:
            try:
                od = self._get_order(order_id)
            except Exception as exc:  # noqa: BLE001 — a read hiccup just means "unknown for now"
                log.warning("order feed REST fallback failed", extra={"id": order_id, "error": str(exc)})
                return None
            if od and od.get("id"):
                self._apply(od)
                return od
        return None

    def snapshot(self) -> dict[str, dict]:
        """A copy of the whole cache (for the dashboard's live-orders view)."""
        with self._lock:
            return dict(self._cache)
