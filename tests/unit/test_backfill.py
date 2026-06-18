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
import pytest

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


def _rec(symbol, quarter):
    """One derived-fundamental record, shaped like derive_historical_fundamentals_yf."""
    return {
        "symbol": symbol, "quarter": quarter, "pe_ratio": 10.0, "pb_ratio": 2.0,
        "roe": 0.15, "gross_margin": 0.4, "report_date": "2025-03-31", "source": "yfinance",
    }


def test_backfill_fundamentals_resumes_and_merges(tmp_path):
    # A symbol from a prior run already sits on disk (e.g. the proving set).
    seed = pd.DataFrame([_rec("OLD", "2025-Q1")]).set_index("symbol")
    ingest.write_fundamentals_parquet(seed, "2025-Q1", tmp_path)

    derived = []

    def fake_derive(symbol):
        derived.append(symbol)
        return [_rec(symbol, "2025-Q1")]

    backfill.backfill_fundamentals(
        ["OLD", "NEW"], tmp_path, sleep_s=0, flush_every=1, derive=fake_derive
    )
    assert derived == ["NEW"]  # OLD already on disk -> skipped (resume)
    out = pd.read_parquet(tmp_path / "2025-Q1.parquet")
    assert set(out.index) == {"OLD", "NEW"}  # merge kept OLD, added NEW (no clobber)


def test_backfill_fundamentals_checkpoint_survives_interruption(tmp_path):
    # Flush after every symbol; the second symbol blows up mid-run.
    def derive_then_die(symbol):
        if symbol == "B":
            raise RuntimeError("network died")
        return [_rec(symbol, "2025-Q1")]

    with pytest.raises(RuntimeError):
        backfill.backfill_fundamentals(
            ["A", "B"], tmp_path, sleep_s=0, flush_every=1, derive=derive_then_die
        )
    # A was checkpointed before B failed -> durable on disk, not lost.
    assert list(pd.read_parquet(tmp_path / "2025-Q1.parquet").index) == ["A"]

    # Re-run resumes: A is skipped, only the previously-failed B is retried.
    retried = []

    def derive_ok(symbol):
        retried.append(symbol)
        return [_rec(symbol, "2025-Q1")]

    backfill.backfill_fundamentals(
        ["A", "B"], tmp_path, sleep_s=0, flush_every=1, derive=derive_ok
    )
    assert retried == ["B"]
    assert set(pd.read_parquet(tmp_path / "2025-Q1.parquet").index) == {"A", "B"}


class _RecordingClient:
    """Fake Alpaca client that records each bars_multi window and returns nothing."""

    def __init__(self):
        self.windows = []  # list of (start, end) pull windows requested

    def bars_multi(self, symbols, start, end, timeframe):
        self.windows.append((start, end))
        return {}  # no bars -> backfill writes nothing, but the call is recorded


def test_year_has_files_detects_year_on_disk(tmp_path):
    ingest.write_equities_parquet(_equity({"AAA": (10.0, 5e6)}), "2021-06-15", tmp_path)
    assert backfill._year_has_files(tmp_path, 2021) is True
    assert backfill._year_has_files(tmp_path, 2022) is False


def test_backfill_equities_resume_skips_completed_year(tmp_path):
    # 2021 already on disk (a prior run), 2022 missing. Both are past years.
    ingest.write_equities_parquet(_equity({"AAA": (10.0, 5e6)}), "2021-06-15", tmp_path)
    client = _RecordingClient()

    backfill.backfill_equities(client, "2021-01-01", "2022-12-31", ["AAA"], tmp_path, sleep_s=0)

    # Only 2022 was pulled; 2021 skipped. 2022's window ends exclusively at 2023-01-01.
    assert len(client.windows) == 1
    assert client.windows[0][1] == "2023-01-01"


def test_backfill_equities_no_resume_repulls_every_year(tmp_path):
    ingest.write_equities_parquet(_equity({"AAA": (10.0, 5e6)}), "2021-06-15", tmp_path)
    client = _RecordingClient()

    backfill.backfill_equities(
        client, "2021-01-01", "2022-12-31", ["AAA"], tmp_path, sleep_s=0, resume=False
    )

    # resume disabled -> both 2021 and 2022 pulled despite 2021 being on disk.
    assert len(client.windows) == 2
