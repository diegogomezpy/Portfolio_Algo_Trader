"""Unit tests for engine.instruments — company name + sector reference (no network/files)."""

from __future__ import annotations

import pandas as pd

from engine import instruments


def _company_tickers():
    return {"0": {"ticker": "AAPL", "cik_str": 320193, "title": "APPLE INC."},
            "1": {"ticker": "msft", "cik_str": 789019, "title": "MICROSOFT CORP"}}


def test_load_company_names_fetches_prettifies_and_caches(tmp_path):
    calls = []
    def fake_fetch(url):
        calls.append(url)
        return _company_tickers()
    path = tmp_path / "names.parquet"
    names = instruments.load_company_names(fetch=fake_fetch, path=path)
    assert names["AAPL"] == "Apple Inc." and names["MSFT"] == "Microsoft Corp"   # title-cased, ticker upper
    assert path.exists() and len(calls) == 1
    # second call hits the cache — no fetch
    calls.clear()
    again = instruments.load_company_names(fetch=fake_fetch, path=path)
    assert again["AAPL"] == "Apple Inc." and calls == []


def test_load_company_names_offline_returns_empty(tmp_path):
    def boom(url):
        raise RuntimeError("offline")
    assert instruments.load_company_names(fetch=boom, path=tmp_path / "missing.parquet") == {}


def test_reference_map_merges_name_sector_industry():
    smap = pd.DataFrame({"sector": ["Information Technology"], "sic_description": ["Electronic Computers"]},
                        index=pd.Index(["AAPL"], name="symbol"))
    ref = instruments.reference_map(["aapl", "ZZZZ"], names={"AAPL": "Apple Inc."},
                                    sector_loader=lambda: smap)
    assert ref["AAPL"] == {"name": "Apple Inc.", "sector": "Information Technology",
                           "industry": "Electronic Computers"}
    assert ref["ZZZZ"] == {"name": None, "sector": None, "industry": None}   # unknown → all None


def test_reference_map_name_override_wins():
    # the fix-up table corrects SEC's mangled casing and beats the derived name
    ref = instruments.reference_map(["JPM", "KO"],
                                    names={"JPM": "Jpmorgan Chase & Co", "KO": "Coca Cola Co"},
                                    sector_loader=lambda: __import__("pandas").DataFrame())
    assert ref["JPM"]["name"] == "JPMorgan Chase & Co"
    assert ref["KO"]["name"] == "Coca-Cola Co"


def test_reference_map_survives_missing_sector_cache():
    def no_sectors():
        raise FileNotFoundError("no sectors.parquet")
    ref = instruments.reference_map(["AAPL"], names={"AAPL": "Apple Inc."}, sector_loader=no_sectors)
    assert ref["AAPL"] == {"name": "Apple Inc.", "sector": None, "industry": None}
