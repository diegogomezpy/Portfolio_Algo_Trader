"""Unit tests for engine.ingest — mocked Alpaca + yfinance, no network.

Covers the Phase 0 gate criteria: correct Parquet schema, no forward-looking
adv computation, idempotent overwrite, and fundamentals caching.
"""

from __future__ import annotations

import math

import pandas as pd
import pytest

from engine import ingest

DATES = ["2026-06-15", "2026-06-16", "2026-06-17", "2026-06-18"]
AS_OF = "2026-06-18"


def _make_bars(closes, volumes):
    """Build a list of normalised daily bars (high/low ±5 around close)."""
    return [
        {
            "time": f"{d}T00:00:00Z",
            "open": c,
            "high": c + 5,
            "low": c - 5,
            "close": c,
            "volume": v,
        }
        for d, c, v in zip(DATES, closes, volumes)
    ]


@pytest.fixture
def bars_by_symbol():
    return {
        "AAPL": _make_bars([100, 110, 120, 130], [10, 10, 10, 10]),
        "MSFT": _make_bars([200, 210, 220, 230], [5, 5, 5, 5]),
    }


class MockAlpaca:
    """Stand-in for AlpacaClient.bars_multi that honors Alpaca's date range.

    Crucially models that Alpaca's ``end`` is an *exclusive* timestamp boundary:
    a daily bar dated ``D`` is only returned when ``end`` is strictly after ``D``
    (in practice ``end >= D+1``). If the mock ignored ``end``, the ingest path's
    end-boundary handling would go untested — that exact gap hid a real bug where
    ``ingest_equities`` dropped the as_of bar.
    """

    def __init__(self, bars):
        self._bars = bars

    def bars_multi(self, symbols, start, end, timeframe="1Day"):
        s_day, e_day = start[:10], end[:10]
        return {
            s: [b for b in self._bars[s] if s_day <= b["time"][:10] < e_day]
            for s in symbols
            if s in self._bars
        }


# ----------------------------- pure compute ------------------------------- #
def test_long_frame_schema(bars_by_symbol):
    long = ingest.bars_to_long_frame(bars_by_symbol, adv_window=3)
    assert list(long.columns) == ["date", "symbol", *ingest.EQUITY_COLUMNS]
    assert set(long["symbol"]) == {"AAPL", "MSFT"}


def test_adv_20d_is_trailing_dollar_volume(bars_by_symbol):
    long = ingest.bars_to_long_frame(bars_by_symbol, adv_window=3)
    aapl = long[long["symbol"] == "AAPL"].set_index("date")
    # window=3, min_periods=3 → first two days undefined (no forward-looking)
    assert math.isnan(aapl.loc["2026-06-15", "adv_20d"])
    assert math.isnan(aapl.loc["2026-06-16", "adv_20d"])
    # day 3 = mean dollar-vol of d1..d3 = mean(1000,1100,1200) = 1100
    assert aapl.loc["2026-06-17", "adv_20d"] == pytest.approx(1100.0)
    # day 4 = mean(1100,1200,1300) = 1200
    assert aapl.loc["2026-06-18", "adv_20d"] == pytest.approx(1200.0)


def test_spread_is_range_over_close(bars_by_symbol):
    long = ingest.bars_to_long_frame(bars_by_symbol, adv_window=3)
    aapl = long[long["symbol"] == "AAPL"].set_index("date")
    assert aapl.loc["2026-06-18", "spread"] == pytest.approx(10.0 / 130.0)


def test_equity_frame_for_date(bars_by_symbol):
    long = ingest.bars_to_long_frame(bars_by_symbol, adv_window=3)
    frame = ingest.equity_frame_for_date(long, AS_OF)
    assert list(frame.columns) == ingest.EQUITY_COLUMNS
    assert frame.index.name == "symbol"
    assert list(frame.index) == ["AAPL", "MSFT"]
    assert frame.loc["MSFT", "close"] == 230


def test_current_universe_filter(bars_by_symbol):
    long = ingest.bars_to_long_frame(bars_by_symbol, adv_window=3)
    frame = ingest.equity_frame_for_date(long, AS_OF)
    # AAPL adv=1200, MSFT adv=mean(1050,1100,1150)=1100; filter adv>1150 keeps AAPL only
    kept = ingest.current_universe(frame, min_price=5.0, min_adv_usd=1150.0)
    assert list(kept.index) == ["AAPL"]


def test_quarter_label():
    assert ingest.quarter_label("2026-06-18") == "2026-Q2"
    assert ingest.quarter_label("2026-01-01") == "2026-Q1"
    assert ingest.quarter_label("2026-12-31") == "2026-Q4"


