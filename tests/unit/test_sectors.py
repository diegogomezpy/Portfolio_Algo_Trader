"""Unit tests for engine.sectors — pure SIC→sector crosswalk + injectable build.

Guards the cross-overs that actually move a diversification cap (pharma vs.
chemicals, computers/semis/software vs. machinery, REITs vs. holding companies,
motor vehicles vs. transport equipment) and the graceful handling of names with
no CIK / no SIC. No network: the SEC fetch is injected.
"""

from __future__ import annotations

import pandas as pd
import pytest

from engine import sectors


# Representative real SIC codes → expected bucket. The first six are the
# cross-overs that must beat their two-digit major group.
@pytest.mark.parametrize("sic, expected", [
    (2834, "Health Care"),              # pharmaceutical preparations (not Materials/chemicals)
    (3674, "Information Technology"),    # semiconductors (not Industrials/machinery)
    (7372, "Information Technology"),    # prepackaged software (not Industrials/services)
    (3711, "Consumer Discretionary"),   # motor vehicles (not Industrials/transport equip)
    (6798, "Real Estate"),              # REIT (not Financials/holding offices)
    (5912, "Consumer Staples"),         # drug stores (not Consumer Discretionary/retail)
    (6021, "Financials"),               # national commercial banks
    (1311, "Energy"),                   # crude petroleum & natural gas
    (2911, "Energy"),                   # petroleum refining
    (4911, "Utilities"),                # electric services
    (4813, "Communication Services"),   # telephone communications
    (4512, "Industrials"),              # air transportation, scheduled
    (1040, "Materials"),                # gold mining
    (2000, "Consumer Staples"),         # food & kindred products
    (3826, "Health Care"),              # laboratory analytical instruments
    (8742, "Industrials"),              # management consulting services
])
def test_sic_to_sector_known_codes(sic, expected):
    assert sectors.sic_to_sector(sic) == expected


def test_sic_to_sector_accepts_strings_and_zero_padded():
    assert sectors.sic_to_sector("2834") == "Health Care"
    assert sectors.sic_to_sector("6021") == "Financials"


@pytest.mark.parametrize("bad", [None, "", "n/a", 0, -5, "abc"])
def test_sic_to_sector_unmappable_is_unknown(bad):
    assert sectors.sic_to_sector(bad) == "Unknown"


def test_every_bucket_is_a_declared_sector():
    # No range may map to a label outside the published SECTORS tuple.
    for _lo, _hi, sector in sectors._SIC_RANGES:
        assert sector in sectors.SECTORS


def test_build_sector_map_injected_fetch():
    # Fake SEC submissions keyed by the URL the code builds from the CIK.
    docs = {
        sectors.SUBMISSIONS_URL.format(cik="0000320193"):
            {"sic": "3571", "sicDescription": "Electronic Computers"},   # AAPL -> IT
        sectors.SUBMISSIONS_URL.format(cik="0000019617"):
            {"sic": "6021", "sicDescription": "National Commercial Banks"},  # JPM -> Financials
        sectors.SUBMISSIONS_URL.format(cik="0001000000"):
            {"sicDescription": "No SIC here"},                            # missing SIC -> Unknown
    }

    def fake_fetch(url):
        return docs[url]

    cikmap = {"AAPL": "0000320193", "JPM": "0000019617", "SHELL": "0001000000"}
    symbols = ["AAPL", "JPM", "SHELL", "SPY"]  # SPY absent from cikmap -> Unknown

    df = sectors.build_sector_map(symbols, cikmap, fetch=fake_fetch, sleep_s=0.0)

    assert list(df.index) == symbols
    assert df.loc["AAPL", "sector"] == "Information Technology"
    assert df.loc["AAPL", "sic"] == 3571
    assert df.loc["JPM", "sector"] == "Financials"
    assert df.loc["SHELL", "sector"] == "Unknown"   # has CIK but no SIC
    assert df.loc["SPY", "sector"] == "Unknown"      # no CIK at all
    assert pd.isna(df.loc["SPY", "cik"])             # no CIK recorded


def test_build_sector_map_fetch_error_is_graceful():
    def boom(url):
        raise RuntimeError("503 from SEC")

    df = sectors.build_sector_map(["AAPL"], {"AAPL": "0000320193"}, fetch=boom, sleep_s=0.0)
    assert df.loc["AAPL", "sector"] == "Unknown"     # error -> Unknown, no raise


def test_write_load_roundtrip(tmp_path):
    df = pd.DataFrame(
        {"cik": ["0000320193"], "sic": [3571],
         "sic_description": ["Electronic Computers"], "sector": ["Information Technology"]},
        index=pd.Index(["AAPL"], name="symbol"),
    )
    path = sectors.write_sector_map(df, tmp_path / "sectors.parquet")
    loaded = sectors.load_sector_map(path)
    pd.testing.assert_frame_equal(loaded, df)
