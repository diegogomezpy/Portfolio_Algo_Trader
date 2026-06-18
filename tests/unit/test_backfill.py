"""Unit tests for scripts/backfill.py derivation helpers (no network).

Focus: the trailing-4Q window must look *backward* in time. yfinance returns
quarters newest-first; a regression here silently summed too few quarters and
inflated derived P/E ~4x.
"""

from __future__ import annotations

import importlib.util
from datetime import date
from pathlib import Path

import pandas as pd

from engine import ingest

_BF = Path(__file__).resolve().parents[2] / "scripts" / "backfill.py"
_spec = importlib.util.spec_from_file_location("backfill_mod", _BF)
backfill = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(backfill)

# Ascending (oldest -> newest), as derive_historical_fundamentals_yf normalises.
COLS = ["2025-03-31", "2025-06-30", "2025-09-30", "2025-12-31", "2026-03-31"]


def _income(values):
    """One-row income frame: columns are quarter dates, value is Net Income."""
    return pd.DataFrame({c: [v] for c, v in zip(COLS, values)}, index=["Net Income"])


def test_trailing_sum_looks_backward():
    frame = _income([10, 20, 30, 40, 50])
    # newest quarter (idx 4): trailing 4 = 20+30+40+50
    assert backfill._trailing_sum(frame, ("Net Income",), COLS, 4, 4) == 140
    # idx 3: 10+20+30+40
    assert backfill._trailing_sum(frame, ("Net Income",), COLS, 3, 4) == 100


def test_trailing_sum_requires_full_window():
    frame = _income([10, 20, 30, 40, 50])
    # fewer than 4 trailing quarters available -> None, not an understated sum
    assert backfill._trailing_sum(frame, ("Net Income",), COLS, 0, 4) is None
    assert backfill._trailing_sum(frame, ("Net Income",), COLS, 2, 4) is None


def test_trailing_sum_none_on_missing_quarter():
    frame = _income([10, 20, None, 40, 50])
    # window [idx1..idx4] includes the missing quarter -> None
    assert backfill._trailing_sum(frame, ("Net Income",), COLS, 4, 4) is None


def _equity(rows):
    """Per-date equity frame (symbol-indexed) from {symbol: (close, adv_20d)}."""
    data = {
        "open": [], "high": [], "low": [], "close": [],
        "volume": [], "adv_20d": [], "spread": [],
    }
    idx = []
    for sym, (close, adv) in rows.items():
        idx.append(sym)
        data["open"].append(close); data["high"].append(close)
        data["low"].append(close); data["close"].append(close)
        data["volume"].append(1.0); data["adv_20d"].append(adv)
        data["spread"].append(0.0)
    frame = pd.DataFrame(data, index=pd.Index(idx, name="symbol"))
    return frame[ingest.EQUITY_COLUMNS]


def test_liquid_universe_uses_completed_day_not_partial_today(tmp_path):
    # Completed prior day: AAA liquid, PENNY fails price, THIN fails ADV.
    prior = _equity({"AAA": (10.0, 5e6), "PENNY": (1.0, 5e6), "THIN": (10.0, 1e5)})
    ingest.write_equities_parquet(prior, "2020-01-02", tmp_path)
    # Today's file holds only a partial bar (AAA hasn't printed yet) — using it
    # would wrongly drop AAA from the universe.
    today = date.today().isoformat()
    ingest.write_equities_parquet(prior.loc[["THIN"]], today, tmp_path)

    liq = backfill.liquid_universe(tmp_path, min_price=5.0, min_adv_usd=1e6)
    assert liq == ["AAA"]  # from the completed prior day, not today's partial
