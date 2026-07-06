"""SEC EDGAR fundamentals — deep, point-in-time financials from XBRL company facts.

Replaces yfinance as the primary fundamentals source (DECISIONS D22). For each US
filer we pull the ``companyfacts`` JSON once, then derive a quarterly history of the
four factor inputs — ROE, gross margin, P/E, P/B — plus a richer raw-tag superset
(net income, revenue, equity, shares, assets, debt, cash flow, dividends) for later
phases. Output matches ``data/raw/fundamentals/YYYY-QN.parquet`` so ``engine.factors``
is unchanged; extra columns ride along.

**Point-in-time, the right way.** Every XBRL datapoint carries the SEC ``filed`` date,
so ``report_date`` is the *actual filing date* and the factor lag is 0 — a quarter is
visible exactly when it was filed, and a later 10-K/A restatement only appears after
its own filing date (no restatement leakage).

**The two hard parts, both isolated in pure functions:**

* *Flows need TTM, and EDGAR has no standalone Q4* (Q4 lives only inside the annual
  10-K). :func:`quarterly_flow_series` rebuilds a clean 3-month series per concept,
  reconstructing ``Q4 = FY - (Q1+Q2+Q3)``; :func:`ttm_at` sums the trailing four.
* *Tags vary by filer and era* (e.g. post-ASC606 revenue). Each concept is a
  priority list of US-GAAP tags; the first present wins (:func:`extract_concept`).

Network access (:func:`fetch_json`, :func:`load_cik_map`, :func:`fetch_companyfacts`)
is thin and injectable; the derivation (:func:`companyfacts_to_fundamentals`) is pure
and unit-tested on synthetic facts. SEC fair-access: a descriptive User-Agent with a
contact is required, and callers must throttle to ≤10 req/s.
"""

from __future__ import annotations

import os
from datetime import date, datetime
from typing import Callable, Optional

import numpy as np
import pandas as pd

from engine.logger import get_logger

log = get_logger(__name__)

SEC_TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
COMPANYFACTS_URL = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json"
# SEC requires a descriptive UA with a contact; override via env for your own contact.
DEFAULT_USER_AGENT = os.environ.get(
    "SEC_USER_AGENT", "sharpe-engine/0.1 (diegogomezzx@gmail.com)"
)

# --- Concept → ordered US-GAAP tag fallbacks --------------------------------- #
# Flows (income/cash-flow statement) — summed over a period; need TTM.
FLOW_CONCEPTS = {
    "net_income": ["NetIncomeLoss", "ProfitLoss"],
    "revenue": [
        "RevenueFromContractWithCustomerExcludingAssessedTax",
        "Revenues",
        "SalesRevenueNet",
    ],
    "gross_profit": ["GrossProfit"],
    "cost_of_revenue": ["CostOfGoodsAndServicesSold", "CostOfRevenue", "CostOfGoodsSold"],
    # NOTE: cash-flow items are filed as cumulative YTD (6/9-month), not 3-month
    # periods, so the 3-month TTM logic leaves these sparse/NaN. They're rich-superset
    # extras (not factor inputs), so this is deferred until Phase 2 needs them — at
    # which point quarter-ize via YTD differencing (Q_n = YTD_n − YTD_{n-1}).
    "operating_cash_flow": ["NetCashProvidedByUsedInOperatingActivities"],
    "capex": ["PaymentsToAcquirePropertyPlantAndEquipment"],
    "dividends_paid": ["PaymentsOfDividendsCommonStock", "PaymentsOfDividends"],
    "eps_diluted": ["EarningsPerShareDiluted"],
}
# Stocks (balance-sheet) — a level at a point in time; never summed.
STOCK_CONCEPTS = {
    "stockholders_equity": [
        "StockholdersEquity",
        "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest",
    ],
    "total_assets": ["Assets"],
    "long_term_debt": ["LongTermDebtNoncurrent", "LongTermDebt"],
    "current_debt": ["DebtCurrent", "LongTermDebtCurrent", "ShortTermBorrowings"],
    "shares_outstanding": ["CommonStockSharesOutstanding"],
}

# Columns written to parquet: the four factor inputs first, then the raw superset.
FACTOR_FIELDS = ["pe_ratio", "pb_ratio", "roe", "gross_margin"]
RICH_FIELDS = [
    "net_income_ttm", "revenue_ttm", "gross_profit_ttm", "eps_diluted_ttm",
    "operating_cash_flow_ttm", "capex_ttm", "free_cash_flow_ttm", "dividends_ttm",
    "stockholders_equity", "total_assets", "total_debt", "shares_outstanding",
]
OUTPUT_COLUMNS = [*FACTOR_FIELDS, *RICH_FIELDS, "period_end", "report_date", "source"]


