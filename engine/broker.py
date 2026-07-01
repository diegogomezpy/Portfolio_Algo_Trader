"""Alpaca write client — order placement, cancellation, and status read-back.

The only module besides the execution layer that touches the alpaca-py *write* API.
``engine/alpaca_client.py`` is deliberately read-only so the data stages (ingest,
reconcile, monitor, the dashboard) physically cannot submit an order; this module is
its write-side counterpart and is used by ``engine/execute.py`` alone.

Surface — everything normalized to the ``engine.db.orders`` shape, so an order looks the
same whether it came back from a write here or a read in :class:`AlpacaClient`:

* :meth:`submit_order` — place one equity order (market or limit)
* :meth:`get_order`    — read one order's status / fills back (the execute.py fill poll)
* :meth:`cancel_order` — cancel one working order
* :meth:`cancel_all_orders` — cancel every open order (session end / SIGTERM)

``paper=True`` is hardcoded until go-live (Phase 7), matching :class:`AlpacaClient`. The
SDK ``TradingClient`` is injectable (``client=``) so execution tests run against a fake
with no network or credentials. Errors are raised as :class:`AlpacaAPIError` — the same
taxonomy the read client uses.
"""

from __future__ import annotations

from typing import Any

from alpaca.common.exceptions import APIError
from alpaca.trading.client import TradingClient
from alpaca.trading.enums import OrderSide, PositionIntent, TimeInForce
from alpaca.trading.requests import LimitOrderRequest, MarketOrderRequest

from engine.alpaca_client import AlpacaAPIError
from engine.logger import get_logger

log = get_logger(__name__)

_NO_SYMBOL = "*"

# Option position intents (single-leg). Covered calls use sell_to_open (write) and
# buy_to_close (close); the others are included for completeness.
_POSITION_INTENTS = {
    "buy_to_open": PositionIntent.BUY_TO_OPEN,
    "buy_to_close": PositionIntent.BUY_TO_CLOSE,
    "sell_to_open": PositionIntent.SELL_TO_OPEN,
    "sell_to_close": PositionIntent.SELL_TO_CLOSE,
}

# Time-in-force: DAY (default) and CLS = the 4pm closing auction (limit-on-close / market-on-close),
# used for the execution redesign's residual fallback (docs/EXECUTION.md §8).
_TIF = {"day": TimeInForce.DAY, "cls": TimeInForce.CLS, "opg": TimeInForce.OPG,
        "gtc": TimeInForce.GTC, "ioc": TimeInForce.IOC, "fok": TimeInForce.FOK}


def _tif(name: str) -> TimeInForce:
    try:
        return _TIF[str(name).lower()]
    except KeyError:
        raise ValueError(f"unknown time_in_force {name!r} (want one of {sorted(_TIF)})") from None


