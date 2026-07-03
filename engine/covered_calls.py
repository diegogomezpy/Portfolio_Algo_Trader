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
import time
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta, timezone
from typing import Callable, Mapping, Optional, Sequence

import numpy as np
import pandas as pd

from engine import factors, options, risk
from engine.alpaca_client import AlpacaAPIError
from engine.logger import get_logger

log = get_logger(__name__)

_CONTRACT_SHARES = 100                     # standard equity-option multiplier
_TRADING_DAYS = 252
# OCC: ROOT + YYMMDD + (C|P) + strike(×1000, 8 digits).
_OCC_RE = re.compile(r"^([A-Z0-9]+?)(\d{6})([CP])(\d{8})$")
# Order states from which no further fill is possible (the option fill poll stops here).
_TERMINAL = {"filled", "canceled", "cancelled", "rejected", "expired", "done_for_day"}


@dataclass
class OptionChase:
    """Knobs for executing option orders — fill-tracking, and optionally chasing the touch (4.7).

    Mirrors the equity chaser (:mod:`engine.execute`). With ``touch`` set, each round re-prices
    the unfilled residual to the option **touch** — ``touch(option_symbol, side)`` returns the
    bid for a sell-to-open / the ask for a buy-to-close — and re-submits, polling
    ``poll_attempts`` × ``poll_interval_s`` per round, repeating until the contracts fill or the
    session comes within ``close_buffer_s`` of ``market_close`` (``max_rounds`` cap). With
    ``touch`` ``None`` it is a single passive pass at the planned mid (still log-on-fill).
    ``sleep``/``now`` are injectable so tests run instantly; ``None`` in place of the whole
    object is the single-pass default with a real ``time.sleep``.
    """
    touch: Optional[Callable[[str, str], Optional[float]]] = None
    now: Optional[Callable[[], datetime]] = None
    market_close: Optional[datetime] = None
    close_buffer_s: float = 300.0
    max_rounds: int = 40
    poll_attempts: int = 30
    poll_interval_s: float = 2.0
    sleep: Callable[[float], None] = time.sleep
    # Fill state source — the live order feed (instant) when set, else broker.get_order (REST).
    order_state: Optional[Callable[[str], dict]] = None
    # Write guard: skip a covered-call *write* when the current bid is below this fraction of the
    # planned chain mid — don't dump a call into a lowball/junk bid (option analog of the equity
    # phantom-spread guard). Option spreads are naturally wide, so this keys off the bid-vs-mid
    # give-up, not a raw spread %. Writes only; closes still cross to get the risk off.
    min_bid_frac: float = 0.7


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


def _occ_expiration(symbol: str) -> Optional[date]:
    """Expiration date parsed from an OCC symbol's YYMMDD field (``None`` if unparseable)."""
    m = _OCC_RE.match(str(symbol))
    if not m:
        return None
    ymd = m.group(2)
    return date(2000 + int(ymd[:2]), int(ymd[2:4]), int(ymd[4:6]))


def contracts_for(shares: float, contract_size: int = _CONTRACT_SHARES) -> int:
    """Whole writable contracts for a holding: ``floor(shares / 100)`` (0 if < 1 contract)."""
    return int(shares) // int(contract_size)


def overlay_mode(settings) -> str:
    """The overlay mode: ``"index"`` (SPY beta-overwrite spread) or ``"per_name"`` (classic
    covered calls). THE single source of truth for the default — config/settings.yaml sets it
    explicitly (``index``), so the fallback here only decides for bare test fixtures, where
    ``"per_name"`` preserves the legacy expectations. Callers must never inline their own
    ``getattr(..., "overlay_mode", ...)`` default (the audit found two with *different* defaults).
    """
    return str(getattr(settings.covered_calls, "overlay_mode", "per_name"))


def select_strike(
    calls: Sequence[Mapping],
    *,
    spot: float,
    iv: float,
    target_delta: float,
    as_of: date,
    min_dte: int,
    max_dte: int,
    expiration: str | None = None,
) -> Optional[dict]:
    """Pick the call nearest ``target_delta`` within the DTE window, from a real chain.

    ``calls`` are chain contracts (``strike``, ``expiration``, ``mid``, ``symbol`` …). Only
    contracts with a usable mid and an expiry in ``[min_dte, max_dte]`` days are considered;
    delta is computed via Black-Scholes from ``spot``/``iv`` (the indicative feed has no
    greeks). ``expiration`` (ISO) restricts to a single expiry — used to pin a spread's long
    wing to the short leg's expiration (a *vertical*, not a diagonal). Returns the chosen
    contract dict with a computed ``delta``, or ``None`` if none qualify.
    """
    best = None
    best_gap = None
    for c in calls:
        strike, exp, mid = c.get("strike"), c.get("expiration"), c.get("mid")
        if strike is None or exp is None or not mid:
            continue
        if expiration is not None and str(exp) != str(expiration):
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


