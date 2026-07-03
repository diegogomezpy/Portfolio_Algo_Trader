"""Symbol classification — THE one definition of "is this an option or an equity?".

Before the 2026-07 audit this logic existed in three flavors: two OCC regexes in
``dashboard/data.py``, one in ``engine/covered_calls.py``, and a fragile
``len(symbol) <= 5`` heuristic ×4 across ``engine/alerts.py`` and ``scripts/run_eod.py``
(which silently misclassifies any 6-character equity ticker as an option). Every module
now imports from here.

OCC format: ``ROOT + YYMMDD + (C|P) + strike×1000 (8 digits)`` — e.g.
``SPY260731C00764000`` = SPY call, 2026-07-31, strike 764.0.
"""

from __future__ import annotations

import re
from datetime import date

# Full-match parse: (root, yymmdd, C|P, strike). Kept as the 4-group shape the
# covered-call engine has always used.
OCC_RE = re.compile(r"^([A-Z0-9]+?)(\d{6})([CP])(\d{8})$")
# Suffix-only test — tolerant of any root, the cheap "is it an option?" check.
OCC_SUFFIX_RE = re.compile(r"\d{6}[CP]\d{8}$")


def is_option(symbol) -> bool:
    """Whether ``symbol`` is an OCC option symbol."""
    return bool(OCC_SUFFIX_RE.search(str(symbol or "")))


def is_equity(symbol) -> bool:
    """Whether ``symbol`` is a plain equity ticker (i.e. not an OCC option symbol)."""
    return bool(symbol) and not is_option(symbol)


def parse_occ(symbol) -> dict | None:
    """Parse an OCC symbol to ``{underlying, type, strike, expiration}`` (or ``None``).

    ``expiration`` is a :class:`datetime.date`; ``type`` is ``"call"`` / ``"put"``;
    ``strike`` is the price (the 8-digit field ÷ 1000). Unparseable input → ``None``
    so a malformed symbol is skipped, never a crash.
    """
    m = OCC_RE.match(str(symbol or ""))
    if not m:
        return None
    root, ymd, cp, strike = m.groups()
    try:
        exp = date(2000 + int(ymd[:2]), int(ymd[2:4]), int(ymd[4:6]))
    except ValueError:
        return None
    return {"underlying": root, "type": "call" if cp == "C" else "put",
            "strike": int(strike) / 1000.0, "expiration": exp}