class Broker:
    """Write client for the Alpaca **paper** trading API (orders only)."""

    def __init__(
        self,
        api_key: str | None = None,
        secret_key: str | None = None,
        *,
        client: Any | None = None,
    ) -> None:
        """Build the write client.

        Args:
            api_key / secret_key: Alpaca **paper** credentials (required unless
                ``client`` is injected).
            client: a pre-built trading client (or test fake). When given, the
                credentials are not needed and no real client is constructed.

        Raises:
            ValueError: if no ``client`` is injected and a credential is missing.
        """
        if client is not None:
            self._trading = client
            return
        if not api_key or not str(api_key).strip():
            raise ValueError("api_key is required and must be a non-empty string")
        if not secret_key or not str(secret_key).strip():
            raise ValueError("secret_key is required and must be a non-empty string")
        # paper=True hardcoded until Phase 7 go-live (see module docstring).
        self._trading = TradingClient(api_key, secret_key, paper=True)

    def submit_order(
        self,
        symbol: str,
        qty: float,
        side: str,
        *,
        order_type: str = "market",
        limit_price: float | None = None,
        client_order_id: str | None = None,
        time_in_force: str = "day",
    ) -> dict:
        """Submit one equity order; return the normalized order dict.

        ``side`` is ``"buy"`` / ``"sell"``; ``order_type`` is ``"market"`` or ``"limit"`` (limit
        requires ``limit_price``). ``client_order_id`` is the idempotency key the execution layer
        sets per rebalance cycle. ``time_in_force`` defaults to ``"day"`` (lapses at the close);
        pass ``"cls"`` to route the residual into the **closing auction** (limit-on-close when a
        ``limit_price`` is given, else market-on-close) — docs/EXECUTION.md §8.

        Raises:
            ValueError: bad ``side`` / ``order_type`` / ``time_in_force``, or limit without a price.
            AlpacaAPIError: if Alpaca rejects the request.
        """
        side_enum = self._side(side)
        tif = _tif(time_in_force)
        if order_type == "market":
            req = MarketOrderRequest(symbol=symbol, qty=qty, side=side_enum,
                                     time_in_force=tif, client_order_id=client_order_id)
        elif order_type == "limit":
            if limit_price is None:
                raise ValueError("limit order requires limit_price")
            req = LimitOrderRequest(symbol=symbol, qty=qty, side=side_enum,
                                    time_in_force=tif, limit_price=float(limit_price),
                                    client_order_id=client_order_id)
        else:
            raise ValueError(f"unknown order_type {order_type!r} (want 'market' or 'limit')")

        try:
            order = self._trading.submit_order(order_data=req)
        except APIError as exc:
            raise AlpacaAPIError(symbol, "submit_order", str(exc)) from exc
        out = _normalize_order(order)
        log.info("order submitted", extra={"symbol": symbol, "side": str(side).lower(),
                                           "qty": float(qty), "type": order_type,
                                           "id": out["id"], "status": out["status"]})
        return out

    def submit_option_order(
        self,
        option_symbol: str,
        contracts: int,
        side: str,
        *,
        position_intent: str,
        order_type: str = "limit",
        limit_price: float | None = None,
        client_order_id: str | None = None,
    ) -> dict:
        """Submit one single-leg option order (covered-call write/close).

        ``option_symbol`` is the OCC contract; ``contracts`` is the number of contracts;
        ``side`` is ``"buy"``/``"sell"`` and ``position_intent`` one of
        ``sell_to_open`` / ``buy_to_close`` / ``buy_to_open`` / ``sell_to_close`` (covered
        calls use the first two). Time-in-force is DAY. Returns the normalized order dict.

        Raises:
            ValueError: bad side / order_type / position_intent, or limit without a price.
            AlpacaAPIError: if Alpaca rejects the request.
        """
        side_enum = self._side(side)
        intent = self._intent(position_intent)
        if order_type == "market":
            req = MarketOrderRequest(symbol=option_symbol, qty=contracts, side=side_enum,
                                     time_in_force=TimeInForce.DAY, position_intent=intent,
                                     client_order_id=client_order_id)
        elif order_type == "limit":
            if limit_price is None:
                raise ValueError("limit order requires limit_price")
            req = LimitOrderRequest(symbol=option_symbol, qty=contracts, side=side_enum,
                                    time_in_force=TimeInForce.DAY, limit_price=float(limit_price),
                                    position_intent=intent, client_order_id=client_order_id)
        else:
            raise ValueError(f"unknown order_type {order_type!r} (want 'market' or 'limit')")

        try:
            order = self._trading.submit_order(order_data=req)
        except APIError as exc:
            raise AlpacaAPIError(option_symbol, "submit_option_order", str(exc)) from exc
        out = _normalize_order(order)
        log.info("option order submitted",
                 extra={"symbol": option_symbol, "side": str(side).lower(),
                        "intent": str(position_intent).lower(), "contracts": int(contracts),
                        "type": order_type, "id": out["id"], "status": out["status"]})
        return out

    def get_order(self, order_id: str) -> dict:
        """Read one order back by id (status + fills), normalized.

        Raises:
            AlpacaAPIError: if the request fails.
        """
        try:
            order = self._trading.get_order_by_id(order_id)
        except APIError as exc:
            raise AlpacaAPIError(_NO_SYMBOL, "get_order", str(exc)) from exc
        return _normalize_order(order)

    def cancel_order(self, order_id: str) -> None:
        """Cancel one working order.

        Raises:
            AlpacaAPIError: if the request fails.
        """
        try:
            self._trading.cancel_order_by_id(order_id)
        except APIError as exc:
            raise AlpacaAPIError(_NO_SYMBOL, "cancel_order", str(exc)) from exc
        log.info("order cancelled", extra={"id": order_id})

    def cancel_all_orders(self) -> int:
        """Cancel every open order; return how many cancels Alpaca acknowledged.

        Used at session end and on SIGTERM so nothing is left working overnight.

        Raises:
            AlpacaAPIError: if the request fails.
        """
        try:
            result = self._trading.cancel_orders()
        except APIError as exc:
            raise AlpacaAPIError(_NO_SYMBOL, "cancel_all_orders", str(exc)) from exc
        n = len(result or [])
        log.info("cancelled all open orders", extra={"count": n})
        return n

    def close_all_positions(self, *, cancel_orders: bool = True) -> int:
        """Liquidate **every** open position to cash; return how many closes Alpaca acknowledged.

        The emergency flatten (``scripts/killswitch.py --flatten``): submits a closing market
        order for each open position — equities and short options alike — and, with
        ``cancel_orders``, cancels working orders first. Orders are DAY, so a flatten while the
        market is closed fills at the next session. Not part of the normal cycle.

        Raises:
            AlpacaAPIError: if the request fails.
        """
        try:
            result = self._trading.close_all_positions(cancel_orders=cancel_orders)
        except APIError as exc:
            raise AlpacaAPIError(_NO_SYMBOL, "close_all_positions", str(exc)) from exc
        n = len(result or [])
        log.warning("liquidated all positions", extra={"count": n, "cancel_orders": cancel_orders})
        return n

    @staticmethod
    def _side(side: str) -> OrderSide:
        s = str(side).lower()
        if s not in ("buy", "sell"):
            raise ValueError(f"side must be 'buy' or 'sell', got {side!r}")
        return OrderSide.BUY if s == "buy" else OrderSide.SELL

    @staticmethod
    def _intent(position_intent: str) -> PositionIntent:
        key = str(position_intent).lower()
        if key not in _POSITION_INTENTS:
            raise ValueError(
                f"position_intent must be one of {sorted(_POSITION_INTENTS)}, got {position_intent!r}")
        return _POSITION_INTENTS[key]


def _normalize_order(order: Any) -> dict:
    """Normalize an SDK order object to the ``engine.db.orders`` field shape."""
    def _f(v: Any) -> float | None:
        return None if v is None else float(v)

    def _s(v: Any) -> str | None:
        return None if v is None else str(getattr(v, "value", v))

    def _iso(v: Any) -> str | None:
        if v is None:
            return None
        return v.isoformat() if hasattr(v, "isoformat") else str(v)

    return {
        "id": str(getattr(order, "id", "")),
        "client_order_id": getattr(order, "client_order_id", None),
        "symbol": getattr(order, "symbol", None),
        "side": _s(getattr(order, "side", None)),
        "qty": _f(getattr(order, "qty", None)),
        "order_type": _s(getattr(order, "order_type", None)),
        "status": _s(getattr(order, "status", None)),
        "limit_price": _f(getattr(order, "limit_price", None)),
        "filled_qty": _f(getattr(order, "filled_qty", None)),
        "filled_avg_price": _f(getattr(order, "filled_avg_price", None)),
        "submitted_at": _iso(getattr(order, "submitted_at", None)),
        "filled_at": _iso(getattr(order, "filled_at", None)),
    }
