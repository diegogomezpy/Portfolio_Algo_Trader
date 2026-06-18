"""One-time historical data backfill (~2016 to present).

Runs once before anything else; NOT part of the daily pipeline. Pulls:

* **Equities** — full-universe daily OHLCV from Alpaca (adjustment='all'),
  processed one calendar year at a time (with a trailing lookback so ``adv_20d``
  is correct at each year boundary) to bound memory, then written per-date via
  the SAME helpers the daily ingest uses — guaranteeing identical schema.

* **Fundamentals** — historical quarterly P/E, P/B, ROE, and gross margin
  derived from yfinance ``quarterly_financials`` + ``quarterly_balance_sheet``
  per ticker (DECISIONS D7), written per-quarter and cached (skipped if present).

Idempotent and rate-limit aware (chunked requests, sleeps, retry/backoff). Safe
to re-run; equity files overwrite, cached fundamental quarters are skipped.

    python scripts/backfill.py --env paper
    python scripts/backfill.py --env paper --start 2016-01-01 --symbols AAPL,MSFT

Note: the fundamentals derivation is best-effort against yfinance's
version-variable statement labels; a bad ticker is skipped, never fatal.
"""

from __future__ import annotations

import argparse
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from engine import config, ingest  # noqa: E402
from engine.logger import get_logger  # noqa: E402

log = get_logger("backfill")


def backfill_equities(
    client,
    start: str,
    end: str,
    symbols: list[str],
    prices_dir: Path = ingest.DEFAULT_PRICES_DIR,
    *,
    adv_window: int = ingest.DEFAULT_ADV_WINDOW,
    chunk_size: int = 200,
    sleep_s: float = 0.2,
    backoff_s=ingest.DEFAULT_BACKOFF_S,
) -> int:
    """Backfill per-date equity Parquet files for ``[start, end]``. Returns files written."""
    start_d = datetime.fromisoformat(start).date()
    end_d = datetime.fromisoformat(end).date()
    files_written = 0

    for year in range(start_d.year, end_d.year + 1):
        year_start = max(date(year, 1, 1), start_d)
        window_end = min(date(year, 12, 31), end_d)
        # Pull extra lookback so adv_20d is defined from the first day of the year.
        pull_start = (year_start - timedelta(days=adv_window * 2 + 15)).isoformat()
        # end is exclusive at the bar timestamp (daily bars stamped ~04:00Z), so pull
        # through the day after window_end; the date filter below trims the overshoot.
        pull_end = (window_end + timedelta(days=1)).isoformat()

        parts: list[pd.DataFrame] = []
        for chunk in ingest._chunks(symbols, chunk_size):
            bars = ingest._with_retries(
                lambda: client.bars_multi(list(chunk), pull_start, pull_end, "1Day"),
                backoff_s,
                label=f"backfill bars {year} [{len(chunk)}]",
            )
            lf = ingest.bars_to_long_frame(bars, adv_window)
            lf = lf[(lf["date"] >= year_start.isoformat()) & (lf["date"] <= window_end.isoformat())]
            if not lf.empty:
                parts.append(lf)
            if sleep_s:
                time.sleep(sleep_s)

        if parts:
            year_long = pd.concat(parts, ignore_index=True)
            written = ingest.write_equities_by_date(year_long, prices_dir)
            files_written += len(written)
            log.info("backfilled year", extra={"year": year, "files": len(written)})
    return files_written


