"""Data ingestion — Alpaca prices + yfinance fundamentals into the Parquet store.

This module pulls external data and writes it to the on-disk Parquet store in
the schema downstream factor computation expects. It backs two entry points:

* ``scripts/backfill.py`` — one-time historical pull (default mid-2020; DECISIONS D21).
* the daily incremental pull (``run_daily_ingest`` / ``python -m engine.ingest``).

Both share the same compute helpers (:func:`bars_to_long_frame`,
:func:`write_equities_parquet`), so backfill and daily ingest produce byte-for-
byte identical schema and consistent values — a Phase 0 gate criterion.

**Equities** (``data/raw/equities/YYYY-MM-DD.parquet``, one row per ticker):
``open, high, low, close, volume, adv_20d, spread``. ``adv_20d`` is the trailing
20-trading-day average *dollar* volume (close × volume); ``spread`` is the daily
range proxy ``(high - low) / close`` (a point-in-time liquidity proxy — execute.py
uses live bid/ask at order time, much later).

**Fundamentals** (``data/raw/fundamentals/YYYY-QN.parquet``, one per quarter):
``pe_ratio, pb_ratio, roe, gross_margin, report_date, source`` — pulled once per
quarter and cached (not re-fetched until the next earnings cycle).

The external fetchers are injected (``client``, ``fetch_one``) so the pipeline
is unit-testable against mocks without importing the Alpaca SDK or hitting Yahoo.
Idempotent: re-running for the same date/quarter overwrites, never duplicates.
"""

from __future__ import annotations

import time
from datetime import date as _date
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable, Iterable, Sequence

import numpy as np
import pandas as pd

from engine.logger import get_logger

log = get_logger(__name__)

# Canonical schemas — both backfill and daily ingest write exactly these.
EQUITY_COLUMNS = ["open", "high", "low", "close", "volume", "adv_20d", "spread"]
FUNDAMENTAL_COLUMNS = ["pe_ratio", "pb_ratio", "roe", "gross_margin", "report_date", "source"]

DEFAULT_PRICES_DIR = Path("data/raw/equities")
DEFAULT_FUNDAMENTALS_DIR = Path("data/raw/fundamentals")
DEFAULT_ADV_WINDOW = 20
DEFAULT_BACKOFF_S = (30, 60, 120)


# ====================================================================== #
# Pure compute (shared by backfill + daily ingest; no I/O, no network)
# ====================================================================== #
def bars_to_long_frame(
    bars_by_symbol: dict[str, list[dict]], adv_window: int = DEFAULT_ADV_WINDOW
) -> pd.DataFrame:
    """Convert ``{symbol: [bar, ...]}`` into a long per-(date, symbol) frame.

    Each bar is the normalised dict from :meth:`AlpacaClient.bars`/``bars_multi``
    (``time, open, high, low, close, volume``). Returns columns
    ``date, symbol, open, high, low, close, volume, adv_20d, spread`` where
    ``adv_20d`` is the trailing ``adv_window``-day mean dollar volume (defined
    only once ``adv_window`` observations exist — no forward fill) and ``spread``
    is ``(high - low) / close``. Symbols with no bars are skipped.
    """
    frames: list[pd.DataFrame] = []
    for symbol, bars in bars_by_symbol.items():
        if not bars:
            continue
        df = pd.DataFrame(bars)
        if df.empty or "time" not in df:
            continue
        df = df.copy()
        df["date"] = df["time"].astype(str).str[:10]
        df = df.sort_values("date")
        for col in ("open", "high", "low", "close", "volume"):
            df[col] = pd.to_numeric(df.get(col), errors="coerce")
        dollar_vol = df["close"] * df["volume"]
        df["adv_20d"] = dollar_vol.rolling(adv_window, min_periods=adv_window).mean()
        # PLACEHOLDER (decided: settle in Phase 2): daily range proxy, NOT a bid/ask
        # spread. Nothing reads this until the Phase 2 backtest cost model, which
        # will replace it (Corwin-Schultz high-low estimator vs tiered fixed bps).
        df["spread"] = np.where(df["close"] > 0, (df["high"] - df["low"]) / df["close"], np.nan)
        df["symbol"] = symbol
        frames.append(df[["date", "symbol", *EQUITY_COLUMNS]])
    if not frames:
        return pd.DataFrame(columns=["date", "symbol", *EQUITY_COLUMNS])
    return pd.concat(frames, ignore_index=True)


