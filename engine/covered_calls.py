"""Covered-call overlay — strike selection and the monthly write/close plan (Phase 4.1).

Given the (post-equity-trade) held book, decide which covered calls to **write** and which
existing calls to **close**, at the monthly rebalance (DECISIONS D31 — one monthly cadence,
no mid-cycle DTE roll). This module is the **pure planner**: it turns real option chains +
holdings into a list of option orders. The I/O — fetching chains, estimating IV, submitting
via the broker, and writing ``options_lifecycle`` — lands in later increments (4.2/4.3).

**Strike selection (the load-bearing logic).** Alpaca's indicative feed gives real chains
(strikes, bid/ask, expiries) but **no greeks**, so we compute delta ourselves: estimate IV
from trailing realized vol (caller-supplied), price ``bs_call_delta`` for every strike in the
30-45 DTE window, and pick the one **nearest the target delta (0.30)**. The premium is taken
from the **real chain mid** (DECISIONS — sell-to-open limit at mid), not a modeled number.

**Partial coverage (DECISIONS D32).** A standard contract is 100 shares and single-name mini
(10-share) options were delisted ~2014, so a position must hold **≥ 100 shares** to be
covered. Smaller positions (and names with no liquid strike) are returned as ``skipped`` and
left bare — the high-priced tail of a ~$200k / 19-name book.

Pure functions only here (:func:`select_strike`, :func:`contracts_for`,
:func:`build_write_plan`, :func:`build_close_plan`) — unit-tested on synthetic chains.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Mapping, Optional, Sequence

import numpy as np
import pandas as pd

from engine import factors, options
from engine.alpaca_client import AlpacaAPIError
from engine.logger import get_logger

log = get_logger(__name__)

_CONTRACT_SHARES = 100                     # standard equity-option multiplier
_TRADING_DAYS = 252
# OCC: ROOT + YYMMDD + (C|P) + strike(×1000, 8 digits).
_OCC_RE = re.compile(r"^([A-Z0-9]+?)(\d{6})([CP])(\d{8})$")


@dataclass
class CoveredCallOrder:
    """One option order in the overlay plan.

    ``action`` is ``"sell_to_open"`` (write) or ``"buy_to_close"`` (close). ``limit_price``
    is the per-share option price (the chain mid); ``premium`` is the cash for the order
    (contracts × 100 × price), informational for the lifecycle log.
    """
    action: str
    option_symbol: str
    underlying: str
    contracts: int
    limit_price: Optional[float] = None
    strike: Optional[float] = None
    expiration: Optional[str] = None
    delta: Optional[float] = None
    premium: float = 0.0


def _to_date(value) -> date:
    return value if isinstance(value, date) else date.fromisoformat(str(value)[:10])


def _occ_underlying(symbol: str) -> str:
    """Root (underlying) of an OCC option symbol, or the symbol itself if unparseable."""
    m = _OCC_RE.match(str(symbol))
    return m.group(1) if m else str(symbol)


def _occ_is_call(symbol: str) -> bool:
    """Whether an OCC symbol is a call (``C`` in the type position)."""
    m = _OCC_RE.match(str(symbol))
    return bool(m) and m.group(3) == "C"


def contracts_for(shares: float, contract_size: int = _CONTRACT_SHARES) -> int:
    """Whole writable contracts for a holding: ``floor(shares / 100)`` (0 if < 1 contract)."""
    return int(shares) // int(contract_size)


def select_strike(
    calls: Sequence[Mapping],
    *,
    spot: float,
    iv: float,
    target_delta: float,
    as_of: date,
    min_dte: int,
    max_dte: int,
) -> Optional[dict]:
    """Pick the call nearest ``target_delta`` within the DTE window, from a real chain.

    ``calls`` are chain contracts (``strike``, ``expiration``, ``mid``, ``symbol`` …). Only
    contracts with a usable mid and an expiry in ``[min_dte, max_dte]`` days are considered;
    delta is computed via Black-Scholes from ``spot``/``iv`` (the indicative feed has no
    greeks). Returns the chosen contract dict with a computed ``delta``, or ``None`` if none
    qualify (illiquid / nothing in the window → the name goes uncovered).
    """
    best = None
    best_gap = None
    for c in calls:
        strike, exp, mid = c.get("strike"), c.get("expiration"), c.get("mid")
        if strike is None or exp is None or not mid:
            continue
        dte = (_to_date(exp) - as_of).days
        if not (min_dte <= dte <= max_dte):
            continue
        T = max(dte, 1) / 365.0
        delta = float(options.bs_call_delta(spot, float(strike), T, iv))
        gap = abs(delta - target_delta)
        if best_gap is None or gap < best_gap:
            best, best_gap = {**dict(c), "delta": delta}, gap
    return best


def build_write_plan(
    holdings_shares: Mapping[str, float],
    chains: Mapping[str, Sequence[Mapping]],
    spots: Mapping[str, float],
    ivs: Mapping[str, float],
    *,
    settings,
    as_of: date,
) -> tuple[list[CoveredCallOrder], list[dict]]:
    """Plan the covered-call writes for the held book; return ``(writes, skipped)``.

    For each holding with ≥ 1 contract (100 sh) and a liquid 0.30-delta strike in the DTE
    window, emit a *sell-to-open* limit (at the chain mid) for ``floor(shares/100)``
    contracts. Holdings below 100 shares, or without a spot / IV / chain / suitable strike,
    are returned in ``skipped`` with a reason (partial coverage, D32).
    """
    cc = settings.covered_calls
    writes: list[CoveredCallOrder] = []
    skipped: list[dict] = []
    for sym, shares in holdings_shares.items():
        n = contracts_for(shares)
        if n < 1:
            skipped.append({"symbol": sym, "reason": f"{int(shares)} sh < 100 (one contract)"})
            continue
        spot, iv, chain = spots.get(sym), ivs.get(sym), chains.get(sym) or []
        if not spot or iv is None or not chain:
            skipped.append({"symbol": sym, "reason": "no spot / iv / chain"})
            continue
        c = select_strike(chain, spot=float(spot), iv=float(iv), target_delta=cc.target_delta,
                          as_of=as_of, min_dte=cc.min_dte_entry, max_dte=cc.max_dte_entry)
        if c is None:
            skipped.append({"symbol": sym, "reason": "no liquid strike in DTE window"})
            continue
        mid = float(c["mid"])
        writes.append(CoveredCallOrder(
            action="sell_to_open", option_symbol=str(c["symbol"]), underlying=sym,
            contracts=n, limit_price=round(mid, 2), strike=float(c["strike"]),
            expiration=str(c["expiration"]), delta=round(float(c["delta"]), 3),
            premium=round(n * _CONTRACT_SHARES * mid, 2)))
    log.info("covered-call write plan", extra={"writes": len(writes), "skipped": len(skipped)})
    return writes, skipped


def build_close_plan(open_call_positions: Sequence[Mapping]) -> list[CoveredCallOrder]:
    """Plan a *buy-to-close* for every currently-open short call (the monthly close-all).

    ``open_call_positions`` are option positions from Alpaca (qty negative for a short call);
    ``contracts = |qty|``. ``limit_price`` is taken from a ``mid`` on the position when the
    caller has attached a fresh quote, else left ``None`` for the I/O layer to fill.
    """
    closes: list[CoveredCallOrder] = []
    for p in open_call_positions:
        contracts = int(abs(float(p.get("qty", 0))))
        if contracts < 1:
            continue
        sym = str(p["symbol"])
        mid = p.get("mid")
        closes.append(CoveredCallOrder(
            action="buy_to_close", option_symbol=sym,
            underlying=p.get("underlying") or _occ_underlying(sym),
            contracts=contracts, limit_price=round(float(mid), 2) if mid else None))
    log.info("covered-call close plan", extra={"closes": len(closes)})
    return closes


# ====================================================================== #
# I/O — chains, IV, positions, submission, lifecycle (increment 4.3)
# ====================================================================== #
def fetch_chains(client, underlyings: Sequence[str], as_of: date, *, min_dte: int,
                 max_dte: int) -> dict[str, list]:
    """Fetch each name's call chain within the DTE window. Failures degrade to ``[]``."""
    gte = (as_of + timedelta(days=min_dte)).isoformat()
    lte = (as_of + timedelta(days=max_dte)).isoformat()
    chains: dict[str, list] = {}
    for u in underlyings:
        try:
            chains[u] = client.option_chain(u, option_type="call",
                                             expiration_gte=gte, expiration_lte=lte)
        except Exception as exc:  # noqa: BLE001 — one bad name shouldn't sink the batch
            log.warning("option chain fetch failed", extra={"underlying": u, "error": str(exc)})
            chains[u] = []
    return chains