def _execute_option_leg(broker, db_engine, orders: Sequence[CoveredCallOrder], *, side: str,
                        position_intent: str, event: str, as_of: date,
                        final_market: bool = False, chase: OptionChase | None = None,
                        alert=None) -> list[CoveredCallOrder]:
    """Submit option ``orders`` (all one ``side``/``intent``/``event``), track to fill, and log
    ``options_lifecycle`` **only for contracts that actually fill — at the real fill price**.

    Logging on the *fill* (not the submission) is what keeps the premium ledger truthful: a
    write that never fills leaves no row, so the dashboard's premium and covered-call book
    (sourced from live positions) can never show premium that wasn't collected.

    ``chase.touch`` ``None`` → one passive limit at the planned mid, poll, cancel the remainder
    (the pre-chase behaviour, now log-on-fill). ``touch`` set → each round re-prices the unfilled
    residual to the touch (bid to sell-to-open / ask to buy-to-close) and re-submits until the
    contracts fill or the session nears ``market_close``. ``final_market`` then sweeps any
    residual at market — used for **closes** (get the risk off); writes leave it uncovered (an
    income miss, not a breach) for the next cycle.

    Returns the filled orders, each with ``contracts``/``limit_price``/``premium`` set to the
    actually-filled quantity and average fill price.
    """
    if not orders:
        return []
    chase = chase or OptionChase()                       # single-pass default (real time.sleep)
    do_chase = chase.touch is not None
    read = chase.order_state or broker.get_order         # live feed (instant) else REST
    stamp = as_of.isoformat() if as_of else "now"        # coid namespace (daily safety pass omits as_of)
    by_sym = {o.option_symbol: o for o in orders}
    filled = {o.option_symbol: 0 for o in orders}
    paid = {o.option_symbol: 0.0 for o in orders}        # Σ fill_px × contracts × 100 (cash)
    active = {o.option_symbol for o in orders}

    is_write = side == "sell"

    def _price(sym: str, o: CoveredCallOrder, *, force_market: bool) -> tuple[Optional[str], Optional[float]]:
        # ``(None, None)`` means "skip this name this round" — only writes skip (an income miss,
        # not a breach); closes never skip (we want the risk off), falling back to market.
        if force_market:
            return "market", None
        if not do_chase:                                 # single passive pass at the planned mid
            return ("limit", o.limit_price) if o.limit_price else ("market", None)
        try:
            px = chase.touch(sym, side)                  # bid (sell) / ask (buy) — cross to the touch
        except AlpacaAPIError as exc:
            log.warning("option touch failed", extra={"symbol": sym, "error": str(exc)})
            return (None, None) if is_write else ("market", None)
        if not px:
            return (None, None) if is_write else ("market", None)
        if is_write and o.limit_price and float(px) < o.limit_price * chase.min_bid_frac:
            log.info("covered-call write skipped: bid below floor",
                     extra={"underlying": o.underlying, "bid": float(px), "planned_mid": o.limit_price})
            return None, None                            # junk bid — don't give away the premium
        return "limit", round(float(px), 2)

    def _round(tag: str, *, force_market: bool) -> None:
        live: list[tuple[str, str]] = []
        for sym in list(active):
            o = by_sym[sym]
            residual = o.contracts - filled[sym]
            if residual <= 0:
                active.discard(sym); continue
            otype, lp = _price(sym, o, force_market=force_market)
            if otype is None:                            # write guard tripped — leave uncovered, retry next round
                continue
            coid = f"cc:{stamp}:{o.underlying}:{event}:{tag}"
            try:
                resp = broker.submit_option_order(sym, residual, side, position_intent=position_intent,
                                                  order_type=otype, limit_price=lp, client_order_id=coid)
            except AlpacaAPIError as exc:
                log.error("covered-call leg rejected",
                          extra={"event": event, "underlying": o.underlying, "error": str(exc)})
                if alert:
                    alert(f"covered-call {event} rejected {o.underlying}: {exc}")
                active.discard(sym); continue
            live.append((resp["id"], sym))

        open_ids = {oid for oid, _ in live}
        for _ in range(chase.poll_attempts):
            if not open_ids:
                break
            chase.sleep(chase.poll_interval_s)
            for oid, _sym in live:
                if oid not in open_ids:
                    continue
                try:
                    st = read(oid)
                except AlpacaAPIError as exc:
                    log.warning("option poll failed; will retry", extra={"id": oid, "error": str(exc)})
                    continue
                if st is None:
                    continue
                if st["status"] in _TERMINAL:
                    open_ids.discard(oid)

        for oid, sym in live:
            if oid in open_ids:
                try:
                    broker.cancel_order(oid)
                except AlpacaAPIError as exc:
                    log.warning("option cancel failed", extra={"id": oid, "error": str(exc)})
            try:
                st = read(oid)
            except AlpacaAPIError as exc:   # leave to the position-sourced book; just don't log a phantom
                log.warning("option round-end read failed", extra={"id": oid, "error": str(exc)})
                continue
            if st is None:
                continue
            fq = int(st.get("filled_qty") or 0)
            if fq > 0:
                paid[sym] += float(st.get("filled_avg_price") or 0.0) * fq * _CONTRACT_SHARES
                filled[sym] += fq
            if filled[sym] >= by_sym[sym].contracts:
                active.discard(sym)

    if not do_chase:
        _round("single", force_market=False)
    else:
        now = chase.now or (lambda: datetime.now(timezone.utc))
        rnd = 0
        while active and rnd < chase.max_rounds:
            if chase.market_close is not None and now() >= chase.market_close - timedelta(seconds=chase.close_buffer_s):
                break
            rnd += 1
            _round(f"r{rnd}", force_market=False)
        if active and final_market:
            _round("final", force_market=True)

    done: list[CoveredCallOrder] = []
    for sym, o in by_sym.items():
        f = filled[sym]
        if f <= 0:
            log.warning("covered-call leg unfilled", extra={"event": event, "underlying": o.underlying})
            if alert:
                alert(f"covered-call {event} unfilled {o.underlying} ({o.contracts} contract(s))")
            continue
        avg_px = round(paid[sym] / (f * _CONTRACT_SHARES), 2)
        rec = replace(o, contracts=f, limit_price=avg_px, premium=round(paid[sym], 2))
        _log_lifecycle(db_engine, event, rec)
        done.append(rec)
    return done


