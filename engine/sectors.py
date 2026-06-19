"""Sector classification for the sector-cap constraint (DECISIONS D23c).

The optimizer caps any single sector at ``portfolio.max_sector_pct`` (30%), but
nothing in the store carries a sector. Rather than add a flaky third-party data
source, we reuse our existing SEC access: every filer's submissions document
(``data.sec.gov/submissions/CIK{…}.json``) carries a four-digit **SIC code**, and
:func:`sic_to_sector` maps that to one of eleven GICS-style buckets.

**This is an approximate SIC→GICS crosswalk, by design.** SIC predates GICS and
does not line up cleanly, so the table special-cases the handful of cross-overs
that actually move a diversification cap — pharma out of chemicals, computers /
semiconductors / software out of machinery, REITs out of holding companies, motor
vehicles out of transport equipment — and falls back to two-digit major groups for
everything else. It is a guardrail input, not a precise classification.

The crosswalk (:func:`sic_to_sector`) is pure and unit-tested. Network access
(:func:`fetch_submissions`) is thin and injectable. SIC is a *current* attribute,
so the cached map is a snapshot (sector rarely changes); names without a CIK
(ETFs/ADRs) bucket to ``"Unknown"``.

Build the cache once::

    python -m engine.sectors --env paper --universe liquid
"""

from __future__ import annotations

import argparse
import time
from pathlib import Path
from typing import Callable, Optional

import pandas as pd

from engine import edgar
from engine.logger import get_logger

log = get_logger(__name__)

SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik}.json"
DEFAULT_SECTORS_PATH = Path("data/ref/sectors.parquet")

# The eleven GICS sectors plus an explicit unknown bucket.
SECTORS = (
    "Energy", "Materials", "Industrials", "Consumer Discretionary",
    "Consumer Staples", "Health Care", "Financials", "Information Technology",
    "Communication Services", "Utilities", "Real Estate", "Unknown",
)

