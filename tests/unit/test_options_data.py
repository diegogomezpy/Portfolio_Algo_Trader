"""Unit tests for engine.options_data — pure chain-selection / premium / VRP math, and the
DoltHub fetcher with a mocked query (no network)."""

from __future__ import annotations

from datetime import date

import numpy as np
import pandas as pd

from engine import options_data as od


def _chain():
    return [
        {"expiration": "2022-09-30", "strike": 167.5, "bid": 1.65, "ask": 1.71, "vol": 0.2811, "delta": 0.2355},
        {"expiration": "2022-09-30", "strike": 162.5, "bid": 3.20, "ask": 3.30, "vol": 0.2940, "delta": 0.3722},
        {"expiration": "2022-10-21", "strike": 170.0, "bid": 2.50, "ask": 2.60, "vol": 0.30, "delta": 0.30},
    ]


def test_select_call_nearest_delta_in_window():
    c = od.select_call(_chain(), date(2022, 9, 1), target_delta=0.30, dte_min=25, dte_max=35)
    assert c is not None and abs(c["strike"] - 167.5) < 1e-9   # 0.2355 is nearest 0.30
    assert abs(c["mid"] - 1.68) < 1e-9


def test_select_call_falls_back_outside_window():
    c = od.select_call(_chain(), date(2022, 9, 1), target_delta=0.30, dte_min=45, dte_max=60)
    assert c is not None and str(c["expiration"]).startswith("2022-10-21")   # only ~50-DTE expiry


def test_select_call_empty_or_no_quote():
    assert od.select_call([], date(2022, 9, 1)) is None
    assert od.select_call(
        [{"delta": 0.3, "bid": None, "ask": None, "expiration": "2022-09-30", "strike": 1}],
        date(2022, 9, 1)) is None


def test_premium_yield():
    assert abs(od.premium_yield({"mid": 2.0}, 100.0) - 0.02) < 1e-12
    assert abs(od.premium_yield({"mid": 2.0}, 100.0, slippage=0.05) - 0.019) < 1e-12
    assert od.premium_yield({"mid": None}, 100.0) is None
    assert od.premium_yield({"mid": 2.0}, 0.0) is None


def test_forward_realized_vol():
    idx = pd.bdate_range("2022-09-01", "2022-09-30")
    s = pd.Series(np.linspace(100, 110, len(idx)), index=idx)
    rv = od.forward_realized_vol(s, date(2022, 9, 1), date(2022, 9, 30))
    assert rv is not None and rv >= 0
    assert od.forward_realized_vol(s.iloc[:1], date(2022, 9, 1), date(2022, 9, 30)) is None


def test_variance_risk_premium():
    v = od.variance_risk_premium(0.28, 0.22)
    assert abs(v["vrp_vol"] - 0.06) < 1e-9
    assert abs(v["vrp_var"] - (0.28 ** 2 - 0.22 ** 2)) < 1e-9
    assert v["ratio"] > 1


def test_fetch_calls_resolves_gap_and_caches(tmp_path):
    calls = {"n": 0}

    def fake_query(sql):                       # target 2022-09-01 is a gap → data on 08-31
        calls["n"] += 1
        return [
            {"date": "2022-08-31", "act_symbol": "AAPL", "expiration": "2022-09-30",
             "strike": 167.5, "bid": 1.65, "ask": 1.71, "vol": 0.28, "delta": 0.2355},
            {"date": "2022-08-31", "act_symbol": "KO", "expiration": "2022-09-30",
             "strike": 63.0, "bid": 0.68, "ask": 0.74, "vol": 0.19, "delta": 0.3376},
        ]

    out = od.fetch_calls(["AAPL", "KO", "ZZZ"], date(2022, 9, 1), cache_dir=tmp_path, query=fake_query)
    assert set(out) == {"AAPL", "KO"}                       # ZZZ had no data
    assert out["AAPL"][0]["date"] == "2022-08-31"           # resolved from the missing 09-01

    out2 = od.fetch_calls(["AAPL", "KO", "ZZZ"], date(2022, 9, 1), cache_dir=tmp_path, query=fake_query)
    assert calls["n"] == 1 and set(out2) == {"AAPL", "KO"}  # cache hit; ZZZ not re-queried (manifest)


def test_optionable_underlyings_classifies_and_caches(tmp_path):
    probed = []

    def probe(sym):                            # AAPL/ALLY optionable; WTM not
        probed.append(sym)
        return sym in {"AAPL", "ALLY"}

    cache = tmp_path / "opt.json"
    out = od.optionable_underlyings(["AAPL", "ALLY", "WTM"], probe=probe, cache_path=cache)
    assert out == {"AAPL", "ALLY"}
    probed.clear()
    out2 = od.optionable_underlyings(["AAPL", "ALLY", "WTM"], probe=probe, cache_path=cache)
    assert out2 == {"AAPL", "ALLY"} and probed == []   # second call served from JSON cache