def write_calls(client, broker, db_engine, holdings_shares: Mapping[str, float], *,
                settings, as_of: date, price_panel: pd.DataFrame, chase: OptionChase | None = None,
                alert=None):
    """Write covered calls on the held book: chains → IV → plan → sell-to-open → lifecycle.

    With ``chase`` set, each write is chased to the bid until it fills or the session nears the
    close (writes never force a market order — an unfilled write just leaves the name uncovered
    for the cycle). The lifecycle row is written **only on a real fill**.

    Returns ``(submitted, skipped)``. A per-name rejection is logged + alerted and skipped;
    it does not abort the batch.
    """
    cc = settings.covered_calls
    names = list(holdings_shares)
    chains = fetch_chains(client, names, as_of, min_dte=cc.min_dte_entry, max_dte=cc.max_dte_entry)
    spots = _spots_from_panel(price_panel, names, as_of)
    ivs = estimate_ivs(price_panel, names, as_of, window=settings.covariance.estimation_window_days)
    writes, skipped = build_write_plan(holdings_shares, chains, spots, ivs, settings=settings, as_of=as_of)

    # Defense in depth (D32): re-derive coverage on the plan before anything reaches the broker.
    # build_write_plan only writes floor(shares/100) so this is structurally satisfied, but the
    # independent gate guarantees no naked / expired call is ever submitted (a bug upstream stops
    # here, not at the market) — the same check the pre-trade risk gate runs.
    cover_fail = risk.check_covered_call_coverage(
        [{"underlying": w.underlying, "contracts": w.contracts, "expiration": w.expiration}
         for w in writes], holdings_shares, as_of)
    if cover_fail:
        log.error("covered-call coverage gate blocked writes", extra={"failures": cover_fail})
        if alert:
            alert(f"covered-call coverage gate blocked: {'; '.join(cover_fail)}")
        return [], skipped

    submitted = _execute_option_leg(broker, db_engine, writes, side="sell",
                                    position_intent="sell_to_open", event="write", as_of=as_of,
                                    final_market=False, chase=chase, alert=alert)
    log.info("covered calls written", extra={"written": len(submitted), "skipped": len(skipped)})
    return submitted, skipped


def portfolio_beta(weights: Mapping[str, float], price_panel: pd.DataFrame, as_of: date,
                   *, window: int, market: str = "SPY") -> Optional[float]:
    """Weighted portfolio beta ``β_p = Σ wᵢ·βᵢ / Σ wᵢ`` over names with a computable β vs
    ``market``. ``weights`` = each name's share of the book (any positive scale; renormalized).
    Reuses the same per-name market beta as the low-beta factor. ``None`` if nothing usable."""
    betas = factors.market_beta(price_panel, as_of, window, market)
    num = den = 0.0
    for sym, w in weights.items():
        b = betas.get(sym)
        if b is not None and np.isfinite(b) and w:
            num += float(w) * float(b)
            den += float(w)
    return (num / den) if den else None