def derive_historical_fundamentals_yf(symbol: str) -> list[dict]:
    """Derive per-quarter fundamentals for ``symbol`` from yfinance statements.

    Returns a list of records (one per available quarter) shaped like
    ``ingest.FUNDAMENTAL_COLUMNS`` plus ``symbol`` and ``quarter``. Best-effort:
    returns ``[]`` on any failure or missing data rather than raising.
    """
    try:
        import yfinance as yf

        ticker = yf.Ticker(symbol)
        income = ticker.quarterly_financials
        balance = ticker.quarterly_balance_sheet
        if income is None or income.empty:
            return []
        info = ticker.info or {}
        shares = info.get("sharesOutstanding")
        prices = ticker.history(period="max", interval="1d")
    except Exception as exc:
        log.warning("yfinance statements failed", extra={"symbol": symbol, "error": str(exc)})
        return []

    records: list[dict] = []
    # yfinance returns quarters newest-first; sort ascending so the trailing-4Q
    # window in _trailing_sum looks back in time, not forward (avoids look-ahead
    # bias and the 1-quarter-EPS bug that inflated P/E ~4x).
    quarter_ends = sorted(income.columns)
    for i, qend in enumerate(quarter_ends):
        try:
            qdate = pd.Timestamp(qend).date()
            revenue = _row(income, ("Total Revenue", "TotalRevenue"), qend)
            gross_profit = _row(income, ("Gross Profit", "GrossProfit"), qend)
            net_income = _row(income, ("Net Income", "NetIncome"), qend)
            equity = _row(balance, ("Stockholders Equity", "Total Stockholder Equity",
                                    "StockholdersEquity"), qend)

            gross_margin = _safe_div(gross_profit, revenue)
            roe = _safe_div(net_income, equity)
            # Trailing-4Q EPS for P/E; book value per share for P/B.
            trailing_ni = _trailing_sum(income, ("Net Income", "NetIncome"), quarter_ends, i, 4)
            eps = _safe_div(trailing_ni, shares)
            bvps = _safe_div(equity, shares)
            px = _price_on_or_before(prices, qdate)
            pe_ratio = _safe_div(px, eps)
            pb_ratio = _safe_div(px, bvps)

            records.append({
                "symbol": symbol,
                "quarter": ingest.quarter_label(qdate),
                "pe_ratio": pe_ratio,
                "pb_ratio": pb_ratio,
                "roe": roe,
                "gross_margin": gross_margin,
                "report_date": qdate.isoformat(),
                "source": "yfinance",
            })
        except Exception:
            continue
    return records


def backfill_fundamentals(
    symbols: list[str],
    fundamentals_dir: Path = ingest.DEFAULT_FUNDAMENTALS_DIR,
    *,
    sleep_s: float = 0.5,
) -> int:
    """Backfill per-quarter fundamentals for ``symbols``. Returns quarter files written."""
    by_quarter: dict[str, list[dict]] = {}
    for symbol in symbols:
        for rec in derive_historical_fundamentals_yf(symbol):
            by_quarter.setdefault(rec["quarter"], []).append(rec)
        if sleep_s:
            time.sleep(sleep_s)

    written = 0
    for quarter, rows in by_quarter.items():
        frame = pd.DataFrame(rows).set_index("symbol")
        frame.index.name = "symbol"
        ingest.write_fundamentals_parquet(frame, quarter, fundamentals_dir)
        written += 1
    log.info("fundamentals backfill done", extra={"quarters": written, "symbols": len(symbols)})
    return written


def liquid_universe(
    prices_dir: Path = ingest.DEFAULT_PRICES_DIR,
    *,
    min_price: float,
    min_adv_usd: float,
) -> list[str]:
    """Symbols passing the SPEC liquidity filter on the most recent backfilled date.

    Reads the latest per-date equity Parquet and applies ``ingest.current_universe``
    (price + dollar-ADV). Used to scope the fundamentals backfill to investable
    names rather than the full ~13k tradable universe (mostly ETFs/micro-caps we
    never trade). Returns a sorted symbol list; empty if no equity files exist.

    Note: this is the *current* liquid snapshot. Names that were liquid years ago
    but are not today won't get fundamentals — a survivorship caveat to revisit
    when the Phase 2 backtest needs point-in-time fundamental coverage.
    """
    prices_dir = Path(prices_dir)
    files = sorted(prices_dir.glob("*.parquet"))
    if not files:
        return []
    # The current-day file may hold only a partial (intraday) bar — daily ingest
    # runs EOD — which undercounts the universe (not all names have printed yet).
    # Prefer the latest completed trading day strictly before today.
    today = date.today().isoformat()
    completed = [f for f in files if f.stem < today]
    chosen = completed[-1] if completed else files[-1]
    latest = ingest.load_equities(chosen.stem, prices_dir)
    kept = ingest.current_universe(latest, min_price=min_price, min_adv_usd=min_adv_usd)
    return sorted(kept.index)