# ------------------------- I/O round-trip + idempotency ------------------- #
def test_write_load_roundtrip_and_schema(bars_by_symbol, tmp_path):
    long = ingest.bars_to_long_frame(bars_by_symbol, adv_window=3)
    frame = ingest.equity_frame_for_date(long, AS_OF)
    ingest.write_equities_parquet(frame, AS_OF, tmp_path)
    loaded = ingest.load_equities(AS_OF, tmp_path)
    assert list(loaded.columns) == ingest.EQUITY_COLUMNS
    assert loaded.index.name == "symbol"
    assert len(loaded) == 2


def test_rerun_overwrites_not_duplicates(bars_by_symbol, tmp_path):
    long = ingest.bars_to_long_frame(bars_by_symbol, adv_window=3)
    frame = ingest.equity_frame_for_date(long, AS_OF)
    ingest.write_equities_parquet(frame, AS_OF, tmp_path)
    ingest.write_equities_parquet(frame, AS_OF, tmp_path)  # second run, same date
    loaded = ingest.load_equities(AS_OF, tmp_path)
    assert len(loaded) == 2  # overwrite, not append


# --------------------------- ingest with mock client --------------------- #
def test_ingest_equities_writes_as_of(bars_by_symbol, tmp_path):
    client = MockAlpaca(bars_by_symbol)
    path = ingest.ingest_equities(
        client, AS_OF, ["AAPL", "MSFT"], tmp_path, adv_window=3, chunk_size=1
    )
    assert path.exists()
    loaded = ingest.load_equities(AS_OF, tmp_path)
    assert list(loaded.index) == ["AAPL", "MSFT"]
    assert loaded.loc["AAPL", "adv_20d"] == pytest.approx(1200.0)


def test_ingest_pulls_through_as_of_exclusive_end(bars_by_symbol, tmp_path):
    """Regression: Alpaca's ``end`` is exclusive, so ingest must request through
    ``as_of + 1`` or it silently drops the as_of bar and writes an empty file.
    With the boundary-aware MockAlpaca, end=as_of would yield an empty frame."""
    client = MockAlpaca(bars_by_symbol)
    ingest.ingest_equities(
        client, AS_OF, ["AAPL", "MSFT"], tmp_path, adv_window=3, chunk_size=1
    )
    loaded = ingest.load_equities(AS_OF, tmp_path)
    assert not loaded.empty  # the as_of bar survived the exclusive end boundary
    assert loaded.loc["AAPL", "close"] == 130  # value from the AS_OF bar itself


# ------------------------------ fundamentals ----------------------------- #
def test_ingest_fundamentals_writes_and_caches(tmp_path):
    calls = {"n": 0}

    def fake_fetch(symbol):
        calls["n"] += 1
        return {
            "pe_ratio": 20.0,
            "pb_ratio": 5.0,
            "roe": 0.3,
            "gross_margin": 0.45,
            "report_date": "2026-06-18",
            "source": "mock",
        }

    quarter = "2026-Q2"
    path = ingest.ingest_fundamentals(
        ["AAPL", "MSFT"], quarter, tmp_path, fetch_one=fake_fetch
    )
    assert path is not None and path.exists()
    loaded = pd.read_parquet(path)
    assert list(loaded.columns) == ingest.FUNDAMENTAL_COLUMNS
    assert set(loaded.index) == {"AAPL", "MSFT"}
    assert calls["n"] == 2

    # Second run: quarter already cached → no new fetches.
    ingest.ingest_fundamentals(["AAPL", "MSFT"], quarter, tmp_path, fetch_one=fake_fetch)
    assert calls["n"] == 2


def test_write_fundamentals_sanitizes_infinity_and_junk(tmp_path):
    """Regression (2026-07-08 prod): a STRING 'Infinity' (or float inf) in a numeric ratio made the
    column object-dtype and pyarrow aborted the whole ingest. write_fundamentals_parquet must coerce
    numeric fields (junk/inf → NaN) so the write always succeeds and the data stays clean."""
    import numpy as np
    import pandas as pd
    from engine import ingest
    frame = pd.DataFrame({
        "symbol": ["AAA", "BBB", "CCC"],
        "pe_ratio": ["Infinity", 15.0, "n/a"],   # str inf, real, junk — object column
        "pb_ratio": [np.inf, 2.0, 3.0],           # float inf
        "roe": [0.2, 0.1, 0.15], "gross_margin": [0.4, 0.5, 0.6],
        "report_date": ["2026-06-30"] * 3, "source": ["yfinance"] * 3,
    }).set_index("symbol")
    path = ingest.write_fundamentals_parquet(frame, "2026Q2", tmp_path)   # must not raise
    out = pd.read_parquet(path)
    assert str(out["pe_ratio"].dtype).startswith("float")                 # now numeric
    assert pd.isna(out.loc["AAA", "pe_ratio"]) and pd.isna(out.loc["CCC", "pe_ratio"])  # inf/junk → NaN
    assert out.loc["BBB", "pe_ratio"] == 15.0                             # real value preserved
    assert pd.isna(out.loc["AAA", "pb_ratio"])                            # float inf → NaN
    assert out.loc["AAA", "source"] == "yfinance"                        # strings untouched
