"""Unit tests for engine.edgar — synthetic XBRL company facts, no network.

The accounting that needs guarding: tag-fallback extraction, the trailing-twelve-month
sum with EDGAR's missing-Q4 reconstruction (Q4 = FY − Q1..Q3), nearest-prior balance
sheet levels, and end-to-end derivation of the four factor inputs with point-in-time
filing dates.
"""

from __future__ import annotations

import pandas as pd
import pytest

from engine import edgar


def _pt(start, end, val, fp, form, filed):
    return {"start": start, "end": end, "val": val, "fp": fp, "form": form, "filed": filed}

# Six reported quarters (NI=100, rev=1000, gp=400, eps=1.0 each) across FY2024-25,
# plus the FY2024 10-K — so 2024-Q4 must be RECONSTRUCTED (no standalone Q4 filing).
_QTRS = [
    ("2024-01-01", "2024-03-31", "Q1", "2024-04-30"),
    ("2024-04-01", "2024-06-30", "Q2", "2024-07-30"),
    ("2024-07-01", "2024-09-30", "Q3", "2024-10-30"),
    ("2025-01-01", "2025-03-31", "Q1", "2025-04-30"),
    ("2025-04-01", "2025-06-30", "Q2", "2025-07-30"),
    ("2025-07-01", "2025-09-30", "Q3", "2025-10-30"),
]
_ENDS = [("2024-03-31", "2024-04-30"), ("2024-06-30", "2024-07-30"),
         ("2024-09-30", "2024-10-30"), ("2024-12-31", "2025-02-15"),
         ("2025-03-31", "2025-04-30"), ("2025-06-30", "2025-07-30"),
         ("2025-09-30", "2025-10-30")]


def synthetic_facts():
    def flow(val, ann):
        pts = [_pt(s, e, val, fp, "10-Q", f) for s, e, fp, f in _QTRS]
        pts.append(_pt("2024-01-01", "2024-12-31", ann, "FY", "10-K", "2025-02-15"))
        return pts

    def stock(val):
        return [_pt(e, e, val, "", "10-Q", f) for e, f in _ENDS]

    return {"facts": {"us-gaap": {
        "NetIncomeLoss": {"units": {"USD": flow(100, 400)}},
        "Revenues": {"units": {"USD": flow(1000, 4000)}},
        "GrossProfit": {"units": {"USD": flow(400, 1600)}},
        "EarningsPerShareDiluted": {"units": {"USD/shares": flow(1.0, 4.0)}},
        "StockholdersEquity": {"units": {"USD": stock(2000)}},
        "CommonStockSharesOutstanding": {"units": {"shares": stock(1000)}},
    }}}


# ----------------------------------------------------------- extract_concept
def test_extract_concept_uses_first_present_tag():
    facts = synthetic_facts()
    df = edgar.extract_concept(facts, ["RevenueFromContractWithCustomerExcludingAssessedTax",
                                       "Revenues"])  # first absent, falls through to Revenues
    assert len(df) == 7
    assert str(df["end"].dtype).startswith("datetime")


def test_extract_concept_missing_returns_empty():
    assert edgar.extract_concept(synthetic_facts(), ["NotATag"]).empty


# ------------------------------------------------------ quarterly + Q4 rebuild
def test_quarterly_flow_reconstructs_q4_from_annual():
    qs = edgar.quarterly_flow_series(edgar.extract_concept(synthetic_facts(), ["NetIncomeLoss"]))
    q4 = qs[qs["end"] == pd.Timestamp("2024-12-31")]
    assert len(q4) == 1
    assert q4["val"].iloc[0] == 100            # 400 (FY) − 300 (Q1..Q3)
    assert q4["filed"].iloc[0] == pd.Timestamp("2025-02-15")  # known only at 10-K filing


# --------------------------------------------------------------------- TTM
def test_ttm_sums_trailing_four_quarters():
    qs = edgar.quarterly_flow_series(edgar.extract_concept(synthetic_facts(), ["NetIncomeLoss"]))
    assert edgar.ttm_at(qs, pd.Timestamp("2024-12-31")) == 400.0   # incl reconstructed Q4
    assert edgar.ttm_at(qs, pd.Timestamp("2025-09-30")) == 400.0   # rolls across the Q4


def test_ttm_requires_four_quarters():
    qs = edgar.quarterly_flow_series(edgar.extract_concept(synthetic_facts(), ["NetIncomeLoss"]))
    assert edgar.ttm_at(qs, pd.Timestamp("2024-03-31")) is None    # only one quarter available


# ------------------------------------------------------------ nearest-prior
def test_stock_asof_is_nearest_prior():
    pts = edgar.extract_concept(synthetic_facts(), ["StockholdersEquity"])
    # asof a date between two period ends → the earlier level, never the future one
    assert edgar._stock_asof(pts, pd.Timestamp("2024-08-15")) == 2000.0
    assert edgar._stock_asof(pts, pd.Timestamp("2023-01-01")) is None


# ----------------------------------------------------------- end-to-end
def test_companyfacts_to_fundamentals_values_and_pit():
    df = edgar.companyfacts_to_fundamentals(
        synthetic_facts(), price_lookup=lambda filed: 40.0
    ).set_index("period_end")
    row = df.loc[pd.Timestamp("2024-12-31").date()]
    assert row["roe"] == pytest.approx(0.20)            # NI_ttm 400 / equity 2000
    assert row["gross_margin"] == pytest.approx(0.40)   # GP_ttm 1600 / rev_ttm 4000
    assert row["pe_ratio"] == pytest.approx(10.0)       # price 40 / EPS_ttm 4
    assert row["pb_ratio"] == pytest.approx(20.0)       # price 40 / BVPS 2
    # point-in-time: the FY-end row is only public at the 10-K filing date
    assert row["report_date"] == pd.Timestamp("2025-02-15").date()
    assert row["source"] == "sec_edgar"


def test_pe_pb_nan_without_price():
    df = edgar.companyfacts_to_fundamentals(synthetic_facts(), price_lookup=None)
    assert df["pe_ratio"].isna().all() and df["pb_ratio"].isna().all()
    assert df["roe"].notna().any() and df["gross_margin"].notna().any()  # still derived
