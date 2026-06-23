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
from datetime import datetime, timezone
from typing import Callable, Optional

from engine.logger import get_logger

log = get_logger(__name__)

ALERT_TYPES = (
    "rebalance_completed", "risk_gate_block", "fill_failure",
    "assignment", "data_staleness", "system_error",
)


def classify(message: str) -> str:
    """Map an alert message to a SPEC alert type (first match wins; default system_error)."""
    m = message.lower()
    if "rebalance" in m and "complet" in m:
        return "rebalance_completed"
    if "risk gate" in m:
        return "risk_gate_block"
    if "assign" in m:
        return "assignment"
    if "reject" in m or "partial fill" in m or "unfilled" in m:
        return "fill_failure"
    if "divergence" in m or "stale" in m:
        return "data_staleness"
    return "system_error"


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
                sender(message, atype, settings)
                delivered = True
            except Exception as exc:  # noqa: BLE001 — alerting must never break the pipeline
                log.error("alert send failed", extra={"type": atype, "error": str(exc)})
        _record(db_engine, atype, message, delivered)

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