# Ordered (lo, hi, sector) on the full four-digit SIC. First match wins, so narrow
# cross-over ranges are listed *before* the broad two-digit major groups that would
# otherwise swallow them (e.g. pharma 2833–2836 before chemicals 2800–2899).
_SIC_RANGES: list[tuple[int, int, str]] = [
    # --- Cross-overs that must beat their major group ----------------------
    (1311, 1311, "Energy"),                 # crude petroleum & natural gas
    (2833, 2836, "Health Care"),            # medicinal chemicals / pharma / biologics
    (3570, 3579, "Information Technology"),  # computer & office equipment
    (3661, 3679, "Information Technology"),  # comms/semiconductor/electronic components
    (3690, 3699, "Information Technology"),  # misc electrical machinery
    (3711, 3716, "Consumer Discretionary"),  # motor vehicles & bodies
    (3826, 3826, "Health Care"),            # laboratory analytical instruments
    (3840, 3851, "Health Care"),            # surgical/medical/dental/ophthalmic
    (4610, 4619, "Energy"),                 # pipelines, except natural gas
    (5912, 5912, "Consumer Staples"),       # drug stores & proprietary stores
    (6798, 6798, "Real Estate"),            # REITs (out of holding/investment offices)
    (7370, 7379, "Information Technology"),  # computer programming, data, software
    # --- Two-digit major-group fallbacks -----------------------------------
    (100, 999, "Consumer Staples"),         # agriculture production
    (1000, 1099, "Materials"),              # metal mining
    (1200, 1299, "Energy"),                 # coal mining
    (1300, 1399, "Energy"),                 # oil & gas extraction
    (1400, 1499, "Materials"),              # nonmetallic minerals
    (1500, 1799, "Industrials"),            # construction
    (2000, 2099, "Consumer Staples"),       # food & kindred products
    (2100, 2199, "Consumer Staples"),       # tobacco
    (2200, 2399, "Consumer Discretionary"),  # textiles & apparel
    (2400, 2499, "Materials"),              # lumber & wood
    (2500, 2599, "Consumer Discretionary"),  # furniture
    (2600, 2699, "Materials"),              # paper
    (2700, 2799, "Communication Services"),  # printing & publishing
    (2800, 2899, "Materials"),              # chemicals (ex-pharma, special-cased above)
    (2900, 2999, "Energy"),                 # petroleum refining
    (3000, 3199, "Consumer Discretionary"),  # rubber, plastics, leather
    (3200, 3399, "Materials"),              # stone/clay/glass, primary metals
    (3400, 3499, "Industrials"),            # fabricated metal
    (3500, 3599, "Industrials"),            # machinery (ex-computers, special-cased)
    (3600, 3699, "Information Technology"),  # electronic & electrical equipment
    (3700, 3799, "Industrials"),            # transportation equipment (ex-autos)
    (3800, 3899, "Industrials"),            # instruments (ex-medical, special-cased)
    (3900, 3999, "Consumer Discretionary"),  # misc manufacturing (toys, jewelry)
    (4000, 4499, "Industrials"),            # rail, trucking, water, transit transport
    (4500, 4599, "Industrials"),            # air transportation
    (4600, 4699, "Industrials"),            # pipelines (ex-petroleum, special-cased)
    (4700, 4799, "Industrials"),            # transportation services
    (4800, 4899, "Communication Services"),  # communications
    (4900, 4999, "Utilities"),              # electric, gas, sanitary services
    (5000, 5199, "Industrials"),            # wholesale trade
    (5200, 5399, "Consumer Discretionary"),  # building materials & general merchandise
    (5400, 5499, "Consumer Staples"),       # food stores
    (5500, 5999, "Consumer Discretionary"),  # auto/apparel/furniture/misc retail
    (6000, 6299, "Financials"),             # banks, credit, brokers
    (6300, 6499, "Financials"),             # insurance
    (6500, 6599, "Real Estate"),            # real estate
    (6700, 6799, "Financials"),             # holding & investment offices (ex-REIT)
    (7000, 7099, "Consumer Discretionary"),  # hotels & lodging
    (7200, 7299, "Consumer Discretionary"),  # personal services
    (7300, 7399, "Industrials"),            # business services (ex-software)
    (7500, 7599, "Consumer Discretionary"),  # automotive repair
    (7800, 7899, "Communication Services"),  # motion pictures
    (7900, 7999, "Consumer Discretionary"),  # amusement & recreation
    (8000, 8099, "Health Care"),            # health services
    (8700, 8799, "Industrials"),            # engineering, accounting, management
]


# ====================================================================== #
# Pure crosswalk — unit-tested
# ====================================================================== #
def sic_to_sector(sic: object) -> str:
    """Map a SIC code to one of :data:`SECTORS`; ``"Unknown"`` if unmappable.

    Accepts an int or a string of digits. The first matching range in
    :data:`_SIC_RANGES` wins (narrow cross-overs are ordered before their
    major group), so the lookup is order-sensitive by construction.
    """
    try:
        code = int(str(sic).strip())
    except (TypeError, ValueError):
        return "Unknown"
    if code <= 0:
        return "Unknown"
    for lo, hi, sector in _SIC_RANGES:
        if lo <= code <= hi:
            return sector
    return "Unknown"


# ====================================================================== #
# Network (thin, injectable)
# ====================================================================== #
def fetch_submissions(cik: str, *, fetch: Callable[[str], dict] = edgar.fetch_json) -> dict:
    """Fetch a filer's SEC submissions document for a zero-padded CIK."""
    return fetch(SUBMISSIONS_URL.format(cik=str(cik).zfill(10)))


def symbol_sic(
    cik: str, *, fetch: Callable[[str], dict] = edgar.fetch_json
) -> tuple[Optional[int], Optional[str]]:
    """Return ``(sic, sic_description)`` for a CIK from its submissions document.

    ``(None, None)`` if the document omits a usable SIC (some shells/funds do).
    """
    doc = fetch_submissions(cik, fetch=fetch)
    raw = str(doc.get("sic", "")).strip()
    if not raw.isdigit():
        return None, None
    return int(raw), (doc.get("sicDescription") or None)


