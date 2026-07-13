"""Alerting — record every alert to Postgres and (optionally) email it (SPEC).

The engine threads an ``alert: Callable[[str], None]`` callback through every stage
(``run_cycle``, ``reconcile``, ``execute``, ``covered_calls``). :func:`make_alerter`
returns a concrete one: it classifies the message into one of the SPEC alert types,
writes a row to the ``alerts`` table, and sends an email — unless ``dry_run`` (the default
until ``settings.alerts.smtp_host`` is configured), which records + logs without sending.

Keeping the callback **single-argument** (just the message) preserves the contract already
wired everywhere; the type is derived from the (stable, descriptive) message text by
:func:`classify`, so no call site or test stub has to change.

**Alert types (SPEC, 6).** SPEC's *"L1 drift rebalance triggered"* is obsolete — DECISIONS
D31 removed the drift-triggered rebalance (drift is telemetry now) — leaving: rebalance
completed, risk-gate block, fill failure/partial, assignment, data staleness, system error.

SMTP send is injectable (``send=``) so tests run with a fake transport and no network.
"""

from __future__ import annotations

import os
import re
from datetime import datetime, timezone
from typing import Callable, Optional

from engine.logger import get_logger

log = get_logger(__name__)

ALERT_TYPES = (
    "rebalance_completed", "risk_gate_block", "fill_failure",
    "assignment", "data_staleness", "system_error",
)


def classify(message: str) -> str:
    """Map an alert message to a SPEC alert type (first match wins).

    Error phrases are checked **before** the "complete" branch so *"failed to complete"* is a
    ``system_error``, not a ``rebalance_completed`` (the old ordering got this backwards).
    """
    m = message.lower()
    if "system error" in m or "failed" in m or "unreachable" in m or "exception" in m:
        return "system_error"
    if "risk gate" in m:
        return "risk_gate_block"
    if "assign" in m or "called away" in m or "re-enter" in m:
        return "assignment"
    if "complet" in m or "top-up" in m:            # rebalance/top-up finished
        return "rebalance_completed"
    if "reject" in m or "partial fill" in m or "unfilled" in m or "coverage gate" in m:
        return "fill_failure"
    if "divergence" in m or "stale" in m:
        return "data_staleness"
    return "system_error"


# Severity drives the dashboard's colour (info/warn/error). It is derived from the **message**
# (the descriptive, stable text), not the coarse alert_type — a top-up that *filled* deferred
# names is info, while "failed to complete" is an error even though it contains "complete".
_SEV_ERROR = ("system error", "failed", "unreachable", "no target weights",
              "coverage gate blocked", "risk gate blocked", "exception", "traceback")
_SEV_DONE = ("complet", "top-up", "re-enter", "called away")     # an operation that finished
_SEV_ERROR_LATE = ("rejected", "blocked")
_SEV_WARN = ("unfilled", "divergence", "stale", "partial")
_SEV_PROBLEM_COUNT = re.compile(r"[1-9]\d*\s+(?:rejected|partial|unfilled)")


def severity(message: str) -> str:
    """Classify a message's severity as ``"error"`` / ``"warn"`` / ``"info"`` (dashboard colour).

    Ordered so a *completed* operation reads as info unless it reports a non-zero problem count
    (then warn), and hard failures read as error regardless of the word "complete" appearing.
    """
    m = (message or "").lower()
    if any(p in m for p in _SEV_ERROR):
        return "error"
    if any(p in m for p in _SEV_DONE):
        return "warn" if _SEV_PROBLEM_COUNT.search(m) else "info"
    if any(p in m for p in _SEV_ERROR_LATE):
        return "error"
    if any(p in m for p in _SEV_WARN):
        return "warn"
    return "info"


def make_alerter(db_engine, settings=None, *, send: Optional[Callable] = None,
                 dry_run: bool = True) -> Callable[[str], None]:
    """Return a single-arg ``alert(message)`` callback.

    Records every alert to the ``alerts`` table (with the classified type and delivery
    status). When ``dry_run`` is False it also emails via ``send`` (default SMTP); a send
    failure is logged and recorded as undelivered — it never raises into the pipeline.
    """
    sender = send or _send_email

    def alerter(message: str) -> None:
        atype = classify(message)
        delivered = False
        if dry_run:
            log.info("alert (dry-run)", extra={"type": atype, "text": message})
        else:
            try:
                sender(_email_body(message, db_engine, settings), atype, settings)
                delivered = True
            except Exception as exc:  # noqa: BLE001 — alerting must never break the pipeline
                log.error("alert send failed", extra={"type": atype, "error": str(exc)})
        _record(db_engine, atype, message, delivered)   # record the original (terse) message

    return alerter


