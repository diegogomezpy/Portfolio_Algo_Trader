"""Emergency killswitch — halt the engine, or flatten the book to cash.

Two actions (one auditable command instead of juggling ``systemctl`` flags under pressure):

* ``--halt``    — stop the scheduler **and** the watchdog timer (so the halt sticks, not
  revived in 5 min), then cancel every open order. Positions are left as held; nothing
  trades until you resume.
* ``--flatten`` — everything ``--halt`` does, **and** liquidate every position to cash
  (market orders, equities + short options). Requires typing ``FLATTEN`` (or ``--yes``).

Both record an alert (visible on the dashboard + ``alerts`` table). The systemd control is
best-effort (needs sudo); the Alpaca actions — the part that actually de-risks the book —
always run, even if systemctl is unavailable. Orders are DAY, so a flatten while the market
is closed fills at the next session.

Usage (on the VM)::

    ./.venv/bin/python scripts/killswitch.py --halt     --env paper
    ./.venv/bin/python scripts/killswitch.py --flatten  --env paper [--yes]

Resume after a halt::

    sudo systemctl start sharpe-eod sharpe-watchdog.timer
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from engine import alerts, config, db  # noqa: E402
from engine.broker import Broker  # noqa: E402
from engine.config import load_settings, require_env  # noqa: E402
from engine.logger import get_logger  # noqa: E402

log = get_logger(__name__)

# Stop the auto-restarter FIRST so stopping the service actually sticks, then the service.
_UNITS = ("sharpe-watchdog.timer", "sharpe-eod")


def stop_services(run=subprocess.run) -> list[str]:
    """Best-effort ``sudo systemctl stop`` of the watchdog timer + EOD service.

    Returns human-readable status lines and never raises — if systemctl/sudo isn't available
    the Alpaca de-risking still runs and we tell the operator to stop the service by hand.
    ``run`` is injectable for tests.
    """
    notes: list[str] = []
    for unit in _UNITS:
        try:
            # stopping sharpe-eod triggers its SIGTERM handler (cancels open orders) — can take
            # up to the unit's TimeoutStopSec (150s) if a rebalance stage is mid-flight.
            r = run(["sudo", "systemctl", "stop", unit], capture_output=True, text=True, timeout=160)
            if getattr(r, "returncode", 1) == 0:
                notes.append(f"stopped {unit}")
            else:
                notes.append(f"could NOT stop {unit} ({(r.stderr or '').strip() or 'non-zero exit'}) "
                             f"— stop it manually: sudo systemctl stop {unit}")
        except Exception as exc:  # noqa: BLE001 — never let systemd trouble block the de-risk
            notes.append(f"could NOT stop {unit} ({exc}) — stop it manually: sudo systemctl stop {unit}")
    return notes


def _confirm(action: str, env: str, assume_yes: bool) -> bool:
    if assume_yes:
        return True
    print(f"\n⚠️  {action.upper()} will CANCEL all open orders and SELL ALL POSITIONS to cash on "
          f"the {env} account. This cannot be undone.")
    try:
        return input("Type FLATTEN to proceed: ").strip() == "FLATTEN"
    except EOFError:                                    # non-interactive without --yes → refuse
        print("no terminal to confirm on; re-run with --yes to flatten non-interactively.")
        return False


def run_killswitch(*, flatten: bool, env: str, assume_yes: bool,
                   broker=None, services_fn=stop_services, alerter=None) -> dict:
    """Execute the killswitch. Returns a summary dict. Steps are injectable for tests."""
    action = "flatten" if flatten else "halt"

    if flatten and not _confirm("flatten", env, assume_yes):
        print("aborted — nothing changed.")
        return {"action": action, "aborted": True}

    # 1. stop the engine so it can't trade or re-establish positions while we de-risk.
    notes = services_fn()
    for n in notes:
        print(" -", n)

    # 2. de-risk via Alpaca (always runs, even if systemd couldn't be stopped).
    broker = broker or Broker(require_env("ALPACA_API_KEY"), require_env("ALPACA_SECRET_KEY"))
    n_cancel = n_closed = None
    try:
        n_cancel = broker.cancel_all_orders()
        print(f" - cancelled {n_cancel} open order(s)")
    except Exception as exc:  # noqa: BLE001
        print(f" - cancel_all_orders FAILED: {exc} — check Alpaca directly")
    if flatten:
        try:
            n_closed = broker.close_all_positions(cancel_orders=True)
            print(f" - submitted close orders for {n_closed} position(s)")
        except Exception as exc:  # noqa: BLE001
            print(f" - close_all_positions FAILED: {exc} — close positions in the Alpaca dashboard")

    # 3. record the intervention (dashboard + alerts table; emails if alerts are enabled).
    summary = (f"KILLSWITCH {action.upper()} ({env}): " + "; ".join(notes)
               + f"; cancelled {n_cancel if n_cancel is not None else 'FAILED'} orders"
               + (f"; flattened {n_closed if n_closed is not None else 'FAILED'} positions" if flatten else ""))
    if alerter is not None:
        try:
            alerter(summary)
        except Exception as exc:  # noqa: BLE001
            print(f" - (alert record failed: {exc})")
    return {"action": action, "aborted": False, "notes": notes,
            "cancelled": n_cancel, "flattened": n_closed, "summary": summary}


def main() -> None:
    ap = argparse.ArgumentParser(description="Emergency killswitch (halt / flatten)")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--halt", action="store_true",
                   help="stop the engine + watchdog and cancel open orders (positions left as held)")
    g.add_argument("--flatten", action="store_true",
                   help="--halt AND liquidate every position to cash (requires confirmation)")
    ap.add_argument("--env", choices=("paper", "live"), required=True)
    ap.add_argument("--yes", action="store_true", help="skip the typed confirmation (--flatten)")
    args = ap.parse_args()

    config.load_env(args.env)
    settings = load_settings()
    db_engine = db.get_engine()
    _smtp = str(getattr(settings.alerts, "smtp_host", "") or "").strip()
    _send_on = bool(getattr(settings.alerts, "send_enabled", False)) and _smtp not in ("", "TBD")
    alerter = alerts.make_alerter(db_engine, settings, dry_run=not _send_on)

    result = run_killswitch(flatten=args.flatten, env=args.env, assume_yes=args.yes, alerter=alerter)
    if result.get("aborted"):
        return
    print("\n" + result["summary"])
    if args.flatten:
        print("Positions are closing at market (DAY orders — fill next session if the market is closed).")
    print("Resume when ready:  sudo systemctl start sharpe-eod sharpe-watchdog.timer")


if __name__ == "__main__":
    main()
