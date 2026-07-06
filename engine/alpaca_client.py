"""Alpaca read client for sharpe-engine.

Single point of contact between sharpe-engine and the Alpaca API for all
**read** access — account/positions, live and historical market data, asset
and calendar discovery, order reads, portfolio history, corporate actions,
and option chains. Nothing outside this file should know that the
``alpaca-py`` SDK exists or what a raw Alpaca response looks like — every
public method returns clean, typed Python values.

Reads only. Order placement / cancellation / position-closing are **writes**
and live in the execution layer (``engine/execute.py``, the only module that
touches the alpaca-py order API). Keeping reads and writes in separate
modules means the data-pulling stages (``ingest``, ``reconcile``,
``monitor``, the dashboard) can never accidentally submit an order.

sharpe-engine is **long-only** (SPEC: no short selling), so open positions
carry positive ``qty``. The client still returns ``qty`` signed exactly as
Alpaca reports it (a negative value would mean short) rather than assuming a
sign — consistent with the validate-before-access philosophy throughout.

The book runs on Alpaca **paper** through go-live (BUILD_ORDER Phase 7).
Paper vs live follows ``ALPACA_BASE_URL`` via ``engine.config.is_paper_env`` —
an unset env defaults to paper, the safe side.

It owns three SDK clients, each constructed once: a ``TradingClient``
(account/asset/order reads), a ``StockHistoricalDataClient``
(quotes/trades/bars), and an ``OptionHistoricalDataClient`` (option
chains/quotes for the covered-call overlay). Unused research surfaces
(portfolio history, corporate actions, snapshots, ETB, per-asset info) were
removed in the 2026-07 audit — restore from git history if a phase needs one. No retry logic — one
attempt per call; the scheduler retries at the stage level (3x exponential
backoff per ARCHITECTURE).

Normalization is tiered: hot-path data (quotes, trades, bars, positions,
account) is fully typed; discovery endpoints (orders, asset lists, calendar,
option chains) are validated at the top level and returned as
lightly-normalized structures.
"""

from __future__ import annotations

import os
from datetime import datetime
from typing import Any, Iterable

from alpaca.common.exceptions import APIError
from alpaca.data.enums import DataFeed
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.historical.option import OptionHistoricalDataClient
from alpaca.data.requests import (
    OptionChainRequest,
    OptionLatestQuoteRequest,
    StockBarsRequest,
    StockLatestQuoteRequest,
    StockLatestTradeRequest,
    StockQuotesRequest,
    StockTradesRequest,
)
from alpaca.data.timeframe import TimeFrame
from alpaca.trading.client import TradingClient
from alpaca.trading.enums import AssetClass, AssetStatus, QueryOrderStatus
from alpaca.trading.requests import (
    GetAssetsRequest,
    GetCalendarRequest,
    GetOrdersRequest,
)


class AlpacaError(Exception):
    """Base class for every error raised by :class:`AlpacaClient`."""


class AlpacaAPIError(AlpacaError):
    """Raised when an API request fails.

    The message always includes the symbol and the method name so a failure
    can be traced without the call site. ``status_code`` (when the underlying
    SDK error carries one) lets the execution layer classify transient failures
    (429 rate limit, 5xx) as retry-next-round instead of reject-for-the-day.
    """

    def __init__(self, symbol: str, method: str, message: str, *,
                 status_code: int | None = None) -> None:
        self.symbol = symbol
        self.method = method
        self.status_code = status_code
        super().__init__(
            f"Alpaca API error [method={method!r} symbol={symbol!r}]: {message}"
        )


class AlpacaResponseError(AlpacaError):
    """Raised when a response parses but has an unexpected shape.

    The message includes the symbol, method, the fields that were expected,
    and the fields that were actually present, so an SDK or API change
    surfaces immediately and legibly.
    """

    def __init__(
        self,
        symbol: str,
        method: str,
        expected_fields: Iterable[str],
        actual_fields: Iterable[str],
    ) -> None:
        self.symbol = symbol
        self.method = method
        self.expected_fields = tuple(expected_fields)
        self.actual_fields = tuple(actual_fields)
        super().__init__(
            f"Alpaca response error [method={method!r} symbol={symbol!r}]: "
            f"expected fields {self.expected_fields}, got {self.actual_fields}"
        )


# Mapping of the bar timeframes this client supports to SDK TimeFrame values.
# Kept as a function (not a module constant) because TimeFrame.Hour & friends
# construct a fresh object on each access.
def _timeframe_table() -> dict[str, TimeFrame]:
    return {
        "1Min": TimeFrame.Minute,
        "1Hour": TimeFrame.Hour,
        "1Day": TimeFrame.Day,
        "1Week": TimeFrame.Week,
        "1Month": TimeFrame.Month,
    }

# Placeholder symbol for account-wide calls (no single ticker).
_NO_SYMBOL = "*"