def equity_frame_for_date(long_frame: pd.DataFrame, as_of: str) -> pd.DataFrame:
    """Slice the long frame to one date, indexed by symbol (``EQUITY_COLUMNS``)."""
    rows = long_frame[long_frame["date"] == as_of]
    out = rows.set_index("symbol")[EQUITY_COLUMNS].sort_index()
    out.index.name = "symbol"
    return out


def current_universe(
    equity_frame: pd.DataFrame, min_price: float, min_adv_usd: float
) -> pd.DataFrame:
    """Apply the SPEC universe filter (price + dollar-ADV) to a per-date frame."""
    mask = (
        (equity_frame["close"] > min_price)
        & (equity_frame["adv_20d"] > min_adv_usd)
        & equity_frame["adv_20d"].notna()
    )
    return equity_frame[mask]


def quarter_label(when: str | _date | datetime) -> str:
    """Return the ``YYYY-QN`` label for a date (e.g. 2026-06-18 -> ``2026-Q2``)."""
    d = _coerce_date(when)
    return f"{d.year}-Q{(d.month - 1) // 3 + 1}"


# ====================================================================== #
# Parquet I/O
# ====================================================================== #
def write_equities_parquet(
    frame: pd.DataFrame, as_of: str, prices_dir: Path = DEFAULT_PRICES_DIR
) -> Path:
    """Write one per-date equities Parquet (indexed by symbol). Overwrites."""
    prices_dir = Path(prices_dir)
    prices_dir.mkdir(parents=True, exist_ok=True)
    path = prices_dir / f"{as_of}.parquet"
    frame[EQUITY_COLUMNS].to_parquet(path)
    return path


def write_equities_by_date(
    long_frame: pd.DataFrame, prices_dir: Path = DEFAULT_PRICES_DIR
) -> list[Path]:
    """Write one Parquet per date present in ``long_frame``. Returns paths."""
    paths: list[Path] = []
    for as_of, group in long_frame.groupby("date"):
        frame = group.set_index("symbol")[EQUITY_COLUMNS].sort_index()
        frame.index.name = "symbol"
        paths.append(write_equities_parquet(frame, str(as_of), prices_dir))
    return paths


def load_equities(as_of: str, prices_dir: Path = DEFAULT_PRICES_DIR) -> pd.DataFrame:
    """Read one per-date equities Parquet back (indexed by symbol)."""
    return pd.read_parquet(Path(prices_dir) / f"{as_of}.parquet")


def write_fundamentals_parquet(
    frame: pd.DataFrame, quarter: str, fundamentals_dir: Path = DEFAULT_FUNDAMENTALS_DIR
) -> Path:
    """Write one per-quarter fundamentals Parquet (indexed by symbol). Overwrites."""
    fundamentals_dir = Path(fundamentals_dir)
    fundamentals_dir.mkdir(parents=True, exist_ok=True)
    path = fundamentals_dir / f"{quarter}.parquet"
    frame[FUNDAMENTAL_COLUMNS].to_parquet(path)
    return path


