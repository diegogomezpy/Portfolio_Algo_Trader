"""Instrument reference data for the dashboard: company name + sector + industry per symbol.

Static, slow-moving reference used only to *label* tickers in the UI (so a row reads
"AAPL · Apple Inc. · Information Technology" instead of a bare symbol). Two sources, both
already used elsewhere in the engine:

* **company name** — SEC's ``company_tickers.json`` ``title`` field (the same document
  :func:`engine.edgar.load_cik_map` reads for CIKs). Fetched once and cached to
  ``data/ref/company_names.parquet``; subsequent loads are offline.
* **sector / industry** — the cached SIC→GICS map from :mod:`engine.sectors`
  (``sector`` = GICS bucket, ``sic_description`` = the finer industry label).

Everything is best-effort: a missing cache, an offline SEC, or an unmapped symbol degrades to
``None`` for that field rather than raising — the dashboard just shows the ticker.
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable, Optional

import pandas as pd

from engine import edgar
from engine import sectors as sectors_mod
from engine.logger import get_logger

log = get_logger(__name__)

NAMES_PATH = Path("data/ref/company_names.parquet")

# SEC stores titles in all-caps, so naive word-capitalisation mangles brand casing
# ("JPMORGAN" → "Jpmorgan"), drops hyphens ("COCA COLA"), and miscases "Mc" names. This small
# table overrides display names for common large caps; it wins over the derived name. Keyed by
# ticker (unambiguous). Extend as held names surface with quirks.
_NAME_OVERRIDES = {
    "JPM": "JPMorgan Chase & Co", "UNH": "UnitedHealth Group Inc", "KO": "Coca-Cola Co",
    "NVDA": "NVIDIA Corp", "PEP": "PepsiCo Inc", "PYPL": "PayPal Holdings Inc", "EBAY": "eBay Inc",
    "MCD": "McDonald's Corp", "HPQ": "HP Inc", "HPE": "Hewlett Packard Enterprise Co",
    "IBM": "IBM", "AMD": "AMD Inc", "T": "AT&T Inc", "MMM": "3M Co", "GE": "GE Aerospace",
    "BAC": "Bank of America Corp", "CRM": "Salesforce Inc", "GS": "Goldman Sachs Group Inc",
    "MS": "Morgan Stanley", "USB": "U.S. Bancorp", "CVS": "CVS Health Corp", "DIS": "Walt Disney Co",
    "NKE": "Nike Inc", "SBUX": "Starbucks Corp", "LOW": "Lowe's Cos Inc", "TJX": "TJX Cos Inc",
    "WMT": "Walmart Inc", "AMZN": "Amazon.com Inc", "GOOGL": "Alphabet Inc", "GOOG": "Alphabet Inc",
    "META": "Meta Platforms Inc", "PM": "Philip Morris International Inc",
}


def _pretty(name: object) -> str:
    """Title-case SEC's all-caps company titles ("APPLE INC." → "Apple Inc.")."""
    return " ".join(w.capitalize() for w in str(name or "").split())


def load_company_names(
    *, fetch: Callable[[str], dict] = edgar.fetch_json, path: Path | str = NAMES_PATH
) -> dict[str, str]:
    """Return ``{TICKER: company name}``, reading the cache or building it from SEC once.

    Reads ``data/ref/company_names.parquet`` if present; otherwise fetches SEC's
    ``company_tickers.json`` (``title`` = name), prettifies, and writes the cache for next
    time. Any failure returns ``{}`` so the caller degrades to bare tickers.
    """
    path = Path(path)
    if path.exists():
        try:
            df = pd.read_parquet(path)
            return {str(s).upper(): str(n) for s, n in df["name"].items()}
        except Exception as exc:  # noqa: BLE001 — corrupt cache → rebuild below
            log.warning("company-names cache read failed: %s", exc)
    try:
        data = fetch(edgar.SEC_TICKERS_URL)
    except Exception as exc:  # noqa: BLE001 — offline / SEC hiccup → no names
        log.warning("company-names fetch failed: %s", exc)
        return {}
    names = {str(row["ticker"]).upper(): _pretty(row.get("title"))
             for row in data.values() if row.get("ticker")}
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        pd.Series(names, name="name").rename_axis("symbol").to_frame().to_parquet(path)
    except Exception as exc:  # noqa: BLE001 — cache write is optional
        log.warning("company-names cache write failed: %s", exc)
    return names


def reference_map(
    symbols: Optional[list[str]] = None,
    *,
    names: Optional[dict[str, str]] = None,
    sector_loader: Callable[[], pd.DataFrame] = sectors_mod.load_sector_map,
) -> dict[str, dict]:
    """``{TICKER: {name, sector, industry}}`` for ``symbols`` (or every known name).

    ``names`` / ``sector_loader`` are injectable for tests. Unknown fields are ``None``.
    """
    names = names if names is not None else load_company_names()
    try:
        smap = sector_loader()
    except Exception as exc:  # noqa: BLE001 — no sector cache → name-only labels
        log.warning("sector map load failed: %s", exc)
        smap = None
    syms = [str(s).upper() for s in (symbols if symbols is not None else names.keys())]
    out: dict[str, dict] = {}
    for s in syms:
        rec = {"name": _NAME_OVERRIDES.get(s) or names.get(s) or None, "sector": None, "industry": None}
        if smap is not None and s in smap.index:
            row = smap.loc[s]
            sec, ind = row.get("sector"), row.get("sic_description")
            rec["sector"] = None if pd.isna(sec) else str(sec)
            rec["industry"] = None if pd.isna(ind) else str(ind)
        out[s] = rec
    return out