def _parse_occ_symbol(symbol: str) -> dict | None:
    """Parse an OCC option symbol into ``{strike, type, expiration}`` (or None).

    OCC format is ``ROOT + YYMMDD + (C|P) + strike`` where the strike is the last
    eight digits as price × 1000 (e.g. ``NVDA260618C00215000`` → NVDA call,
    2026-06-18, strike 215.0). Returns ``None`` for anything that does not parse,
    so a malformed symbol is skipped rather than crashing the chain read.
    """
    try:
        strike = int(symbol[-8:]) / 1000.0
        cp = symbol[-9]
        ymd = symbol[-15:-9]
        if cp not in ("C", "P") or len(ymd) != 6:
            return None
        year = 2000 + int(ymd[0:2])
        expiration = f"{year:04d}-{int(ymd[2:4]):02d}-{int(ymd[4:6]):02d}"
        return {"strike": strike, "type": "call" if cp == "C" else "put",
                "expiration": expiration}
    except (ValueError, IndexError):
        return None


class AlpacaClient:
    """Read client for the Alpaca **paper** trading and market-data APIs.

    Owns one :class:`TradingClient`, one :class:`StockHistoricalDataClient`,
    and one :class:`OptionHistoricalDataClient`, all created at construction
    time and reused for every call. Every response is validated before any field is
    accessed.
    """

    def __init__(
        self,
        api_key: str,
        secret_key: str,
        feed: str = "iex",
    ) -> None:
        """Construct the client.

        Args:
            api_key: Alpaca API key. Required and non-empty.
            secret_key: Alpaca secret key. Required and non-empty.
            feed: Market-data feed name (``"iex"``, ``"sip"``, ...).

        Raises:
            ValueError: If ``api_key`` or ``secret_key`` is missing/empty,
                or if ``feed`` is not a recognised feed.

        Note:
            Paper vs live follows ``ALPACA_BASE_URL`` (engine.config.is_paper_env);
            an unset env defaults to paper — the safe side.
        """
        if not api_key or not str(api_key).strip():
            raise ValueError("api_key is required and must be a non-empty string")
        if not secret_key or not str(secret_key).strip():
            raise ValueError("secret_key is required and must be a non-empty string")
        try:
            self.feed = DataFeed(feed)
        except ValueError as exc:
            valid = ", ".join(f.value for f in DataFeed)
            raise ValueError(f"unknown feed {feed!r}; valid feeds: {valid}") from exc

        from engine.config import is_paper_env
        self._trading = TradingClient(api_key, secret_key, paper=is_paper_env())
        self._data = StockHistoricalDataClient(api_key, secret_key)
        # Option chain/quote reads for the D3/D4 covered call overlay.
        self._option_data = OptionHistoricalDataClient(api_key, secret_key)
        # Kept for the direct-REST account-activities read (the SDK 0.43 has no
        # account-activities surface, so :meth:`account_activities` calls the REST API).
        self._api_key, self._secret_key = api_key, secret_key
        self._base_url = os.environ.get(
            "ALPACA_BASE_URL", "https://paper-api.alpaca.markets").rstrip("/")

    # ================================================================== #
    # Live market data
    # ================================================================== #
    def latest_quote(self, symbol: str) -> float:
        """Return the latest ask price for ``symbol``.

        Falls back to the latest trade price when no usable ask is available
        (e.g. outside market hours on the IEX feed, when the ask is absent
        or non-positive).

        Raises:
            AlpacaAPIError: If both the quote and the fallback trade fetch
                fail.
            AlpacaResponseError: If the response is missing the symbol.
        """
        request = StockLatestQuoteRequest(symbol_or_symbols=symbol, feed=self.feed)
        try:
            result = self._data.get_stock_latest_quote(request)
        except APIError as exc:
            raise AlpacaAPIError(symbol, "latest_quote", str(exc)) from exc

        quote = result.get(symbol) if isinstance(result, dict) else None
        ask = getattr(quote, "ask_price", None) if quote is not None else None
        if ask is not None and float(ask) > 0:
            return float(ask)
        # Quote unavailable — fall back to the most recent trade.
        return self.latest_trade(symbol)

    def latest_nbbo(self, symbol: str, *, fallback_to_trade: bool = True) -> tuple[float, float]:
        """Return ``(bid, ask)`` from the latest NBBO quote for ``symbol``.

        With ``fallback_to_trade`` (the default), each side falls back to the latest trade
        price when it is absent or non-positive (e.g. outside market hours on the IEX feed).
        The execution chaser passes ``False``: its spread/staleness guards need the RAW quote —
        a missing side silently replaced by a (possibly stale) print would masquerade as a
        tight two-sided market.

        Raises:
            AlpacaAPIError: If both the quote and the fallback trade fetch fail.
        """
        request = StockLatestQuoteRequest(symbol_or_symbols=symbol, feed=self.feed)
        try:
            result = self._data.get_stock_latest_quote(request)
        except APIError as exc:
            raise AlpacaAPIError(symbol, "latest_nbbo", str(exc),
                                 status_code=getattr(exc, "status_code", None)) from exc
        quote = result.get(symbol) if isinstance(result, dict) else None
        bid = getattr(quote, "bid_price", None) if quote is not None else None
        ask = getattr(quote, "ask_price", None) if quote is not None else None
        bid = float(bid) if bid is not None and float(bid) > 0 else None
        ask = float(ask) if ask is not None and float(ask) > 0 else None
        if (bid is None or ask is None) and fallback_to_trade:
            last = self.latest_trade(symbol)
            bid = bid if bid is not None else last
            ask = ask if ask is not None else last
        return bid, ask

    def latest_nbbo_batch(self, symbols) -> dict[str, tuple[float | None, float | None]]:
        """Latest NBBO for many symbols in ONE request — the tiered chase's per-round sweep.

        Per-symbol quote calls at full book size (~40–60 names × quote+post+cancel every 30 s)
        ran ~2× Alpaca's 200 req/min limit; one batched call replaces N of them. Sides are
        ``None`` when absent or non-positive, and there is **no trade fallback** (see
        :meth:`latest_nbbo` — the executor's guards want the raw quote).

        Raises:
            AlpacaAPIError: If the batched request itself fails.
        """
        syms = [str(s) for s in symbols]
        if not syms:
            return {}
        request = StockLatestQuoteRequest(symbol_or_symbols=syms, feed=self.feed)
        try:
            result = self._data.get_stock_latest_quote(request)
        except APIError as exc:
            raise AlpacaAPIError(",".join(syms[:5]), "latest_nbbo_batch", str(exc),
                                 status_code=getattr(exc, "status_code", None)) from exc
        out: dict[str, tuple[float | None, float | None]] = {}
        for s in syms:
            q = result.get(s) if isinstance(result, dict) else None
            bid = getattr(q, "bid_price", None) if q is not None else None
            ask = getattr(q, "ask_price", None) if q is not None else None
            out[s] = (float(bid) if bid is not None and float(bid) > 0 else None,
                      float(ask) if ask is not None and float(ask) > 0 else None)
        return out

    def latest_trades_batch(self, symbols) -> dict[str, tuple[float, "datetime | None"]]:
        """Latest trade ``(price, timestamp)`` for many symbols in one request.

        Names with no usable trade are omitted. The timestamp lets the execution layer drop
        stale prints (a last trade from hours ago anchoring a chase is the INBX phantom-quote
        lesson in mirror image).

        Raises:
            AlpacaAPIError: If the batched request itself fails.
        """
        syms = [str(s) for s in symbols]
        if not syms:
            return {}
        request = StockLatestTradeRequest(symbol_or_symbols=syms, feed=self.feed)
        try:
            result = self._data.get_stock_latest_trade(request)
        except APIError as exc:
            raise AlpacaAPIError(",".join(syms[:5]), "latest_trades_batch", str(exc),
                                 status_code=getattr(exc, "status_code", None)) from exc
        out: dict[str, tuple[float, "datetime | None"]] = {}
        for s in syms:
            t = result.get(s) if isinstance(result, dict) else None
            price = getattr(t, "price", None) if t is not None else None
            if price is not None and float(price) > 0:
                out[s] = (float(price), getattr(t, "timestamp", None))
        return out

    def latest_option_quote(self, option_symbol: str) -> tuple[float | None, float | None]:
        """Return ``(bid, ask)`` for one OCC option contract — the covered-call chaser's touch.

        Crossing to the touch on an option means **hitting the bid** to sell-to-open and
        **lifting the ask** to buy-to-close. Either side is ``None`` when the indicative feed
        omits it (a thin contract, off-hours); the caller then falls back to a market order.
        Uses the ``indicative`` feed — no OPRA subscription, same feed as the chain reader.

        Raises:
            AlpacaAPIError: If the request fails.
        """
        request = OptionLatestQuoteRequest(symbol_or_symbols=option_symbol, feed="indicative")
        try:
            result = self._option_data.get_option_latest_quote(request)
        except APIError as exc:
            raise AlpacaAPIError(option_symbol, "latest_option_quote", str(exc)) from exc
        quote = result.get(option_symbol) if isinstance(result, dict) else None
        bid = getattr(quote, "bid_price", None) if quote is not None else None
        ask = getattr(quote, "ask_price", None) if quote is not None else None
        bid = float(bid) if bid is not None and float(bid) > 0 else None
        ask = float(ask) if ask is not None and float(ask) > 0 else None
        return bid, ask

    def latest_trade(self, symbol: str) -> float:
        """Return the price of the most recent trade for ``symbol``.

        Raises:
            AlpacaAPIError: If the request fails.
            AlpacaResponseError: If the response is missing the symbol or
                the trade price.
        """
        return self.latest_trade_at(symbol)[0]

    def latest_trade_at(self, symbol: str) -> tuple[float, "datetime | None"]:
        """The most recent trade as ``(price, timestamp)`` — the timestamped form of
        :meth:`latest_trade`, so callers can reject stale prints (execution freshness guard).

        Raises:
            AlpacaAPIError: If the request fails.
            AlpacaResponseError: If the response is missing the symbol or
                the trade price.
        """
        request = StockLatestTradeRequest(symbol_or_symbols=symbol, feed=self.feed)
        try:
            result = self._data.get_stock_latest_trade(request)
        except APIError as exc:
            raise AlpacaAPIError(symbol, "latest_trade", str(exc),
                                 status_code=getattr(exc, "status_code", None)) from exc

        trade = self._extract(result, symbol, "latest_trade")
        price = getattr(trade, "price", None)
        if price is None:
            raise AlpacaResponseError(
                symbol, "latest_trade", ("price",), self._obj_fields(trade)
            )
        return float(price), getattr(trade, "timestamp", None)




    def bars(
        self,
        symbol: str,
        start: str,
        end: str,
        timeframe: str = "1Hour",
    ) -> list[dict]:
        """Return OHLCV bars for ``symbol`` in ``[start, end]``.

        ``start`` and ``end`` are ISO date strings (e.g. ``"2024-01-01"``).
        ``timeframe`` is one of ``"1Min"``, ``"1Hour"`` (default), ``"1Day"``,
        ``"1Week"``, ``"1Month"``. Each bar is normalised to::

            {"time": str (ISO), "open": float, "high": float,
             "low": float, "close": float, "volume": float}

        Returns:
            A chronological list of bars (empty if none exist in the range).

        Raises:
            AlpacaAPIError: If the request fails.
            AlpacaResponseError: If a bar is missing an expected field.
            ValueError: If ``start``/``end`` or ``timeframe`` are invalid.
        """
        request = StockBarsRequest(
            symbol_or_symbols=symbol,
            timeframe=self._timeframe(timeframe),
            start=self._parse_dt(start),
            end=self._parse_dt(end),
            feed=self.feed,
        )
        try:
            barset = self._data.get_stock_bars(request)
        except APIError as exc:
            raise AlpacaAPIError(symbol, "bars", str(exc)) from exc

        data = getattr(barset, "data", None)
        if not isinstance(data, dict) or symbol not in data:
            return []
        return [self._parse_bar(bar, symbol) for bar in data[symbol]]

    def bars_multi(
        self,
        symbols: list[str],
        start: str,
        end: str,
        timeframe: str = "1Day",
    ) -> dict[str, list[dict]]:
        """Return OHLCV bars for many ``symbols`` in ``[start, end]`` in one request.

        The batched sibling of :meth:`bars` — the SDK pulls every symbol's bars
        in a single multi-symbol request, which is how the ingest/backfill
        pipeline reads the full equity universe efficiently. Callers chunk the
        symbol list (settings ``ingest.symbol_chunk_size``) to bound request size.

        Returns a dict keyed by symbol; each value is a chronological list of
        bars normalised exactly like :meth:`bars`. Symbols the venue returns no
        data for are simply absent. An empty ``symbols`` list returns ``{}``
        without a request.

        Raises:
            AlpacaAPIError: If the request fails.
            AlpacaResponseError: If a bar is missing an expected field.
            ValueError: If ``start``/``end`` or ``timeframe`` are invalid.
        """
        if not symbols:
            return {}
        label = ",".join(symbols)
        request = StockBarsRequest(
            symbol_or_symbols=list(symbols),
            timeframe=self._timeframe(timeframe),
            start=self._parse_dt(start),
            end=self._parse_dt(end),
            feed=self.feed,
        )
        try:
            barset = self._data.get_stock_bars(request)
        except APIError as exc:
            raise AlpacaAPIError(label, "bars_multi", str(exc)) from exc

        data = getattr(barset, "data", None)
        if not isinstance(data, dict):
            return {}
        return {
            str(sym): [self._parse_bar(bar, str(sym)) for bar in rows]
            for sym, rows in data.items()
        }

    def historical_quotes(
        self, symbol: str, start: str, end: str, limit: int | None = None
    ) -> list[dict]:
        """Return historical (tick) quotes for ``symbol`` in ``[start, end]``.

        Each quote is normalised to
        ``{time, bid_px, bid_size, ask_px, ask_size}``.

        Raises:
            AlpacaAPIError: If the request fails.
        """
        request = StockQuotesRequest(
            symbol_or_symbols=symbol,
            start=self._parse_dt(start),
            end=self._parse_dt(end),
            limit=limit,
            feed=self.feed,
        )
        try:
            result = self._data.get_stock_quotes(request)
        except APIError as exc:
            raise AlpacaAPIError(symbol, "historical_quotes", str(exc)) from exc
        return [
            {
                "time": self._iso(getattr(q, "timestamp", None)),
                "bid_px": self._opt_float(getattr(q, "bid_price", None)),
                "bid_size": self._opt_float(getattr(q, "bid_size", None)),
                "ask_px": self._opt_float(getattr(q, "ask_price", None)),
                "ask_size": self._opt_float(getattr(q, "ask_size", None)),
            }
            for q in self._rows_for(result, symbol)
        ]

    def historical_trades(
        self, symbol: str, start: str, end: str, limit: int | None = None
    ) -> list[dict]:
        """Return historical (tick) trades for ``symbol`` in ``[start, end]``.

        Each trade is normalised to ``{time, price, size}``.

        Raises:
            AlpacaAPIError: If the request fails.
        """
        request = StockTradesRequest(
            symbol_or_symbols=symbol,
            start=self._parse_dt(start),
            end=self._parse_dt(end),
            limit=limit,
            feed=self.feed,
        )
        try:
            result = self._data.get_stock_trades(request)
        except APIError as exc:
            raise AlpacaAPIError(symbol, "historical_trades", str(exc)) from exc
        return [
            {
                "time": self._iso(getattr(t, "timestamp", None)),
                "price": self._opt_float(getattr(t, "price", None)),
                "size": self._opt_float(getattr(t, "size", None)),
            }
            for t in self._rows_for(result, symbol)
        ]

    # ================================================================== #
    # Account / positions
    # ================================================================== #
    def account(self) -> dict:
        """Return the paper account summary.

        Normalised to the fields the strategy and pre-run checklist care
        about: status, equity/buying-power/cash, market values, margin,
        shorting flag, and blocking flags.

        Raises:
            AlpacaAPIError: If the request fails.
            AlpacaResponseError: If core fields are absent.
        """
        try:
            acct = self._trading.get_account()
        except APIError as exc:
            raise AlpacaAPIError(_NO_SYMBOL, "account", str(exc)) from exc
        self._require_attrs(acct, ("equity", "buying_power"), _NO_SYMBOL, "account")
        return {
            "account_number": str(getattr(acct, "account_number", "")),
            "status": self._enum_str(getattr(acct, "status", None)),
            "currency": getattr(acct, "currency", None),
            "equity": self._opt_float(getattr(acct, "equity", None)),
            "last_equity": self._opt_float(getattr(acct, "last_equity", None)),
            "cash": self._opt_float(getattr(acct, "cash", None)),
            "buying_power": self._opt_float(getattr(acct, "buying_power", None)),
            "portfolio_value": self._opt_float(getattr(acct, "portfolio_value", None)),
            "long_market_value": self._opt_float(getattr(acct, "long_market_value", None)),
            "short_market_value": self._opt_float(getattr(acct, "short_market_value", None)),
            "initial_margin": self._opt_float(getattr(acct, "initial_margin", None)),
            "maintenance_margin": self._opt_float(getattr(acct, "maintenance_margin", None)),
            "multiplier": self._opt_float(getattr(acct, "multiplier", None)),
            "daytrade_count": self._opt_int(getattr(acct, "daytrade_count", None)),
            "shorting_enabled": bool(getattr(acct, "shorting_enabled", False)),
            "pattern_day_trader": bool(getattr(acct, "pattern_day_trader", False)),
            "trading_blocked": bool(getattr(acct, "trading_blocked", False)),
            "account_blocked": bool(getattr(acct, "account_blocked", False)),
        }


    def all_positions(self) -> list[dict]:
        """Return every open position on the account.

        Each is normalised like :meth:`get_open_position`.

        Raises:
            AlpacaAPIError: If the request fails.
            AlpacaResponseError: If a position is missing an expected field.
        """
        try:
            positions = self._trading.get_all_positions()
        except APIError as exc:
            raise AlpacaAPIError(_NO_SYMBOL, "all_positions", str(exc)) from exc
        return [
            self._parse_position(p, str(getattr(p, "symbol", _NO_SYMBOL)))
            for p in positions
        ]


    def account_activities(self, activity_types: list[str] | str | None = None, *,
                           date: str | None = None, page_size: int = 100) -> list[dict]:
        """Return account activities — ``["OPASN"]`` for assignments, ``"FEE"`` for fees, etc.

        The installed alpaca-py has no account-activities surface, so this calls the REST
        endpoint ``/v2/account/activities`` directly with the API-key headers. Each record is
        the **raw Alpaca activity dict** (schemas vary by type — fees carry ``net_amount`` /
        ``activity_sub_type``) overlaid with the normalized common fields ``{id, activity_type,
        symbol, qty (float), side, price, date, raw}``, so both raw-shape and normalized
        consumers read one return type. ``date`` filters to one day; ``page_size`` caps the
        page (Alpaca max 100).

        NOTE: this class must define ``account_activities`` exactly ONCE — a second ``def``
        silently shadows the first (Python keeps the last), which is precisely the bug that
        broke assignment detection (audit 2026-07-02). Guarded by the AST no-duplicate-methods
        test in tests/unit/test_no_duplicate_methods.py.

        Raises:
            AlpacaAPIError: If the request fails.
        """
        import json
        import urllib.parse
        import urllib.request

        params = {"page_size": str(min(int(page_size), 100))}   # Alpaca caps page_size at 100
        if activity_types:
            types = [activity_types] if isinstance(activity_types, str) else list(activity_types)
            params["activity_types"] = ",".join(str(t) for t in types)
        if date:
            params["date"] = str(date)
        url = f"{self._base_url}/v2/account/activities?" + urllib.parse.urlencode(params)
        req = urllib.request.Request(url, headers={
            "APCA-API-KEY-ID": self._api_key, "APCA-API-SECRET-KEY": self._secret_key})
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = json.loads(resp.read())
        except Exception as exc:  # noqa: BLE001 — network / HTTP error
            raise AlpacaAPIError(_NO_SYMBOL, "account_activities", str(exc)) from exc
        return [{**a, **self._parse_activity(a)} for a in (data or [])]

    @staticmethod
    def _parse_activity(a: dict) -> dict:
        """Normalize one REST activity record (dict) to common fields; keep the raw."""
        return {
            "id": a.get("id"),
            "activity_type": a.get("activity_type"),
            "symbol": a.get("symbol"),
            "qty": None if a.get("qty") is None else float(a.get("qty")),
            "side": a.get("side"),
            "price": None if a.get("price") is None else float(a.get("price")),
            "date": a.get("date") or a.get("transaction_time"),
            "raw": a,
        }

    # ================================================================== #
    # Asset / order / calendar / corporate-action reads (research)
    # ================================================================== #


    def list_assets(
        self,
        status: str = "active",
        asset_class: str = "us_equity",
        shortable: bool | None = None,
        easy_to_borrow: bool | None = None,
    ) -> list[dict]:
        """Return assets matching the given filters (universe discovery).

        ``status``/``asset_class`` are filtered server-side; ``shortable``
        and ``easy_to_borrow`` are filtered client-side when provided (these
        two are unused by the long-only strategy but kept for generality).
        Each asset is normalised like :meth:`asset_info`.

        Raises:
            AlpacaAPIError: If the request fails.
            ValueError: If ``status``/``asset_class`` is not recognised.
        """
        try:
            request = GetAssetsRequest(
                status=AssetStatus(status), asset_class=AssetClass(asset_class)
            )
        except ValueError as exc:
            raise ValueError(f"invalid status/asset_class: {exc}") from exc
        try:
            assets = self._trading.get_all_assets(request)
        except APIError as exc:
            raise AlpacaAPIError(_NO_SYMBOL, "list_assets", str(exc)) from exc

        out: list[dict] = []
        for asset in assets:
            if shortable is not None and bool(getattr(asset, "shortable", False)) != shortable:
                continue
            if easy_to_borrow is not None and bool(getattr(asset, "easy_to_borrow", False)) != easy_to_borrow:
                continue
            out.append(self._parse_asset(asset))
        return out

    def get_orders(
        self,
        status: str = "all",
        limit: int | None = None,
        symbols: list[str] | None = None,
    ) -> list[dict]:
        """Return orders (read-only) matching the filters.

        ``status`` is ``"open"``, ``"closed"``, or ``"all"`` (default). Order
        *placement* is not here — it lives in the execution layer. Each order
        is normalised to key fields (id, symbol, qty, side, type, status,
        prices, timestamps).

        Raises:
            AlpacaAPIError: If the request fails.
            ValueError: If ``status`` is not recognised.
        """
        try:
            request = GetOrdersRequest(
                status=QueryOrderStatus(status), limit=limit, symbols=symbols
            )
        except ValueError as exc:
            raise ValueError(f"invalid order status {status!r}: {exc}") from exc
        try:
            orders = self._trading.get_orders(request)
        except APIError as exc:
            raise AlpacaAPIError(_NO_SYMBOL, "get_orders", str(exc)) from exc
        return [self._parse_order(order) for order in orders]

    def market_clock(self) -> dict:
        """Return Alpaca's market clock.

        Normalised to ``{timestamp, is_open, next_open, next_close}`` (ISO
        strings + bool). Note: the scheduler's ``holiday_check_job`` uses
        :meth:`market_calendar` to confirm a trading day before running the
        pipeline; this clock is the broker's own, useful for operational
        checks.

        Raises:
            AlpacaAPIError: If the request fails.
            AlpacaResponseError: If core fields are absent.
        """
        try:
            clock = self._trading.get_clock()
        except APIError as exc:
            raise AlpacaAPIError(_NO_SYMBOL, "market_clock", str(exc)) from exc
        self._require_attrs(clock, ("timestamp", "is_open"), _NO_SYMBOL, "market_clock")
        return {
            "timestamp": self._iso(getattr(clock, "timestamp", None)),
            "is_open": bool(clock.is_open),
            "next_open": self._iso(getattr(clock, "next_open", None)),
            "next_close": self._iso(getattr(clock, "next_close", None)),
        }

    def market_calendar(self, start: str, end: str) -> list[dict]:
        """Return the NYSE trading calendar between ``start`` and ``end``.

        ISO date strings in; each entry is
        ``{date, open, close}`` (ISO strings). Useful for backtest date
        ranges and ex-dividend windowing.

        Raises:
            AlpacaAPIError: If the request fails.
            ValueError: If ``start``/``end`` are not ISO dates.
        """
        request = GetCalendarRequest(start=self._parse_date(start), end=self._parse_date(end))
        try:
            calendar = self._trading.get_calendar(request)
        except APIError as exc:
            raise AlpacaAPIError(_NO_SYMBOL, "market_calendar", str(exc)) from exc
        return [
            {
                "date": self._iso(getattr(day, "date", None)),
                "open": self._iso(getattr(day, "open", None)),
                "close": self._iso(getattr(day, "close", None)),
            }
            for day in calendar
        ]


    def option_chain(
        self,
        underlying: str,
        *,
        option_type: str | None = "call",
        expiration_gte: str | None = None,
        expiration_lte: str | None = None,
        strike_gte: float | None = None,
        strike_lte: float | None = None,
        feed: str = "indicative",
    ) -> list[dict]:
        """Return the option chain for ``underlying`` (D3/D4 covered call overlay).

        Read-only chain reader feeding the covered call overlay's target-delta
        (0.30, DECISIONS D29) strike selection. Each contract is normalised to::

            {"symbol", "underlying", "type" ("call"/"put"), "strike",
             "expiration" (ISO date), "bid", "ask", "mid", "last"}

        Strike / expiration / type are parsed from the OCC ``symbol``; ``bid`` /
        ``ask`` come from the contract's latest quote and ``mid`` is their
        midpoint (``None`` when either side is missing or non-positive). Optional
        ``expiration_*`` / ``strike_*`` bounds narrow the request server-side.

        Defaults to the ``indicative`` feed (no OPRA subscription required);
        greeks / implied vol are not provided on that feed, only quotes.

        Raises:
            AlpacaAPIError: If the request fails.
        """
        request = OptionChainRequest(
            underlying_symbol=underlying,
            feed=feed,
            type=option_type,
            expiration_date_gte=expiration_gte,
            expiration_date_lte=expiration_lte,
            strike_price_gte=strike_gte,
            strike_price_lte=strike_lte,
        )
        try:
            chain = self._option_data.get_option_chain(request)
        except APIError as exc:
            raise AlpacaAPIError(underlying, "option_chain", str(exc)) from exc

        out: list[dict] = []
        for sym, snap in (chain or {}).items():
            parsed = _parse_occ_symbol(str(sym))
            if parsed is None:
                continue
            quote = getattr(snap, "latest_quote", None)
            bid = getattr(quote, "bid_price", None) if quote is not None else None
            ask = getattr(quote, "ask_price", None) if quote is not None else None
            trade = getattr(snap, "latest_trade", None)
            last = getattr(trade, "price", None) if trade is not None else None
            bid = float(bid) if bid not in (None, 0) else None
            ask = float(ask) if ask not in (None, 0) else None
            mid = (bid + ask) / 2.0 if (bid is not None and ask is not None) else None
            out.append({
                "symbol": str(sym),
                "underlying": underlying,
                "type": parsed["type"],
                "strike": parsed["strike"],
                "expiration": parsed["expiration"],
                "bid": bid,
                "ask": ask,
                "mid": mid,
                "last": float(last) if last not in (None, 0) else None,
            })
        return out

    # ================================================================== #
    # Internal helpers
    # ================================================================== #

    def _parse_position(self, position: Any, symbol: str) -> dict:
        """Normalise one position object to the documented dict shape."""
        self._require_attrs(
            position, ("symbol", "qty", "avg_entry_price"), symbol, "position"
        )
        return {
            "symbol": str(position.symbol),
            "qty": float(position.qty),
            "avg_entry_px": float(position.avg_entry_price),
            "market_value": self._float_or_zero(getattr(position, "market_value", None)),
            "unrealized_pl": self._float_or_zero(getattr(position, "unrealized_pl", None)),
            "asset_class": self._enum_str(getattr(position, "asset_class", None)),
            "lastday_px": self._opt_float(getattr(position, "lastday_price", None)),  # prior close → day %
        }

    def _parse_asset(self, asset: Any) -> dict:
        """Normalise one asset object to the documented dict shape."""
        return {
            "symbol": str(getattr(asset, "symbol", "")),
            "name": getattr(asset, "name", None) or "",
            "tradable": bool(getattr(asset, "tradable", False)),
            "shortable": bool(getattr(asset, "shortable", False)),
            "easy_to_borrow": bool(getattr(asset, "easy_to_borrow", False)),
            "marginable": bool(getattr(asset, "marginable", False)),
            "fractionable": bool(getattr(asset, "fractionable", False)),
        }

    def _parse_order(self, order: Any) -> dict:
        """Normalise one order object to key fields (read-only view)."""
        return {
            "id": str(getattr(order, "id", "")),
            "client_order_id": getattr(order, "client_order_id", None),
            "symbol": getattr(order, "symbol", None),
            "qty": self._opt_float(getattr(order, "qty", None)),
            "filled_qty": self._opt_float(getattr(order, "filled_qty", None)),
            "side": self._enum_str(getattr(order, "side", None)),
            "type": self._enum_str(getattr(order, "order_type", None)),
            "status": self._enum_str(getattr(order, "status", None)),
            "limit_price": self._opt_float(getattr(order, "limit_price", None)),
            "filled_avg_price": self._opt_float(getattr(order, "filled_avg_price", None)),
            "submitted_at": self._iso(getattr(order, "submitted_at", None)),
            "filled_at": self._iso(getattr(order, "filled_at", None)),
        }

    def _parse_bar(self, bar: Any, symbol: str) -> dict:
        """Validate then normalise one bar object."""
        self._require_attrs(
            bar, ("timestamp", "open", "high", "low", "close", "volume"), symbol, "bars"
        )
        return self._bar_to_dict(bar)


    def _bar_to_dict(self, bar: Any) -> dict:
        """Normalise a bar object without validation (None-safe fields)."""
        timestamp = getattr(bar, "timestamp", None)
        return {
            "time": self._iso(timestamp),
            "open": self._opt_float(getattr(bar, "open", None)),
            "high": self._opt_float(getattr(bar, "high", None)),
            "low": self._opt_float(getattr(bar, "low", None)),
            "close": self._opt_float(getattr(bar, "close", None)),
            "volume": self._opt_float(getattr(bar, "volume", None)),
        }

    @staticmethod
    def _timeframe(name: str) -> TimeFrame:
        """Map a timeframe string to an SDK :class:`TimeFrame`."""
        table = _timeframe_table()
        if name not in table:
            raise ValueError(
                f"unknown timeframe {name!r}; valid: {', '.join(table)}"
            )
        return table[name]

    @staticmethod
    def _rows_for(result: Any, symbol: str) -> list:
        """Extract a per-symbol row list from a *Set response (or [])."""
        data = getattr(result, "data", None)
        if isinstance(data, dict):
            return data.get(symbol, [])
        if isinstance(result, dict):
            return result.get(symbol, [])
        return []

    @staticmethod
    def _extract(result: Any, symbol: str, method: str) -> Any:
        """Return ``result[symbol]`` from a ``{symbol: obj}`` mapping."""
        if not isinstance(result, dict) or symbol not in result:
            actual = tuple(result.keys()) if isinstance(result, dict) else (type(result).__name__,)
            raise AlpacaResponseError(symbol, method, (symbol,), actual)
        return result[symbol]

    def _require_attrs(
        self, obj: Any, required: Iterable[str], symbol: str, method: str
    ) -> None:
        """Raise :class:`AlpacaResponseError` if any required attr is missing/None."""
        required = tuple(required)
        if any(getattr(obj, attr, None) is None for attr in required):
            raise AlpacaResponseError(symbol, method, required, self._obj_fields(obj))

    @staticmethod
    def _obj_fields(obj: Any) -> tuple[str, ...]:
        """Best-effort list of the field names available on an SDK object."""
        model_fields = getattr(type(obj), "model_fields", None)
        if model_fields:
            return tuple(model_fields)
        if hasattr(obj, "__dict__"):
            return tuple(vars(obj))
        return (type(obj).__name__,)

    @staticmethod
    def _float_or_zero(value: Any) -> float:
        """Cast to float; ``None`` becomes ``0.0`` (keeps the float contract)."""
        return 0.0 if value is None else float(value)

    @staticmethod
    def _opt_float(value: Any) -> float | None:
        """Cast to float, passing ``None`` through unchanged."""
        return None if value is None else float(value)

    @staticmethod
    def _opt_int(value: Any) -> int | None:
        """Cast to int, passing ``None`` through unchanged."""
        return None if value is None else int(value)

    @staticmethod
    def _to_plain(obj: Any) -> Any:
        """Convert a pydantic SDK record to a plain dict; passthrough otherwise."""
        dump = getattr(obj, "model_dump", None)
        return dump() if callable(dump) else obj

    @staticmethod
    def _enum_str(value: Any) -> str | None:
        """Render an enum (or plain value) as a string, ``None`` passthrough."""
        if value is None:
            return None
        return str(getattr(value, "value", value))

    @staticmethod
    def _iso(value: Any) -> str | None:
        """Render a datetime/date/time as ISO, ``None`` passthrough."""
        if value is None:
            return None
        return value.isoformat() if hasattr(value, "isoformat") else str(value)

    @staticmethod
    def _parse_dt(value: str) -> datetime:
        """Parse an ISO date/datetime string into a ``datetime``."""
        return datetime.fromisoformat(value)

    @staticmethod
    def _parse_date(value: str):
        """Parse an ISO date string into a ``date``."""
        return datetime.fromisoformat(value).date()

    @staticmethod
    def _is_not_found(exc: APIError) -> bool:
        """Whether an Alpaca error means the resource simply does not exist."""
        return (
            getattr(exc, "status_code", None) == 404
            or "does not exist" in str(exc).lower()
        )