# ====================================================================== #
# Ingest orchestration (network fetchers injected for testability)
# ====================================================================== #
def ingest_equities(
    client,
    as_of: str,
    symbols: Sequence[str],
    prices_dir: Path = DEFAULT_PRICES_DIR,
    *,
    adv_window: int = DEFAULT_ADV_WINDOW,
    lookback_days: int = 40,
    chunk_size: int = 200,
    sleep_s: float = 0.0,
    backoff_s: Sequence[int] = DEFAULT_BACKOFF_S,
) -> Path:
    """Pull a trailing window of daily bars and write the ``as_of`` equities file.

    ``lookback_days`` calendar days are pulled so ``adv_20d`` has its full
    trailing window; only the ``as_of`` row is written. Symbols are requested in
    ``chunk_size`` batches via :meth:`AlpacaClient.bars_multi`. Returns the path.
    """
    start = (_coerce_date(as_of) - timedelta(days=lookback_days)).isoformat()
    # Alpaca's `end` is an exclusive timestamp boundary and daily bars are stamped
    # at ~04:00Z of their own day, so end=as_of would drop the as_of bar itself.
    # Pull through the next day; equity_frame_for_date slices back to exactly as_of.
    end = (_coerce_date(as_of) + timedelta(days=1)).isoformat()
    bars_by_symbol: dict[str, list[dict]] = {}
    for chunk in _chunks(symbols, chunk_size):
        batch = _with_retries(
            lambda: client.bars_multi(list(chunk), start, end, "1Day"),
            backoff_s,
            label=f"bars_multi[{len(chunk)}]",
        )
        bars_by_symbol.update(batch)
        if sleep_s:
            time.sleep(sleep_s)
    long_frame = bars_to_long_frame(bars_by_symbol, adv_window)
    frame = equity_frame_for_date(long_frame, as_of)
    path = write_equities_parquet(frame, as_of, prices_dir)
    log.info("ingest_equities done", extra={"date": as_of, "rows": int(len(frame)), "path": str(path)})
    return path


def ingest_fundamentals(
    symbols: Sequence[str],
    quarter: str,
    fundamentals_dir: Path = DEFAULT_FUNDAMENTALS_DIR,
    *,
    fetch_one: Callable[[str], dict | None],
    sleep_s: float = 0.0,
    overwrite: bool = False,
) -> Path | None:
    """Fetch and cache fundamentals for ``quarter`` (skips if already cached).

    Per BUILD_ORDER, fundamentals are pulled only if the quarter is not already
    cached. ``fetch_one(symbol)`` returns a dict of ``FUNDAMENTAL_COLUMNS`` (minus
    ``symbol``) or ``None`` to skip. Returns the path, or ``None`` if nothing was
    fetched and no cache exists.
    """
    fundamentals_dir = Path(fundamentals_dir)
    path = fundamentals_dir / f"{quarter}.parquet"
    if path.exists() and not overwrite:
        log.info("fundamentals cached, skipping", extra={"quarter": quarter, "path": str(path)})
        return path

    rows: list[dict] = []
    for symbol in symbols:
        record = fetch_one(symbol)
        if record:
            rows.append({"symbol": symbol, "source": record.get("source", "yfinance"), **record})
        if sleep_s:
            time.sleep(sleep_s)
    if not rows:
        log.warning("fundamentals: no records fetched", extra={"quarter": quarter})
        return None

    frame = pd.DataFrame(rows).set_index("symbol")
    for col in FUNDAMENTAL_COLUMNS:
        if col not in frame:
            frame[col] = np.nan
    frame.index.name = "symbol"
    out = write_fundamentals_parquet(frame, quarter, fundamentals_dir)
    log.info("ingest_fundamentals done", extra={"quarter": quarter, "rows": int(len(frame)), "path": str(out)})
    return out


# ====================================================================== #
# Default live fetchers (used in production; replaced by mocks in tests)
# ====================================================================== #
def fetch_fundamentals_yf(symbol: str) -> dict | None:
    """Pull current fundamentals for ``symbol`` from yfinance.

    Returns the ``FUNDAMENTAL_COLUMNS`` payload, or ``None`` on any failure
    (a single bad ticker never halts the batch).
    """
    try:
        import yfinance as yf

        info = yf.Ticker(symbol).info or {}
    except Exception as exc:  # yfinance is an unofficial scraper — be defensive
        log.warning("yfinance fetch failed", extra={"symbol": symbol, "error": str(exc)})
        return None
    return {
        "pe_ratio": info.get("trailingPE"),
        "pb_ratio": info.get("priceToBook"),
        "roe": info.get("returnOnEquity"),
        "gross_margin": info.get("grossMargins"),
        "report_date": _date.today().isoformat(),
        "source": "yfinance",
    }