def estimate_ivs(price_panel: pd.DataFrame, underlyings: Sequence[str], as_of: date,
                 *, window: int) -> dict[str, float]:
    """Per-name IV estimate = annualized trailing realized vol (the D27 basis)."""
    ann = factors.realized_vol(price_panel, as_of, window) * np.sqrt(_TRADING_DAYS)
    out: dict[str, float] = {}
    for s in underlyings:
        v = ann.get(s)
        if v is not None and np.isfinite(v):
            out[s] = float(v)
    return out


def _spots_from_panel(price_panel: pd.DataFrame, names: Sequence[str], as_of: date) -> dict[str, float]:
    row = price_panel.loc[: pd.Timestamp(as_of)].iloc[-1]
    return {s: float(row[s]) for s in names if s in row.index and np.isfinite(row.get(s, np.nan))}


def open_call_positions(client) -> list[dict]:
    """Currently-open short call positions from Alpaca, each with an estimated ``mid``.

    Filters to option positions that are calls held short (``qty < 0``). ``mid`` is derived
    from the position's market value (``|market_value| / (contracts × 100)``) so the close
    plan has a limit price without a separate quote call.
    """
    out = []
    for p in client.all_positions():
        sym = str(p.get("symbol", ""))
        is_option = str(p.get("asset_class") or "").endswith("option") or _OCC_RE.match(sym)
        if not (is_option and _occ_is_call(sym) and float(p.get("qty", 0)) < 0):
            continue
        qty = abs(float(p["qty"]))
        mv = p.get("market_value")
        mid = abs(float(mv)) / (qty * _CONTRACT_SHARES) if mv else None
        out.append({**p, "underlying": _occ_underlying(sym), "type": "call", "mid": mid})
    return out


