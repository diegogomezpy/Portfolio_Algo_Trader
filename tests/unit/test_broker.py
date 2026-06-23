"""Unit tests for engine.broker — the Alpaca write client.

Real SDK request objects are constructed (offline pydantic models), but the trading
client is a fake, so order placement/cancel/read-back are exercised with no network or
credentials. Guards: request building (symbol/qty/side/limit/client_order_id),
normalization to the orders shape, validation, and APIError → AlpacaAPIError wrapping.
"""

from __future__ import annotations

from datetime import datetime

import pytest
from alpaca.common.exceptions import APIError
from alpaca.trading.enums import OrderSide, PositionIntent

from engine.alpaca_client import AlpacaAPIError
from engine.broker import Broker


class _FakeOrder:
    def __init__(self, **kw):
        self.__dict__.update(kw)


class _FakeTrading:
    """Records submitted request objects and serves them back like Alpaca would."""

    def __init__(self):
        self.submitted = []
        self.cancelled = []
        self.cancel_all_calls = 0
        self._store = {}

    def submit_order(self, order_data):
        oid = f"oid-{len(self.submitted) + 1}"
        self.submitted.append(order_data)
        order = _FakeOrder(
            id=oid,
            client_order_id=getattr(order_data, "client_order_id", None),
            symbol=order_data.symbol,
            side=order_data.side,                       # OrderSide enum (as Alpaca returns)
            qty="10",                                   # str → exercises float coercion
            order_type=getattr(order_data, "type", "market"),
            status="accepted",
            limit_price=getattr(order_data, "limit_price", None),
            filled_qty="0",
            filled_avg_price=None,
            submitted_at=datetime(2026, 7, 1, 16, 30),
            filled_at=None,
        )
        self._store[oid] = order
        return order

    def get_order_by_id(self, order_id):
        return self._store[order_id]

    def cancel_order_by_id(self, order_id):
        self.cancelled.append(order_id)

    def cancel_orders(self):
        self.cancel_all_calls += 1
        return [object(), object()]                     # two acks


class _BoomTrading:
    """Every call fails the way Alpaca signals an API error."""

    def submit_order(self, order_data):
        raise APIError("boom")

    def get_order_by_id(self, order_id):
        raise APIError("boom")

    def cancel_order_by_id(self, order_id):
        raise APIError("boom")

    def cancel_orders(self):
        raise APIError("boom")


def test_requires_credentials_without_injected_client():
    with pytest.raises(ValueError):
        Broker()                                        # no keys, no client
    with pytest.raises(ValueError):
        Broker(api_key="k", secret_key="")              # empty secret


def test_submit_market_order_builds_request_and_normalizes():
    fake = _FakeTrading()
    out = Broker(client=fake).submit_order("AAPL", 10, "buy", client_order_id="cyc-AAPL")
    req = fake.submitted[0]
    assert req.symbol == "AAPL" and float(req.qty) == 10 and req.side == OrderSide.BUY
    assert req.client_order_id == "cyc-AAPL"
    assert out["id"] == "oid-1" and out["symbol"] == "AAPL"
    assert out["side"] == "buy"                         # enum normalized to its value
    assert out["qty"] == 10.0                           # str coerced to float
    assert out["status"] == "accepted"


def test_submit_limit_order_requires_price_and_sets_it():
    with pytest.raises(ValueError):
        Broker(client=_FakeTrading()).submit_order("MSFT", 5, "sell", order_type="limit")
    fake = _FakeTrading()
    out = Broker(client=fake).submit_order("MSFT", 5, "sell", order_type="limit", limit_price=100.5)
    assert float(fake.submitted[0].limit_price) == 100.5
    assert out["side"] == "sell"


def test_bad_side_and_type_raise():
    b = Broker(client=_FakeTrading())
    with pytest.raises(ValueError):
        b.submit_order("AAPL", 1, "hold")
    with pytest.raises(ValueError):
        b.submit_order("AAPL", 1, "buy", order_type="stop")


def test_get_cancel_and_cancel_all():
    fake = _FakeTrading()
    b = Broker(client=fake)
    out = b.submit_order("AAPL", 3, "buy")
    assert b.get_order(out["id"])["id"] == out["id"]
    b.cancel_order(out["id"])
    assert fake.cancelled == [out["id"]]
    assert b.cancel_all_orders() == 2 and fake.cancel_all_calls == 1


def test_submit_option_order_sell_to_open_limit():
    fake = _FakeTrading()
    out = Broker(client=fake).submit_option_order(
        "AAPL260821C00215000", 2, "sell", position_intent="sell_to_open",
        order_type="limit", limit_price=3.10, client_order_id="cyc-AAPL-call")
    req = fake.submitted[0]
    assert req.symbol == "AAPL260821C00215000" and float(req.qty) == 2
    assert req.side == OrderSide.SELL and req.position_intent == PositionIntent.SELL_TO_OPEN
    assert float(req.limit_price) == 3.10 and req.client_order_id == "cyc-AAPL-call"
    assert out["id"] == "oid-1"


def test_submit_option_order_buy_to_close_market():
    fake = _FakeTrading()
    Broker(client=fake).submit_option_order(
        "AAPL260821C00215000", 1, "buy", position_intent="buy_to_close", order_type="market")
    req = fake.submitted[0]
    assert req.side == OrderSide.BUY and req.position_intent == PositionIntent.BUY_TO_CLOSE


def test_submit_option_order_validates_intent_and_price():
    b = Broker(client=_FakeTrading())
    with pytest.raises(ValueError):
        b.submit_option_order("X260821C00100000", 1, "sell", position_intent="bogus")
    with pytest.raises(ValueError):
        b.submit_option_order("X260821C00100000", 1, "sell",
                              position_intent="sell_to_open", order_type="limit")  # no price


def test_api_errors_become_alpaca_api_error():
    b = Broker(client=_BoomTrading())
    with pytest.raises(AlpacaAPIError):
        b.submit_order("AAPL", 1, "buy")
    with pytest.raises(AlpacaAPIError):
        b.get_order("x")
    with pytest.raises(AlpacaAPIError):
        b.cancel_order("x")
    with pytest.raises(AlpacaAPIError):
        b.cancel_all_orders()
