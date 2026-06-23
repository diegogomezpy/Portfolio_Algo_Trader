"""Unit tests for engine.risk — the pre-trade gate, pure and Alpaca-free.

The gate re-derives every constraint from the *proposed post-trade* book (defense in
depth — it does not trust the optimizer). Tests cover each check in isolation plus the
end-to-end `check_pretrade` aggregation, including the named Phase 3 gate criterion:
the gate must BLOCK a synthetic bad order batch.
"""

from __future__ import annotations

from datetime import date
from types import SimpleNamespace

import pandas as pd

from engine import risk


def _settings(max_name=0.05, max_sector=0.30):
    return SimpleNamespace(portfolio=SimpleNamespace(
        max_single_name_pct=max_name, max_sector_pct=max_sector))


_SECTORS = pd.Series({
    "AAPL": "Information Technology", "MSFT": "Information Technology",
    "XOM": "Energy", "JPM": "Financials", "PG": "Consumer Staples",
    "IAU": "Unknown",
})
_UNIVERSE = list(_SECTORS.index)


# --------------------------------------------------------------- approve ----
def test_clean_book_is_approved():
    w = pd.Series({"AAPL": 0.05, "XOM": 0.04, "JPM": 0.04, "PG": 0.03})
    res = risk.check_pretrade(w, settings=_settings(), sector_map=_SECTORS, universe=_UNIVERSE)
    assert res.approved is True
    assert res.failures == []
    assert res.reason == "ok"


# ---------------------------------------------------------- single checks ----
def test_long_only_blocks_negative_weight():
    bad = risk._check_long_only(pd.Series({"AAPL": 0.05, "XOM": -0.02}))
    assert len(bad) == 1 and "XOM" in bad[0]


def test_single_name_cap_blocks_oversized():
    bad = risk._check_single_name(pd.Series({"AAPL": 0.08, "XOM": 0.04}), cap=0.05)
    assert len(bad) == 1 and "AAPL" in bad[0]


def test_sector_cap_blocks_concentration():
    # AAPL+MSFT = 0.40 in Information Technology > 0.30 cap.
    w = pd.Series({"AAPL": 0.20, "MSFT": 0.20, "XOM": 0.04})
    bad = risk._check_sector(w, _SECTORS, cap=0.30)
    assert len(bad) == 1 and "Information Technology" in bad[0]


def test_unknown_sector_is_exempt_from_cap():
    # Two Unknown names summing to 0.40 must NOT trip the sector cap (matches optimizer).
    w = pd.Series({"IAU": 0.20, "SLV": 0.20})
    smap = pd.Series({"IAU": "Unknown", "SLV": "Unknown"})
    assert risk._check_sector(w, smap, cap=0.30) == []


def test_leverage_blocks_over_one_x_nav():
    w = pd.Series({f"S{i}": 0.10 for i in range(11)})    # 1.10 invested
    assert len(risk._check_leverage(w)) == 1
    assert risk._check_leverage(pd.Series({"A": 0.6, "B": 0.35})) == []   # 0.95 ok


def test_off_universe_symbol_blocks():
    w = pd.Series({"AAPL": 0.05, "WXYZ": 0.04})
    bad = risk._check_universe(w, _UNIVERSE)
    assert len(bad) == 1 and "WXYZ" in bad[0]


# ----------------------------------------------------- covered calls (P4) ----
def test_naked_call_blocks():
    # 1 contract × 100 shares needs 100 held; only 50 held ⇒ naked.
    orders = [{"underlying": "AAPL", "contracts": 1, "expiration": "2026-12-18"}]
    bad = risk._check_covered_calls(orders, {"AAPL": 50}, as_of=date(2026, 6, 23))
    assert len(bad) == 1 and "naked call" in bad[0]


def test_fully_covered_call_is_ok():
    orders = [{"underlying": "AAPL", "contracts": 1, "expiration": "2026-12-18"}]
    assert risk._check_covered_calls(orders, {"AAPL": 100}, as_of=date(2026, 6, 23)) == []


def test_expired_option_blocks():
    orders = [{"underlying": "AAPL", "contracts": 1, "expiration": "2026-01-01"}]
    bad = risk._check_covered_calls(orders, {"AAPL": 100}, as_of=date(2026, 6, 23))
    assert len(bad) == 1 and "expired" in bad[0]


def test_mini_contract_size_respected():
    # A 10-share mini against 10 held shares is covered.
    orders = [{"underlying": "AAPL", "contracts": 1, "contract_size": 10,
               "expiration": "2026-12-18"}]
    assert risk._check_covered_calls(orders, {"AAPL": 10}, as_of=date(2026, 6, 23)) == []


# ------------------------------------------------------- end-to-end gate ----
def test_check_pretrade_blocks_bad_batch_and_aggregates_reasons():
    # A batch that breaks several rules at once — the named Phase 3 gate criterion.
    w = pd.Series({"AAPL": 0.40, "MSFT": 0.40, "XOM": -0.01, "WXYZ": 0.05})
    res = risk.check_pretrade(w, settings=_settings(), sector_map=_SECTORS, universe=_UNIVERSE)
    assert res.approved is False
    joined = res.reason
    assert "single-name" in joined          # AAPL/MSFT > 0.05
    assert "negative weight" in joined       # XOM short
    assert "sector cap" in joined            # IT 0.80 > 0.30
    assert "off-universe" in joined          # WXYZ
    assert "; " in joined                    # multiple failures joined


def test_check_pretrade_approves_covered_equity_plus_calls():
    w = pd.Series({"AAPL": 0.05, "XOM": 0.04})
    orders = [{"underlying": "AAPL", "contracts": 1, "expiration": "2026-12-18"}]
    res = risk.check_pretrade(
        w, settings=_settings(), sector_map=_SECTORS, universe=_UNIVERSE,
        equity_positions={"AAPL": 100}, option_orders=orders, as_of=date(2026, 6, 23),
    )
    assert res.approved is True