def write_calls(client, broker, db_engine, holdings_shares: Mapping[str, float], *,
                settings, as_of: date, price_panel: pd.DataFrame, alert=None):
    """Write covered calls on the held book: chains → IV → plan → sell-to-open → lifecycle.

    Returns ``(submitted, skipped)``. A per-name rejection is logged + alerted and skipped;
    it does not abort the batch.
    """
    cc = settings.covered_calls
    names = list(holdings_shares)
    chains = fetch_chains(client, names, as_of, min_dte=cc.min_dte_entry, max_dte=cc.max_dte_entry)
    spots = _spots_from_panel(price_panel, names, as_of)
    ivs = estimate_ivs(price_panel, names, as_of, window=settings.covariance.estimation_window_days)
    writes, skipped = build_write_plan(holdings_shares, chains, spots, ivs, settings=settings, as_of=as_of)

    submitted = []
    for w in writes:
        coid = f"cc:{as_of.isoformat()}:{w.underlying}:open"
        try:
            resp = broker.submit_option_order(w.option_symbol, w.contracts, "sell",
                                              position_intent="sell_to_open", order_type="limit",
                                              limit_price=w.limit_price, client_order_id=coid)
        except AlpacaAPIError as exc:
            log.error("covered-call write rejected", extra={"underlying": w.underlying, "error": str(exc)})
            if alert:
                alert(f"covered-call write rejected {w.underlying}: {exc}")
            continue
        _log_lifecycle(db_engine, "write", w)
        submitted.append(w)
    log.info("covered calls written", extra={"written": len(submitted), "skipped": len(skipped)})
    return submitted, skipped


def close_calls(client, broker, db_engine, *, as_of: date | None = None, alert=None):
    """Close every open short call (monthly close-all): buy-to-close → lifecycle."""
    closes = build_close_plan(open_call_positions(client))
    submitted = []
    stamp = (as_of.isoformat() if as_of else "now")
    for o in closes:
        coid = f"cc:{stamp}:{o.underlying}:close"
        try:
            resp = broker.submit_option_order(
                o.option_symbol, o.contracts, "buy", position_intent="buy_to_close",
                order_type=("limit" if o.limit_price else "market"),
                limit_price=o.limit_price, client_order_id=coid)
        except AlpacaAPIError as exc:
            log.error("covered-call close rejected", extra={"underlying": o.underlying, "error": str(exc)})
            if alert:
                alert(f"covered-call close rejected {o.underlying}: {exc}")
            continue
        _log_lifecycle(db_engine, "close", o)
        submitted.append(o)
    log.info("covered calls closed", extra={"closed": len(submitted)})
    return submitted


def _log_lifecycle(db_engine, event_type: str, order: CoveredCallOrder) -> None:
    """Append a row to ``options_lifecycle`` (premium +collected on write, −paid on close)."""
    if db_engine is None:
        return
    from sqlalchemy import insert
    from engine.db import options_lifecycle
    if event_type == "write":
        premium = order.premium
    else:  # close — modeled cost from the limit (the audit log; real fill refines)
        premium = -((order.limit_price or 0.0) * order.contracts * _CONTRACT_SHARES)
    with db_engine.begin() as conn:
        conn.execute(insert(options_lifecycle).values(
            ts=datetime.now(timezone.utc), event_type=event_type, underlying=order.underlying,
            option_symbol=order.option_symbol, strike=order.strike,
            expiration=_to_date(order.expiration) if order.expiration else None,
            delta=order.delta, contracts=order.contracts, premium=round(premium, 2)))
