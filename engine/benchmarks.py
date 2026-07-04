"""Strategy benchmarks for the dashboard — SPY (market beta) and CBOE BXMD (30-delta buy-write).

Used by both the live Track-Record tab and the backtest report to contextualize the strategy.

* **SPY** — the S&P 500, plain long-only beta (via yfinance).
* **BXMD** — CBOE S&P 500 30-Delta BuyWrite Index, the *direct* index analog of our 0.30-delta
  covered-call overlay. Yahoo only serves a single stale quote for it, so the full daily history
  is pulled straight from CBOE's public CDN (``..._History.csv``, back to 1986).

Each benchmark caches to ``data/ref/benchmarks/`` (git-ignored) with a few-hour TTL; a fetch
failure degrades to the cached copy. The fetchers are injectable so tests/preview run offline.
"""

from __future__ import annotations

import csv
import io
import json
import time
import urllib.request
from pathlib import Path

from engine.logger import get_logger

log = get_logger(__name__)

_CACHE = Path("data/ref/benchmarks")
_CBOE_URL = "https://cdn.cboe.com/api/global/us_indices/daily_prices/{ticker}_History.csv"
_TTL_S = 6 * 3600

# symbol → {source, ticker, name, desc}. ``name``/``desc`` surface in the dashboard UI.
BENCHMARKS: dict[str, dict] = {
    "SPY": {
        "source": "yf", "ticker": "SPY", "name": "S&P 500 (SPY)",
        "desc": "The S&P 500 large-cap index via SPDR's ETF — plain long-only equity beta, no "
                "options. The 'just own the market' baseline: full upside and full downside.",
    },
    "BXMD": {
        "source": "cboe", "ticker": "BXMD", "name": "CBOE S&P 500 30Δ BuyWrite (BXMD)",
        "desc": "CBOE S&P 500 30-Delta BuyWrite Index — holds the S&P 500 and sells 30-delta "
                "out-of-the-money monthly calls. The direct index analog of our 0.30-delta "
                "covered-call overlay: keeps most of the upside, harvests option premium, and "
                "gives up only the far tail. The apples-to-apples 'is the overlay worth it?' line.",
    },
    # BXRD (Russell 2000 buy-write) removed 2026-07-03 (Diego): a small-cap reference added
    # noise, not signal, against a large-cap book — JEPI/USMV cover the useful comparisons.
    "JEPI": {
        "source": "yf", "ticker": "JEPI", "name": "JPMorgan Equity Premium Income (JEPI)",
        "desc": "The $30B+ real-world peer: a defensive low-volatility equity book plus "
                "systematic S&P call overwriting for premium income — the same product design "
                "as this fund, run by professionals at ~0.65 beta unlevered. The 'is SEPI a "
                "good version of this strategy?' head-to-head (compare Sharpe, not raw return — "
                "our 2× gross runs roughly twice JEPI's beta).",
    },
    "USMV": {
        "source": "yf", "ticker": "USMV", "name": "iShares MSCI Min Vol USA (USMV)",
        "desc": "Passive minimum-volatility US equity ETF — the low-beta stock-picking sleeve's "
                "benchmark with no options and no leverage. If the factor model earns its keep, "
                "the equity book should beat this on risk-adjusted return; if not, the stock "
                "selection is just expensive USMV.",
    },
}


def describe(symbol: str) -> dict:
    """``{name, desc}`` for a benchmark symbol (falls back to the raw symbol)."""
    b = BENCHMARKS.get(symbol, {})
    return {"name": b.get("name", symbol), "desc": b.get("desc", "")}


# ---- fetchers (injectable) --------------------------------------------------
def _parse_cboe_csv(text: str) -> dict[str, float]:
    """Parse a CBOE ``DATE,<IDX>`` history CSV (MM/DD/YYYY) → ``{ISO date: close}``."""
    out: dict[str, float] = {}
    for row in csv.reader(io.StringIO(text)):
        if len(row) < 2 or row[0].strip().upper() == "DATE":
            continue
        try:
            mm, dd, yy = row[0].strip().split("/")
            out[f"{yy}-{int(mm):02d}-{int(dd):02d}"] = float(row[1])
        except (ValueError, IndexError):
            continue
    return out


def _fetch_cboe(ticker: str) -> dict[str, float]:
    req = urllib.request.Request(_CBOE_URL.format(ticker=ticker),
                                 headers={"User-Agent": "Mozilla/5.0 (sharpe-engine)"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return _parse_cboe_csv(resp.read().decode("utf-8", "replace"))


def _fetch_yf(ticker: str, start: str) -> dict[str, float]:
    import yfinance as yf
    hist = yf.Ticker(ticker).history(start=start)
    return {str(idx.date()): float(c) for idx, c in hist["Close"].items() if c == c}


# ---- cached access ----------------------------------------------------------
def _cache_path(symbol: str) -> Path:
    return _CACHE / f"{symbol}.json"


def _load_cache(symbol: str) -> dict | None:
    p = _cache_path(symbol)
    if p.exists():
        try:
            return json.loads(p.read_text())
        except Exception:  # noqa: BLE001
            return None
    return None


def closes(symbol: str, start: str, *, fetch_cboe=_fetch_cboe, fetch_yf=_fetch_yf) -> dict[str, float]:
    """``{ISO date: close}`` for one benchmark since ``start``; cached with a few-hour TTL."""
    p = _cache_path(symbol)
    if p.exists() and (time.time() - p.stat().st_mtime) < _TTL_S:
        cached = _load_cache(symbol)
        if cached:
            return cached
    spec = BENCHMARKS.get(symbol)
    if not spec:
        return {}
    try:
        data = fetch_cboe(spec["ticker"]) if spec["source"] == "cboe" else fetch_yf(spec["ticker"], start)
        if data:
            _CACHE.mkdir(parents=True, exist_ok=True)
            p.write_text(json.dumps(data))
        return data
    except Exception as exc:  # noqa: BLE001 — degrade to stale cache, never raise into the UI
        log.warning("benchmark fetch failed for %s: %s", symbol, exc)
        return _load_cache(symbol) or {}


def fetch_closes(symbols, start: str, *, fetch_cboe=_fetch_cboe, fetch_yf=_fetch_yf) -> dict[str, dict]:
    """``{symbol: {ISO date: close}}`` for several benchmarks (best-effort, cached)."""
    return {s: closes(s, start, fetch_cboe=fetch_cboe, fetch_yf=fetch_yf) for s in symbols}


def align(close_map: dict, dates: list[str]) -> list:
    """Reindex a ``{ISO date: close}`` map onto ``dates``, forward-filling the latest prior close."""
    import bisect
    items = sorted(close_map.items())
    keys, vals = [k for k, _ in items], [v for _, v in items]
    out = []
    for d in dates:
        i = bisect.bisect_right(keys, d) - 1
        out.append(vals[i] if i >= 0 else (vals[0] if vals else None))
    return out