def _record(db_engine, alert_type: str, message: str, delivered: bool) -> None:
    if db_engine is None:
        return
    from sqlalchemy import insert
    from engine.db import alerts as alerts_t
    with db_engine.begin() as conn:
        conn.execute(insert(alerts_t).values(
            ts=datetime.now(timezone.utc), alert_type=alert_type,
            message=message, delivered=delivered))


def _usd(v) -> str:
    try:
        return ("-$" if float(v) < 0 else "$") + f"{abs(float(v)):,.0f}"
    except (TypeError, ValueError):
        return "—"


def _portfolio_context(db_engine) -> str:
    """A compact current-book snapshot for the email body (NAV, leverage, drift, last rebalance).

    Best-effort and read-only: returns ``""`` if the DB is empty or any read fails — alerts must
    never break the pipeline, so this can only *add* detail, never throw.
    """
    if db_engine is None:
        return ""
    try:
        from sqlalchemy import desc, select
        from engine.db import rebalance_log, snapshots
        with db_engine.connect() as conn:
            snap = conn.execute(
                select(snapshots.c.nav, snapshots.c.cash, snapshots.c.weights,
                       snapshots.c.positions, snapshots.c.drift)
                .order_by(desc(snapshots.c.ts)).limit(1)).first()
            reb = conn.execute(
                select(rebalance_log.c.ts, rebalance_log.c.trigger_reason)
                .where(rebalance_log.c.risk_gate_passed.is_(True))
                .order_by(desc(rebalance_log.c.ts)).limit(1)).first()
        if not snap:
            return ""
        nav, cash, weights, positions, drift = snap
        weights, positions = (weights or {}), (positions or {})
        from engine.symbols import is_equity
        lev = sum(float(v) for s, v in weights.items() if is_equity(s))   # equity legs only
        npos = sum(1 for s, q in positions.items() if q and is_equity(s))
        lines = ["— Portfolio —",
                 f"  NAV         {_usd(nav)}",
                 f"  Leverage    {lev:.2f}x  (gross {_usd((nav or 0) * lev)})",
                 f"  Cash        {_usd(cash)}",
                 f"  Positions   {npos}",
                 f"  Drift (L1)  {('%.1f%%' % (drift * 100)) if drift is not None else '—'}"]
        if reb:
            lines.append(f"  Last rebal  {str(reb[0])[:10]}  ({reb[1] or 'monthly'})")
        return "\n".join(lines)
    except Exception:  # noqa: BLE001 — context is optional; never let it break the alert
        return ""


def _dashboard_footer(settings) -> str:
    """Link to the public dashboard. ``public_url`` comes from ``settings.dashboard`` (not secret).
    The dashboard is openly viewable (no login) — execution stays gated by SEPI_EXEC_TOKEN inside
    the app — so there is no credential to append. Returns ``""`` when no ``public_url`` is set."""
    d = getattr(settings, "dashboard", None)
    url = str(getattr(d, "public_url", "") or "").strip()
    if not url:
        return ""
    return f"— Dashboard —\n  {url}"


def _email_body(message: str, db_engine, settings) -> str:
    """The full alert email: the event message, then the portfolio snapshot, then the dashboard
    link. Each section is best-effort — a missing DB or unconfigured dashboard just drops it."""
    parts = [message]
    for extra in (_portfolio_context(db_engine), _dashboard_footer(settings)):
        if extra:
            parts.append(extra)
    return "\n\n".join(parts)


def _send_email(message: str, subject: str, settings) -> None:
    """Send one alert email via SMTP. Raises if SMTP isn't configured (→ recorded undelivered)."""
    import smtplib
    from email.message import EmailMessage

    a = getattr(settings, "alerts", None)
    host = str(getattr(a, "smtp_host", "") or "").strip()
    to_addr = str(getattr(a, "email_to", "") or "").strip()
    from_addr = str(getattr(a, "email_from", "") or "").strip() or to_addr
    if not host or host == "TBD" or not to_addr or to_addr == "TBD":
        raise RuntimeError("SMTP not configured (settings.alerts.smtp_host / email_to)")

    msg = EmailMessage()
    msg["Subject"] = f"[sharpe-engine] {subject}"
    msg["From"] = from_addr
    msg["To"] = to_addr
    msg.set_content(message)
    with smtplib.SMTP(host, int(getattr(a, "smtp_port", 587) or 587)) as smtp:
        smtp.starttls()
        password = os.environ.get("SMTP_PASSWORD", "")
        if password:
            smtp.login(from_addr, password)
        smtp.send_message(msg)