def is_trading_day(client, as_of: str) -> bool:
    """Whether ``as_of`` is an NYSE trading day per Alpaca's calendar."""
    return bool(client.market_calendar(as_of, as_of))


def tradable_equity_universe(client) -> list[str]:
    """Discover the active, tradable US-equity symbol universe from Alpaca."""
    assets = client.list_assets(status="active", asset_class="us_equity")
    return sorted(a["symbol"] for a in assets if a.get("tradable"))


# ====================================================================== #
# Daily entry point
# ====================================================================== #
def run_daily_ingest(env: str = "paper", as_of: str | None = None) -> None:
    """Run the daily incremental pull (prices always; fundamentals if new quarter)."""
    from engine import config

    config.load_env(env)
    settings = config.load_settings()
    client = config.get_alpaca_client()

    as_of = as_of or _date.today().isoformat()
    if not is_trading_day(client, as_of):
        log.info("not a trading day, skipping ingest", extra={"date": as_of})
        return

    ing = settings.ingest
    symbols = tradable_equity_universe(client)
    log.info("daily ingest start", extra={"date": as_of, "env": env, "symbols": len(symbols)})

    ingest_equities(
        client,
        as_of,
        symbols,
        DEFAULT_PRICES_DIR,
        adv_window=ing.adv_window,
        lookback_days=ing.lookback_days,
        chunk_size=ing.symbol_chunk_size,
        sleep_s=ing.request_sleep_s,
        backoff_s=tuple(ing.retry_backoff_s),
    )
    ingest_fundamentals(
        symbols,
        quarter_label(as_of),
        DEFAULT_FUNDAMENTALS_DIR,
        fetch_one=fetch_fundamentals_yf,
        sleep_s=ing.request_sleep_s,
    )


# ====================================================================== #
# Internal helpers
# ====================================================================== #
def _chunks(seq: Sequence[str], n: int) -> Iterable[Sequence[str]]:
    """Yield successive ``n``-sized chunks of ``seq``."""
    n = max(1, n)
    for i in range(0, len(seq), n):
        yield seq[i : i + n]


def _with_retries(fn: Callable, backoff_s: Sequence[int], label: str = ""):
    """Call ``fn`` with retries on any exception, sleeping per ``backoff_s``.

    One attempt plus ``len(backoff_s)`` retries (30s/60s/120s by default). The
    final failure re-raises so the scheduler cancels downstream jobs and alerts.
    """
    attempts = len(backoff_s) + 1
    for attempt in range(attempts):
        try:
            return fn()
        except Exception as exc:
            if attempt == attempts - 1:
                log.error("retries exhausted", extra={"call": label, "error": str(exc)})
                raise
            wait = backoff_s[attempt]
            log.warning("call failed, backing off", extra={"call": label, "attempt": attempt + 1, "wait_s": wait, "error": str(exc)})
            time.sleep(wait)


def _coerce_date(when: str | _date | datetime) -> _date:
    """Coerce an ISO string / datetime / date to a ``date``."""
    if isinstance(when, datetime):
        return when.date()
    if isinstance(when, _date):
        return when
    return datetime.fromisoformat(str(when)[:10]).date()


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="sharpe-engine daily ingest")
    parser.add_argument("--env", choices=("paper", "live"), default="paper")
    parser.add_argument("--date", default=None, help="ISO date (default: today)")
    args = parser.parse_args()
    run_daily_ingest(env=args.env, as_of=args.date)