def build_sector_map(
    symbols: list[str],
    cikmap: dict[str, str],
    *,
    fetch: Callable[[str], dict] = edgar.fetch_json,
    sleep_s: float = 0.15,
) -> pd.DataFrame:
    """Build a ``symbol → sector`` table by reading each filer's SIC from SEC.

    Symbols absent from ``cikmap`` (ETFs/ADRs without a CIK) and filers whose
    submissions omit a SIC bucket to ``"Unknown"``. A fetch error for one symbol
    is logged and bucketed ``"Unknown"`` rather than failing the whole build, so
    a long run degrades gracefully. ``sleep_s`` throttles to honour SEC fair-access
    (≤10 req/s). Returns a frame indexed by ``symbol`` with columns
    ``cik, sic, sic_description, sector``.
    """
    rows = []
    fetched = 0
    for symbol in symbols:
        cik = cikmap.get(symbol.upper())
        sic: Optional[int] = None
        desc: Optional[str] = None
        if cik is not None:
            try:
                sic, desc = symbol_sic(cik, fetch=fetch)
            except Exception as exc:  # network/JSON hiccup on a single name
                log.warning("submissions fetch failed", extra={"symbol": symbol, "error": str(exc)})
            fetched += 1
            if sleep_s:
                time.sleep(sleep_s)
        rows.append({
            "symbol": symbol,
            "cik": cik,
            "sic": sic,
            "sic_description": desc,
            "sector": sic_to_sector(sic),
        })
    out = pd.DataFrame(rows).set_index("symbol")
    log.info(
        "sector map built",
        extra={"symbols": len(symbols), "fetched": fetched,
               "unknown": int((out["sector"] == "Unknown").sum())},
    )
    return out


# ====================================================================== #
# Cache I/O
# ====================================================================== #
def write_sector_map(df: pd.DataFrame, path: Path | str = DEFAULT_SECTORS_PATH) -> Path:
    """Persist the sector map to parquet, creating the parent directory."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    df.to_parquet(path)
    return path


def load_sector_map(path: Path | str = DEFAULT_SECTORS_PATH) -> pd.DataFrame:
    """Load the cached sector map (indexed by symbol). Raises if absent."""
    return pd.read_parquet(path)


def main() -> None:
    from scripts import backfill  # liquid_universe lives with the other backfill tooling

    parser = argparse.ArgumentParser(description="Build the symbol→sector cache from SEC SIC codes")
    parser.add_argument("--env", choices=("paper", "live"), default="paper")
    parser.add_argument("--symbols", default=None, help="Comma-separated subset (default: universe)")
    parser.add_argument("--universe", choices=("liquid", "all"), default="liquid",
                        help="'liquid' = current investable snapshot (default); 'all' = full CIK map")
    parser.add_argument("--out", default=str(DEFAULT_SECTORS_PATH))
    parser.add_argument("--sleep", type=float, default=0.15, help="seconds between SEC requests")
    args = parser.parse_args()

    from engine import config
    config.load_env(args.env)
    settings = config.load_settings()

    cikmap = edgar.load_cik_map()
    if args.symbols:
        symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    elif args.universe == "liquid":
        symbols = backfill.liquid_universe(
            min_price=settings.universe.min_price,
            min_adv_usd=settings.universe.min_adv_usd,
        )
    else:
        symbols = sorted(cikmap)
    log.info("building sector map", extra={"symbols": len(symbols), "universe": args.universe})

    df = build_sector_map(symbols, cikmap, sleep_s=args.sleep)
    out = write_sector_map(df, args.out)
    counts = df["sector"].value_counts().to_dict()
    log.info("sector map written", extra={"path": str(out), "by_sector": counts})
    print(f"Sector map written to {out} ({len(df)} symbols).")
    for sector, n in df["sector"].value_counts().items():
        print(f"  {sector:24s} {n}")


if __name__ == "__main__":
    main()
