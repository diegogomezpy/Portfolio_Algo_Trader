"""Send a one-off ops email via the engine's configured SMTP (used by the watchdog).

Best-effort and standalone: chdir to the repo root so relative config/.env paths resolve,
load the paper env, and reuse engine.alerts._send_email. Never raises into the caller.

    python deploy/notify.py "sharpe-engine: watchdog restarted sharpe-eod"
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

os.chdir(Path(__file__).resolve().parent.parent)  # repo root: config/settings.yaml, .env.paper

from engine import alerts, config  # noqa: E402


def main() -> None:
    msg = sys.argv[1] if len(sys.argv) > 1 else "sharpe-engine ops notification"
    try:
        config.load_env("paper")
        alerts._send_email(msg, "ops", config.load_settings())
        print("notify sent:", msg)
    except Exception as exc:  # noqa: BLE001 — notification must never fail the watchdog
        print("notify failed:", exc)


if __name__ == "__main__":
    main()
