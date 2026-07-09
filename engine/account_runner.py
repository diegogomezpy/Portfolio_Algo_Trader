"""Run a strategy against ONE non-primary account — the tracked-sleeves / testbed executor (CS-2).

This is how a :class:`~engine.config_strategy.ConfigStrategy` (or any :class:`~engine.strategy.Strategy`)
actually trades the **test account**: it builds that account's own client + broker from its stored
credentials, so every order physically routes to *that* Alpaca account and can never touch the
primary book. It mirrors ``run_cycle``'s sizing → risk-gate → plan → execute → snapshot, reusing the
proven engine (``plan_orders`` / ``submit_and_track`` / ``submit_express``), with three deliberate
differences for safety:

* **No ``reconcile``.** ``reconcile`` is single-account and writes shared position state; running it
  with a second account's client could cross-contaminate the primary. A tracked sleeve is
  engine-sole-traded on a fresh account, so reconcile is unnecessary here.
* **Primary is refused.** This path is only for non-primary accounts; the primary book stays on
  ``run_cycle`` unchanged (never break the current strategy).
* **Account-tagged snapshot + namespaced ``cycle_key``** (``acct:<slug>:<date>``) so the sleeve's
  NAV history and orders are attributable.

Weights are fractions of the base (like the live strategy); ``spec.leverage`` is applied at sizing
(``nav = equity × leverage``). Equity-only for now — per-account overlay execution is a later piece.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Callable, Optional

from engine import credstore, execute, ingest, monitor, risk, sectors, symbols
from engine.execute import PlannedOrder, plan_orders, submit_and_track
from engine.logger import get_logger
from engine.monitor import PRIMARY_ACCOUNT
from engine.strategy import StrategyContext

log = get_logger(__name__)

_PRICES_DIR = str(ingest.DEFAULT_PRICES_DIR)
_FUNDAMENTALS_DIR = str(ingest.DEFAULT_FUNDAMENTALS_DIR)


def _make_client(creds: dict):
    from engine.alpaca_client import AlpacaClient
    return AlpacaClient(api_key=creds["api_key"], secret_key=creds["api_secret"])


def _make_broker(creds: dict):
    from engine.broker import Broker
    return Broker(creds["api_key"], creds["api_secret"])


def _equity_positions(client) -> dict[str, float]:
    out: dict[str, float] = {}
    for p in client.all_positions():
        sym = str(p.get("symbol", ""))
        if symbols.is_equity(sym) and float(p.get("qty", 0)):
            out[sym] = float(p["qty"])
    return out


def _market_close(client) -> Optional[datetime]:
    try:
        nc = client.market_clock().get("next_close")
        return datetime.fromisoformat(nc) if nc else None
    except Exception:  # noqa: BLE001 — run without a close guarantee rather than block
        return None


def run_strategy_on_account(
    account: str,
    strategy,
    *,
    db_engine,
    settings,
    as_of: Optional[date] = None,
    mode: str = "normal",
    dry_run: bool = False,
    alert: Optional[Callable[[str], None]] = None,
    make_client: Optional[Callable] = None,
    make_broker: Optional[Callable] = None,
) -> dict:
    """Execute ``strategy`` on ``account`` (a non-primary credstore account). Returns a summary
    dict. Places real (paper) orders on that account — operator-triggered, one account at a time.

    ``dry_run`` computes and risk-gates the plan, then returns it (the sized orders) **without
    submitting anything** — the safe preview for the testbed."""
    if account == PRIMARY_ACCOUNT:
        raise ValueError("run_strategy_on_account is for NON-primary sleeves; the primary book "
                         "trades via run_cycle. Refusing to touch the primary through this path.")
    if mode not in ("normal", "express"):
        raise ValueError(f"mode must be normal|express, got {mode!r}")
    as_of = as_of or date.today()

    creds = credstore.get_credentials(db_engine, account)      # KeyError if the account is unknown
    client = (make_client or _make_client)(creds)
    broker = (make_broker or _make_broker)(creds)

    # NB: no reconcile (see module docstring) — the sleeve trades its own isolated account.
    positions = _equity_positions(client)
    ctx = StrategyContext(settings=settings, db_engine=db_engine, prices_dir=_PRICES_DIR,
                          fundamentals_dir=_FUNDAMENTALS_DIR, positions=positions)
    book = strategy.generate(ctx, as_of)
    if book.is_empty:
        log.warning("no targets for account strategy", extra={"account": account})
        return {"status": "no_targets", "account": account}

    leverage = float(getattr(getattr(strategy, "spec", None), "leverage", 1.0))
    try:
        sector_map = sectors.load_sector_map()["sector"]
    except Exception:  # noqa: BLE001 — sector cap degrades to empty rather than blocking the sleeve
        import pandas as pd
        sector_map = pd.Series(dtype=object)
    rc = risk.check_pretrade(book.weights, settings=settings, sector_map=sector_map,
                             universe=book.inputs.universe, equity_positions=positions,
                             as_of=as_of, leverage=leverage)
    if not rc.approved:
        log.error("risk gate blocked account strategy", extra={"account": account, "reason": rc.reason})
        if alert:
            alert(f"[{account}] risk gate blocked: {rc.reason}")
        return {"status": "blocked_risk", "account": account, "reason": rc.reason}

    equity = float(client.account().get("equity") or 0.0)
    if equity <= 0:
        return {"status": "no_equity", "account": account}
    nav = equity * leverage                                     # leverage applied here (weights are fractions)
    orders, pending = plan_orders(book.weights, positions, book.inputs.prices, nav=nav,
                                  settings=settings, adv=book.inputs.adv, spread=book.inputs.spread)

    if dry_run:                                                 # preview only — submit NOTHING
        return {"status": "dry_run", "account": account, "equity": equity, "leverage": leverage,
                "n_orders": len(orders),
                "orders": [{"symbol": o.symbol, "side": o.side, "qty": o.qty,
                            "notional": round(o.notional, 2)} for o in orders]}

    cycle_key = f"acct:{account}:{as_of.isoformat()}"
    stale = float(getattr(settings.execution, "stale_trade_max_s", 900) or 0)
    if mode == "express":
        orders = [PlannedOrder(o.symbol, o.side, o.qty, "market", None, o.notional) for o in orders]
        report = execute.submit_express(
            orders, broker=broker, db_engine=db_engine, cycle_key=cycle_key, pending=pending,
            quote=execute.quote_fn_for(client, stale_trade_max_s=stale),
            clock=(client.market_clock if hasattr(client, "market_clock") else None),
            ex=settings.execution, alert=alert)
    else:
        report = submit_and_track(
            orders, broker=broker, db_engine=db_engine, cycle_key=cycle_key, pending=pending,
            quote=execute.quote_fn_for(client, stale_trade_max_s=stale),
            quote_batch=execute.batch_quote_fn(client, stale_trade_max_s=stale),
            adv=book.inputs.adv, ex=settings.execution, market_close=_market_close(client), alert=alert)

    monitor.monitor_once(client, db_engine, account=account)    # tracked-sleeves: tag the snapshot
    result = {"status": "executed", "account": account, "cycle_key": cycle_key, "mode": mode,
              "submitted": report.submitted, "filled": report.filled, "partial": report.partial,
              "rejected": report.rejected, "deferred": report.deferred, "names": len(orders)}
    log.warning("account strategy complete", extra=result)
    return result