def write_index_overwrite(client, broker, db_engine, holdings_shares: Mapping[str, float], *,
                          settings, as_of: date, price_panel: pd.DataFrame,
                          chase: OptionChase | None = None, alert=None):
    """Portfolio-level SPY **call-spread overwrite** sized to the book's market beta (replaces
    per-name covered calls for low-beta books whose single-name options are illiquid).

    Sells ``N`` SPY vertical call spreads (short at ``target_delta``, long wing at ``wing_delta``,
    same expiry) where ``N ≈ coverage · β_p · gross_equity / (100 · spot)`` — beta-sizing so the
    short-leg market exposure matches the book's (low-beta ⇒ fewer spreads). Value-weighted β_p
    and gross equity come from ``holdings_shares`` × the panel spot. A **spread, not a naked call**
    — defined-risk, tradable at Alpaca's spread tier (uncovered short calls are not permitted).
    The lifecycle row records the **net credit** collected. Returns ``(submitted, skipped, info)``.
    """
    cc = settings.covered_calls
    market = getattr(settings.factors, "beta_market", "SPY")
    coverage = float(getattr(cc, "overwrite_coverage", 1.0))
    names = [s for s, sh in holdings_shares.items() if sh]
    spots = _spots_from_panel(price_panel, [*names, market], as_of)
    spot = spots.get(market)
    mv = {s: float(holdings_shares[s]) * spots[s] for s in names if s in spots}
    gross_equity = sum(mv.values())
    bp = portfolio_beta(mv, price_panel, as_of, window=settings.factors.beta_window, market=market)
    info = {"beta_p": round(bp, 3) if bp else bp, "market": market, "coverage": coverage,
            "gross": round(gross_equity) if gross_equity else 0, "contracts": 0}
    if not bp or bp <= 0 or not gross_equity or not spot:
        log.info("index overwrite skipped: no beta / exposure / spot", extra=info)
        return [], [{"reason": "no beta / exposure / spot"}], info
    n = int((coverage * bp * gross_equity) // (spot * _CONTRACT_SHARES))
    info["contracts"] = n
    if n < 1:
        return [], [{"reason": "below one contract"}], info
    chain = fetch_chains(client, [market], as_of, min_dte=cc.min_dte_entry, max_dte=cc.max_dte_entry).get(market) or []
    iv = estimate_ivs(price_panel, [market], as_of, window=settings.covariance.estimation_window_days).get(market)
    if iv is None or not chain:
        return [], [{"reason": "no chain / iv for market"}], info
    # Vertical call spread (defined risk, tradable at Alpaca's spread tier — naked calls are not
    # permitted): sell the target-delta short, buy a further-OTM long wing at the SAME expiry.
    short = select_strike(chain, spot=float(spot), iv=float(iv), target_delta=cc.target_delta,
                          as_of=as_of, min_dte=cc.min_dte_entry, max_dte=cc.max_dte_entry)
    if short is None:
        return [], [{"reason": "no short strike at target delta"}], info
    exp = str(short["expiration"])
    wing_delta = float(getattr(cc, "wing_delta", 0.15))
    long_leg = select_strike(chain, spot=float(spot), iv=float(iv), target_delta=wing_delta,
                             as_of=as_of, min_dte=cc.min_dte_entry, max_dte=cc.max_dte_entry,
                             expiration=exp)
    if long_leg is None or float(long_leg["strike"]) <= float(short["strike"]):
        return [], [{"reason": "no long wing above short at same expiry"}], info
    net = round(float(short["mid"]) - float(long_leg["mid"]), 2)
    info.update(expiration=exp, short_strike=float(short["strike"]), long_strike=float(long_leg["strike"]),
                short_delta=round(float(short["delta"]), 3), net_credit=net,
                premium=round(n * _CONTRACT_SHARES * net, 2))
    if net <= 0:
        return [], [{"reason": "non-positive net credit"}], info
    log.info("index spread overwrite plan", extra=info)
    coid = f"ovw:{as_of.isoformat()}:{market}"
    try:
        resp = broker.submit_option_spread(
            [(short["symbol"], "sell", "sell_to_open"), (long_leg["symbol"], "buy", "buy_to_open")],
            contracts=n, net_limit_price=net, client_order_id=coid)
    except AlpacaAPIError as exc:
        log.error("index spread overwrite rejected", extra={"error": str(exc)})
        if alert:
            alert(f"SPY spread overwrite rejected: {exc}")
        return [], [{"reason": f"rejected: {exc}"}], info
    oid = resp["id"]
    filled = int(float(resp.get("filled_qty") or 0))
    for _ in range(20):                                   # poll to fill (SPY spreads are liquid)
        if filled >= n or str(resp.get("status") or "").lower() in ("filled", "canceled", "rejected", "expired"):
            break
        time.sleep(2)
        try:
            resp = broker.get_order(oid)
        except AlpacaAPIError:
            break
        filled = int(float(resp.get("filled_qty") or 0))
    if filled < 1:
        log.info("index spread overwrite unfilled", extra={"id": oid, "status": resp.get("status")})
        return [], [{"reason": f"unfilled ({resp.get('status')})"}], info
    # Lifecycle: one row on the short leg carrying the NET credit collected (real cash income).
    order = CoveredCallOrder(action="sell_to_open", option_symbol=str(short["symbol"]), underlying=market,
                             contracts=filled, limit_price=net, strike=float(short["strike"]),
                             expiration=exp, delta=round(float(short["delta"]), 3),
                             premium=round(filled * _CONTRACT_SHARES * net, 2))
    _log_lifecycle(db_engine, "write", order)
    info["filled"] = filled
    log.info("index spread overwrite filled", extra=info)
    return [order], [], info


def index_overwrite_legs(client, market: str = "SPY") -> tuple[list[dict], list[dict]]:
    """The overlay spread's legs from live positions: ``(short_calls, long_calls)`` on ``market``.

    Each leg dict carries the position plus ``underlying``/``type``/``mid`` (mid derived from the
    position's market value, like :func:`open_call_positions`). Only calls on ``market`` count —
    the equity book holds no other options under the index overlay.
    """
    shorts: list[dict] = []
    longs: list[dict] = []
    for p in client.all_positions():
        sym = str(p.get("symbol", ""))
        m = _OCC_RE.match(sym)
        if not m or m.group(1) != market or m.group(3) != "C":
            continue
        qty = float(p.get("qty", 0))
        if not qty:
            continue
        mv = p.get("market_value")
        mid = abs(float(mv)) / (abs(qty) * _CONTRACT_SHARES) if mv else None
        rec = {**p, "underlying": market, "type": "call", "mid": mid}
        (shorts if qty < 0 else longs).append(rec)
    return shorts, longs


def close_index_overwrite(client, broker, db_engine, *, as_of: date | None = None,
                          market: str = "SPY", chase: OptionChase | None = None, alert=None):
    """Close the SPY call-spread overwrite — **buy-to-close the short leg FIRST, then
    sell-to-close the long wing**.

    The order is load-bearing: selling the long first would leave the short momentarily naked,
    which Alpaca rejects outright (error 40310000 — no uncovered tier). The short-leg close is
    a risk-off action (chases the ask, final market sweep); the long-leg sale is cleanup of a
    small residual (chases the bid, also swept at market so a stale quote can't strand it).
    Both legs log ``options_lifecycle`` rows on real fills — the buy negative (cash out), the
    sell positive (cash in). Returns the filled orders across both legs.
    """
    shorts, longs = index_overwrite_legs(client, market)
    closed = _submit_closes(broker, db_engine, build_close_plan(shorts),
                            as_of=as_of, event="close", chase=chase, alert=alert)
    sells = [CoveredCallOrder(
        action="sell_to_close", option_symbol=str(p["symbol"]), underlying=market,
        contracts=int(abs(float(p["qty"]))),
        limit_price=round(float(p["mid"]), 2) if p.get("mid") else None)
        for p in longs]
    closed += _execute_option_leg(broker, db_engine, sells, side="sell",
                                  position_intent="sell_to_close", event="close", as_of=as_of,
                                  final_market=True, chase=chase, alert=alert)
    log.info("index overwrite closed", extra={"market": market, "short_legs": len(shorts),
                                              "long_legs": len(longs), "filled": len(closed)})
    return closed


def _submit_closes(broker, db_engine, closes, *, as_of, event, chase: OptionChase | None = None,
                   alert=None):
    """Submit buy-to-close orders under one lifecycle ``event``, tracked to fill.

    Closes chase to the ask and end in a market sweep (``final_market``) — closing a short call
    removes risk, so we guarantee it goes through rather than leaving the position dangling.
    The lifecycle row is written only for contracts that actually close.
    """
    return _execute_option_leg(broker, db_engine, closes, side="buy",
                               position_intent="buy_to_close", event=event, as_of=as_of,
                               final_market=True, chase=chase, alert=alert)


def close_calls(client, broker, db_engine, *, as_of: date | None = None,
                chase: OptionChase | None = None, alert=None):
    """Close every open short call (monthly close-all): buy-to-close → lifecycle."""
    submitted = _submit_closes(broker, db_engine, build_close_plan(open_call_positions(client)),
                               as_of=as_of, event="close", chase=chase, alert=alert)
    log.info("covered calls closed", extra={"closed": len(submitted)})
    return submitted


def _log_lifecycle(db_engine, event_type: str, order: CoveredCallOrder) -> None:
    """Append a row to ``options_lifecycle``, signed by cash direction: **sell legs positive**
    (cash collected — a write, or selling the spread's long wing), **buy legs negative** (cash
    paid — buying a short call back).

    Called only from :func:`_execute_option_leg` with an order whose ``contracts``/``premium``/
    ``limit_price`` are the **actually-filled** quantity and average fill price, so the ledger
    records real cash, not the modeled mid.
    """
    if db_engine is None:
        return
    from sqlalchemy import insert
    from engine.db import options_lifecycle
    if str(order.action).startswith("sell"):
        premium = order.premium                          # +cash collected (real fill)
    else:  # buy_to_close / buy_to_open — cash paid (real fill avg × contracts × 100)
        premium = -((order.limit_price or 0.0) * order.contracts * _CONTRACT_SHARES)
    with db_engine.begin() as conn:
        conn.execute(insert(options_lifecycle).values(
            ts=datetime.now(timezone.utc), event_type=event_type, underlying=order.underlying,
            option_symbol=order.option_symbol, strike=order.strike,
            expiration=_to_date(order.expiration) if order.expiration else None,
            delta=order.delta, contracts=order.contracts, premium=round(premium, 2)))


# ====================================================================== #
# Daily safety checks — earnings-close, expiry force-close, rewrite (4.5)
# ====================================================================== #
_REWRITE_AFTER_DAYS = 5      # rewrite a call within this many days of the underlying's report


def needs_earnings_close(expiration: Optional[date], earnings_date: Optional[date],
                         as_of: date) -> bool:
    """True if an upcoming earnings date falls within the call's remaining life.

    Close the call *before* the announcement (DECISIONS D31, the one mid-cycle action):
    the earnings date is on/after ``as_of`` and on/before the call's ``expiration``.
    """
    if expiration is None or earnings_date is None:
        return False
    return as_of <= earnings_date <= expiration


def is_expiring(expiration: Optional[date], as_of: date, *, within_days: int = 0) -> bool:
    """True if the contract expires on/before ``as_of + within_days`` (force-close at DTE 0)."""
    return expiration is not None and expiration <= as_of + timedelta(days=within_days)


def next_earnings(dates: Sequence[date], as_of: date) -> Optional[date]:
    """The soonest earnings date on/after ``as_of`` (``None`` if none known)."""
    fut = sorted(d for d in dates if d >= as_of)
    return fut[0] if fut else None


def last_earnings(dates: Sequence[date], as_of: date) -> Optional[date]:
    """The most recent earnings date before ``as_of`` (``None`` if none known)."""
    past = sorted(d for d in dates if d < as_of)
    return past[-1] if past else None


def earnings_close_plan(open_calls: Sequence[Mapping], earnings_by_underlying: Mapping[str, date],
                        as_of: date) -> list[CoveredCallOrder]:
    """Buy-to-close orders for open calls whose underlying reports within the call's life."""
    facing = [c for c in open_calls
              if needs_earnings_close(_occ_expiration(str(c["symbol"])),
                                      earnings_by_underlying.get(c.get("underlying")
                                                                 or _occ_underlying(str(c["symbol"]))),
                                      as_of)]
    return build_close_plan(facing)


def expiry_close_plan(open_calls: Sequence[Mapping], as_of: date, *, within_days: int = 0) -> list[CoveredCallOrder]:
    """Buy-to-close orders for open calls at/under ``within_days`` to expiry (force-close)."""
    expiring = [c for c in open_calls if is_expiring(_occ_expiration(str(c["symbol"])), as_of,
                                                     within_days=within_days)]
    return build_close_plan(expiring)


def fetch_earnings_dates(underlyings: Sequence[str], *, fetch=None) -> dict[str, list]:
    """``{symbol: [earnings dates]}`` (past + upcoming). ``fetch(sym) -> list[date]`` is
    injectable; the default uses yfinance (Alpaca's feed has no forward earnings dates)."""
    if fetch is None:
        import yfinance as yf

        def fetch(sym):                                   # noqa: E306
            try:
                df = yf.Ticker(sym).get_earnings_dates(limit=12)
                return [d.date() for d in df.index]
            except Exception:                             # noqa: BLE001
                return []
    return {s: list(fetch(s) or []) for s in underlyings}


def _is_equity(position: Mapping) -> bool:
    sym = str(position.get("symbol", ""))
    return str(position.get("asset_class") or "us_equity").endswith("equity") and not _OCC_RE.match(sym)


def options_daily_check(client, broker, db_engine, *, settings, as_of: date,
                        price_panel: pd.DataFrame, earnings_fetch=None,
                        chase: OptionChase | None = None, alert=None) -> dict:
    """Daily overlay safety pass, dispatched by :func:`overlay_mode`.

    **index** — manage only the market (SPY) spread: force-close it (short leg first) once the
    short leg reaches expiry, and process assignments. NO earnings machinery and NO per-name
    rewrites — an index has no earnings, and under the index overlay every equity name is
    deliberately uncovered (the audit found the ungated per-name rewrite would have quietly
    started selling single-name calls on top of the SPY spread after any holding's earnings).

    **per_name** — the classic pass: force-close expiring calls, close calls into earnings and
    rewrite just-reported names (both gated on ``covered_calls.close_before_earnings``), then
    process assignments.

    Returns counts ``{expiry_closed, earnings_closed, rewritten, reentered}``. Non-rebalance
    days only; the monthly rebalance handles the full close-all + rewrite.
    """
    if overlay_mode(settings) == "index":
        return _index_daily_check(client, broker, db_engine, settings=settings, as_of=as_of,
                                  chase=chase, alert=alert)

    open_calls = open_call_positions(client)
    held = {str(p["symbol"]): float(p["qty"]) for p in client.all_positions() if _is_equity(p)}

    expiry = expiry_close_plan(open_calls, as_of)
    _submit_closes(broker, db_engine, expiry, as_of=as_of, event="force_close", chase=chase, alert=alert)

    # Earnings close + post-earnings rewrite are one feature (close before the print, restore
    # the call after), gated together on close_before_earnings (default on, matching the YAML).
    facing: list[CoveredCallOrder] = []
    rewritten: list = []
    if bool(getattr(settings.covered_calls, "close_before_earnings", True)):
        universe = sorted({c["underlying"] for c in open_calls} | set(held))
        earnings = fetch_earnings_dates(universe, fetch=earnings_fetch)
        nxt = {u: next_earnings(earnings.get(u, []), as_of) for u in universe}
        facing = earnings_close_plan(open_calls, nxt, as_of)
        _submit_closes(broker, db_engine, facing, as_of=as_of, event="earnings_close",
                       chase=chase, alert=alert)

        # Rewrite names that are held (≥1 contract), now uncovered, and reported very recently.
        closed_now = {o.underlying for o in expiry + facing}
        covered = {c["underlying"] for c in open_calls} - closed_now
        to_rewrite = {
            s: q for s, q in held.items()
            if contracts_for(q) >= 1 and s not in covered
            and last_earnings(earnings.get(s, []), as_of) is not None
            and (as_of - last_earnings(earnings.get(s, []), as_of)).days <= _REWRITE_AFTER_DAYS
        }
        if to_rewrite:
            rewritten, _ = write_calls(client, broker, db_engine, to_rewrite, settings=settings,
                                       as_of=as_of, price_panel=price_panel, chase=chase, alert=alert)

    asg = process_assignments(client, broker, db_engine, settings=settings, as_of=as_of, alert=alert)
    out = {"expiry_closed": len(expiry), "earnings_closed": len(facing),
           "rewritten": len(rewritten), "reentered": asg["reentered"]}
    log.info("options daily check", extra=out)
    return out


def _index_daily_check(client, broker, db_engine, *, settings, as_of: date,
                       chase: OptionChase | None = None, alert=None) -> dict:
    """The index-overlay daily pass: force-close the SPY spread at the short leg's expiry
    (short first — see :func:`close_index_overwrite`), then process assignments. Same return
    shape as the per-name pass so callers/alerts are mode-agnostic."""
    market = str(getattr(getattr(settings, "factors", None), "beta_market", "SPY"))
    shorts, _longs = index_overwrite_legs(client, market)
    expiring = [p for p in shorts
                if is_expiring(_occ_expiration(str(p["symbol"])), as_of)]
    closed: list = []
    if expiring:
        closed = close_index_overwrite(client, broker, db_engine, as_of=as_of, market=market,
                                       chase=chase, alert=alert)
        if alert:
            alert(f"index overlay: {market} spread force-closed at expiry "
                  f"({len(closed)} leg(s) filled)")
    asg = process_assignments(client, broker, db_engine, settings=settings, as_of=as_of, alert=alert)
    out = {"expiry_closed": len(closed), "earnings_closed": 0, "rewritten": 0,
           "reentered": asg["reentered"]}
    log.info("options daily check (index overlay)", extra=out)
    return out


# ====================================================================== #
# Assignment re-entry (4.6) — detect via Alpaca activities, re-buy if scored
# ====================================================================== #
def latest_scores(db_engine) -> dict[str, float]:
    """``{symbol: composite_score}`` for the most recent ``factor_scores`` date (or ``{}``)."""
    if db_engine is None:
        return {}
    from sqlalchemy import func, select
    from engine.db import factor_scores
    with db_engine.connect() as conn:
        d = conn.execute(select(func.max(factor_scores.c.date))).scalar()
        if d is None:
            return {}
        rows = conn.execute(select(factor_scores.c.symbol, factor_scores.c.composite_score)
                            .where(factor_scores.c.date == d)).all()
    return {r[0]: float(r[1]) for r in rows if r[1] is not None}


def assignment_reentry_plan(assignments: Sequence[Mapping], scores: Mapping[str, float],
                            *, reentry_threshold: float) -> list[dict]:
    """Which called-away names to re-buy: those still scoring > ``reentry_threshold`` (pure).

    ``assignments`` = ``[{underlying, shares}]``; returns ``[{symbol, shares}]`` to buy back.
    """
    plan = []
    for a in assignments:
        shares = int(a.get("shares") or 0)
        sym = a["underlying"]
        if shares > 0 and scores.get(sym, float("-inf")) > reentry_threshold:
            plan.append({"symbol": sym, "shares": shares})
    return plan


def _assignments_from_activities(activities: Sequence[Mapping]) -> list[dict]:
    """Map OPASN activity records to ``[{underlying, shares, option_symbol}]``."""
    out = []
    for a in activities:
        if str(a.get("activity_type") or "").upper() != "OPASN":
            continue
        sym = str(a.get("symbol") or "")
        underlying = _occ_underlying(sym) if _OCC_RE.match(sym) else sym
        shares = int(abs(a.get("qty") or 0))
        if underlying and shares:
            out.append({"underlying": underlying, "shares": shares, "option_symbol": sym})
    return out


def process_assignments(client, broker, db_engine, *, settings, as_of: date, alert=None) -> dict:
    """Detect option assignments (Alpaca OPASN), re-buy scored called-away stock, and flatten
    any short stock an assignment created.

    Two distinct aftermaths:

    * **Covered call assigned** (per-name mode) — the held shares were called away. For each
      assigned name still scoring above ``covered_calls.reentry_threshold``, buy the shares
      back at market (idempotent per name/day via ``client_order_id``).
    * **Uncovered-by-stock short call assigned** (the index overlay's SPY short leg) — there
      are no shares behind it, so assignment leaves **short stock** in the account. That is
      never the strategy (long-only book): flatten it at market immediately rather than carry
      short-index exposure until the next rebalance notices.

    Logs ``assignment`` / ``reentry`` / ``short_flatten`` lifecycle events. Resilient: an
    activities-read failure is logged and skipped (assignment is rare under the monthly
    close-before-expiry cadence).
    """
    try:
        activities = client.account_activities(["OPASN"], date=as_of.isoformat())
    except Exception as exc:  # noqa: BLE001
        log.warning("assignment check skipped; activities read failed", extra={"error": str(exc)})
        return {"assignments": 0, "reentered": 0, "flattened": 0}

    assignments = _assignments_from_activities(activities)
    for a in assignments:
        _log_simple(db_engine, "assignment", a["underlying"],
                    contracts=a["shares"] // _CONTRACT_SHARES, option_symbol=a.get("option_symbol"))

    plan = assignment_reentry_plan(assignments, latest_scores(db_engine),
                                   reentry_threshold=settings.covered_calls.reentry_threshold)
    reentered = 0
    for p in plan:
        coid = f"reentry:{as_of.isoformat()}:{p['symbol']}"
        try:
            broker.submit_order(p["symbol"], p["shares"], "buy", order_type="market",
                                client_order_id=coid)
        except AlpacaAPIError as exc:
            log.error("re-entry buy rejected", extra={"symbol": p["symbol"], "error": str(exc)})
            if alert:
                alert(f"assignment re-entry rejected {p['symbol']}: {exc}")
            continue
        _log_simple(db_engine, "reentry", p["symbol"])
        reentered += 1

    # Flatten short stock left by assigning a short call with no shares behind it (B4, audit).
    flattened = 0
    assigned_unders = {a["underlying"] for a in assignments}
    if assigned_unders:
        try:
            shorts = {str(p["symbol"]): float(p["qty"]) for p in client.all_positions()
                      if str(p.get("symbol")) in assigned_unders and float(p.get("qty") or 0) < 0}
        except Exception as exc:  # noqa: BLE001 — can't read positions → reconcile/next pass catches it
            log.warning("short-flatten check skipped; positions read failed", extra={"error": str(exc)})
            shorts = {}
        for sym, q in shorts.items():
            coid = f"flatten:{as_of.isoformat()}:{sym}"
            try:
                broker.submit_order(sym, int(abs(q)), "buy", order_type="market",
                                    client_order_id=coid)
            except AlpacaAPIError as exc:
                log.error("short flatten rejected", extra={"symbol": sym, "error": str(exc)})
                if alert:
                    alert(f"assignment left SHORT stock {sym} ({q:+.0f}) and the flatten was "
                          f"rejected: {exc} — flatten manually")
                continue
            _log_simple(db_engine, "short_flatten", sym)
            flattened += 1
            if alert:
                alert(f"assignment left short stock {sym} ({q:+.0f} sh); bought back to flat")

    if assignments and alert:
        alert(f"assignment: {len(assignments)} name(s) assigned, {reentered} re-entered, "
              f"{flattened} short position(s) flattened")
    log.info("assignment check", extra={"assignments": len(assignments), "reentered": reentered,
                                        "flattened": flattened})
    return {"assignments": len(assignments), "reentered": reentered, "flattened": flattened}


def _log_simple(db_engine, event_type: str, underlying: str, *, contracts=None,
                premium: float = 0.0, option_symbol=None) -> None:
    if db_engine is None:
        return
    from sqlalchemy import insert
    from engine.db import options_lifecycle
    with db_engine.begin() as conn:
        conn.execute(insert(options_lifecycle).values(
            ts=datetime.now(timezone.utc), event_type=event_type, underlying=underlying,
            option_symbol=option_symbol, contracts=contracts, premium=round(premium, 2)))