# ====================================================================== #
# Network (thin, injectable) — no derivation here
# ====================================================================== #
def fetch_json(url: str, *, user_agent: str = DEFAULT_USER_AGENT, timeout: int = 30) -> dict:
    """GET a JSON document from SEC with the required User-Agent header."""
    import json
    import urllib.request

    req = urllib.request.Request(url, headers={"User-Agent": user_agent,
                                               "Accept-Encoding": "gzip, deflate"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read()
        if resp.headers.get("Content-Encoding") == "gzip":
            import gzip
            raw = gzip.decompress(raw)
        return json.loads(raw)


def load_cik_map(*, fetch: Callable[[str], dict] = fetch_json) -> dict[str, str]:
    """Return ``{TICKER: zero-padded-10-digit CIK}`` from SEC's ticker directory."""
    data = fetch(SEC_TICKERS_URL)
    return {row["ticker"].upper(): str(row["cik_str"]).zfill(10) for row in data.values()}


def fetch_companyfacts(cik: str, *, fetch: Callable[[str], dict] = fetch_json) -> dict:
    """Fetch the full XBRL company-facts document for a zero-padded CIK."""
    return fetch(COMPANYFACTS_URL.format(cik=str(cik).zfill(10)))


# ====================================================================== #
# Pure derivation — unit-tested on synthetic facts
# ====================================================================== #
def extract_concept(facts: dict, tags: list[str]) -> pd.DataFrame:
    """Tidy a concept's datapoints from the first present tag in ``tags``.

    Returns columns ``start, end, val, fp, form, filed`` (dates parsed). USD and
    plain numeric units are accepted (covers $ amounts, share counts, per-share EPS).
    Empty frame if no tag is present.
    """
    gaap = facts.get("facts", {}).get("us-gaap", {})
    cols = ["start", "end", "val", "fp", "form", "filed"]
    for tag in tags:
        if tag not in gaap:
            continue
        units = gaap[tag]["units"]
        if not units:
            # Rare but real: a concept present with an EMPTY units dict. next(iter({})) raised
            # StopIteration here and killed the entire daily ingest — and with it the rebalance
            # that runs after it (2026-07-06). Treat like an absent tag: try the next one.
            continue
        unit_key = next((u for u in units if u in ("USD", "USD/shares", "shares")), None)
        if unit_key is None:
            unit_key = next(iter(units))
        rows = units[unit_key]
        df = pd.DataFrame(rows)
        if df.empty:
            continue
        df = df.reindex(columns=cols)
        df["start"] = pd.to_datetime(df["start"])
        df["end"] = pd.to_datetime(df["end"])
        df["filed"] = pd.to_datetime(df["filed"])
        return df.dropna(subset=["end", "val", "filed"])
    return pd.DataFrame(columns=cols)


def quarterly_flow_series(points: pd.DataFrame) -> pd.DataFrame:
    """Build a clean 3-month-period series for a flow concept, indexed by period end.

    Keeps genuine quarterly (~3-month) observations, deduped to the *earliest* filing
    per period (as-originally-reported, point-in-time). Reconstructs each fiscal Q4 as
    ``annual − (Q1+Q2+Q3)`` from the 10-K. Returns columns ``end, val, filed``.
    """
    if points.empty:
        return pd.DataFrame(columns=["end", "val", "filed"])
    df = points.copy()
    df["months"] = ((df["end"] - df["start"]).dt.days / 30.4).round()

    quarters = (df[df["months"].between(2, 4)]
                .sort_values("filed").drop_duplicates("end", keep="first"))
    annuals = (df[df["months"].between(11, 13)]
               .sort_values("filed").drop_duplicates("end", keep="first"))

    series = {row.end: (row.val, row.filed) for row in quarters.itertuples()}

    # Reconstruct Q4 from each annual: FY minus the three quarters inside that year.
    for a in annuals.itertuples():
        fy_end, fy_start = a.end, a.start
        in_year = quarters[(quarters["end"] > fy_start) & (quarters["end"] <= fy_end)]
        if len(in_year) == 3 and fy_end not in series:
            q4_val = a.val - in_year["val"].sum()
            series[fy_end] = (q4_val, a.filed)

    out = pd.DataFrame(
        [(end, v, f) for end, (v, f) in series.items()], columns=["end", "val", "filed"]
    ).sort_values("end").reset_index(drop=True)
    return out


def ttm_at(qseries: pd.DataFrame, ref_end: pd.Timestamp) -> Optional[float]:
    """Trailing-twelve-month sum ending at ``ref_end``: the 4 quarters up to it.

    Returns None unless exactly four consecutive quarters spanning ~1 year are
    present (so a gap yields a missing value rather than a silently short TTM).
    """
    upto = qseries[qseries["end"] <= ref_end].tail(4)
    if len(upto) < 4:
        return None
    span_days = (upto["end"].iloc[-1] - upto["end"].iloc[0]).days
    if not (250 <= span_days <= 320):  # ~3 quarter-gaps between 4 points
        return None
    return float(upto["val"].sum())


def _stock_asof(points: pd.DataFrame, ref_end: pd.Timestamp) -> Optional[float]:
    """Most recent balance-sheet level at or before ``ref_end``, as first filed.

    Nearest-prior (not exact-match) so an irregularly-tagged level — e.g. shares
    outstanding not stamped at every period end — falls back to the latest known
    figure rather than going missing. Still point-in-time: never uses a value dated
    after the period.
    """
    prior = points[points["end"] <= ref_end]
    if prior.empty:
        return None
    latest_end = prior["end"].max()
    match = prior[prior["end"] == latest_end].sort_values("filed")
    return float(match["val"].iloc[0])


def companyfacts_to_fundamentals(
    facts: dict,
    *,
    price_lookup: Optional[Callable[[pd.Timestamp], Optional[float]]] = None,
    source: str = "sec_edgar",
) -> pd.DataFrame:
    """Derive a point-in-time quarterly fundamentals history from company facts.

    One row per fiscal period end. ``report_date`` is the SEC filing date (lag 0).
    ``price_lookup(filed_date) -> price`` supplies the price for P/E and P/B; without
    it (or before the price-history floor) P/E and P/B are NaN but ROE / gross margin
    and the raw superset are still derived. Indexed by symbol-less period; the caller
    attaches the ticker.
    """
    flows = {name: quarterly_flow_series(extract_concept(facts, tags))
             for name, tags in FLOW_CONCEPTS.items()}
    stocks = {name: extract_concept(facts, tags)
              for name, tags in STOCK_CONCEPTS.items()}

    # Report events = every fiscal quarter end we have net income for (incl. Q4).
    ni = flows["net_income"]
    rows = []
    for evt in ni.itertuples():
        end, filed = evt.end, evt.filed
        ni_ttm = ttm_at(flows["net_income"], end)
        rev_ttm = ttm_at(flows["revenue"], end)
        gp_ttm = ttm_at(flows["gross_profit"], end)
        if gp_ttm is None and rev_ttm is not None:
            cor_ttm = ttm_at(flows["cost_of_revenue"], end)
            gp_ttm = (rev_ttm - cor_ttm) if cor_ttm is not None else None
        eps_ttm = ttm_at(flows["eps_diluted"], end)
        ocf_ttm = ttm_at(flows["operating_cash_flow"], end)
        capex_ttm = ttm_at(flows["capex"], end)
        div_ttm = ttm_at(flows["dividends_paid"], end)

        equity = _stock_asof(stocks["stockholders_equity"], end)
        assets = _stock_asof(stocks["total_assets"], end)
        ltd = _stock_asof(stocks["long_term_debt"], end)
        cd = _stock_asof(stocks["current_debt"], end)
        shares = _stock_asof(stocks["shares_outstanding"], end)

        price = price_lookup(filed) if price_lookup is not None else None

        roe = (ni_ttm / equity) if (ni_ttm is not None and equity) else np.nan
        gross_margin = (gp_ttm / rev_ttm) if (gp_ttm is not None and rev_ttm) else np.nan
        pe = (price / eps_ttm) if (price is not None and eps_ttm) else np.nan
        bvps = (equity / shares) if (equity is not None and shares) else None
        pb = (price / bvps) if (price is not None and bvps) else np.nan
        fcf = (ocf_ttm - capex_ttm) if (ocf_ttm is not None and capex_ttm is not None) else np.nan
        total_debt = sum(x for x in (ltd, cd) if x is not None) if (ltd or cd) else np.nan

        rows.append({
            "pe_ratio": pe, "pb_ratio": pb, "roe": roe, "gross_margin": gross_margin,
            "net_income_ttm": ni_ttm, "revenue_ttm": rev_ttm, "gross_profit_ttm": gp_ttm,
            "eps_diluted_ttm": eps_ttm, "operating_cash_flow_ttm": ocf_ttm,
            "capex_ttm": capex_ttm, "free_cash_flow_ttm": fcf, "dividends_ttm": div_ttm,
            "stockholders_equity": equity, "total_assets": assets,
            "total_debt": total_debt, "shares_outstanding": shares,
            "period_end": end.date(), "report_date": filed.date(), "source": source,
        })

    out = pd.DataFrame(rows, columns=OUTPUT_COLUMNS)
    # One row per period end: keep the earliest filing (as-originally-reported).
    return (out.sort_values("report_date")
            .drop_duplicates("period_end", keep="first")
            .reset_index(drop=True))