def main() -> None:
    parser = argparse.ArgumentParser(description="sharpe-engine one-time backfill")
    parser.add_argument("--env", choices=("paper", "live"), default="paper")
    parser.add_argument("--start", default=None, help="ISO start date (default: settings.ingest.backfill_start)")
    parser.add_argument("--end", default=None, help="ISO end date (default: today)")
    parser.add_argument("--symbols", default=None, help="Comma-separated subset (default: full universe)")
    parser.add_argument("--skip-fundamentals", action="store_true")
    parser.add_argument("--skip-equities", action="store_true")
    parser.add_argument(
        "--fundamentals-universe",
        choices=("all", "liquid"),
        default="all",
        help="symbols to pull fundamentals for: 'all' (full universe) or 'liquid' "
        "(ADV>$1M, price>$5, from the latest backfilled equities) to skip the "
        "~13k illiquid/ETF names we never trade",
    )
    args = parser.parse_args()

    config.load_env(args.env)
    settings = config.load_settings()
    client = config.get_alpaca_client()

    start = args.start or settings.ingest.backfill_start
    end = args.end or date.today().isoformat()
    symbols = (
        [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
        if args.symbols
        else ingest.tradable_equity_universe(client)
    )
    log.info("backfill start", extra={"start": start, "end": end, "symbols": len(symbols), "env": args.env})

    if not args.skip_equities:
        files = backfill_equities(
            client, start, end, symbols,
            adv_window=settings.ingest.adv_window,
            chunk_size=settings.ingest.symbol_chunk_size,
            sleep_s=settings.ingest.request_sleep_s,
            backoff_s=tuple(settings.ingest.retry_backoff_s),
        )
        log.info("equities backfill complete", extra={"files": files})

    if not args.skip_fundamentals:
        if args.fundamentals_universe == "liquid":
            fund_symbols = liquid_universe(
                min_price=settings.universe.min_price,
                min_adv_usd=settings.universe.min_adv_usd,
            )
            log.info("fundamentals scoped to liquid universe", extra={"symbols": len(fund_symbols)})
        else:
            fund_symbols = symbols
        quarters = backfill_fundamentals(fund_symbols, sleep_s=settings.ingest.request_sleep_s)
        log.info("fundamentals backfill complete", extra={"quarters": quarters})

    print("Backfill complete.")


# ---- yfinance statement parsing helpers (tolerant of label/version drift) ---- #
def _row(frame, labels: tuple[str, ...], column) -> float | None:
    """Return the value at one of ``labels`` for ``column`` in a statement frame."""
    if frame is None or frame.empty:
        return None
    for label in labels:
        if label in frame.index:
            try:
                value = frame.loc[label, column]
                return None if pd.isna(value) else float(value)
            except Exception:
                continue
    return None


def _trailing_sum(frame, labels, columns, end_idx: int, n: int) -> float | None:
    """Sum a line item over the ``n`` quarters ending at ``end_idx`` (inclusive).

    ``columns`` must be ascending (oldest→newest) so the window is
    ``[end_idx-n+1 .. end_idx]`` — i.e. looking back in time. Returns ``None``
    unless all ``n`` quarters are present and non-null; partial windows (the first
    few quarters of available history) yield no value rather than an understated,
    misleading sum that would distort the derived P/E.
    """
    if end_idx - n + 1 < 0:
        return None
    cols = columns[end_idx - n + 1 : end_idx + 1]
    vals = [_row(frame, labels, c) for c in cols]
    vals = [v for v in vals if v is not None]
    return sum(vals) if len(vals) == n else None


def _price_on_or_before(prices, when: date) -> float | None:
    """Return the close on or just before ``when`` from a yfinance history frame."""
    if prices is None or prices.empty or "Close" not in prices:
        return None
    ts = pd.Timestamp(when)
    idx = prices.index
    try:
        naive = idx.tz_localize(None) if idx.tz is not None else idx
        eligible = prices.loc[naive <= ts]
        return float(eligible["Close"].iloc[-1]) if not eligible.empty else None
    except Exception:
        return None


def _safe_div(num, den) -> float | None:
    """Divide guarding None / zero / NaN."""
    try:
        if num is None or den is None or den == 0 or pd.isna(num) or pd.isna(den):
            return None
        return float(num) / float(den)
    except Exception:
        return None


if __name__ == "__main__":
    main()
