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


def _put_chain():
    return [
        {"expiration": "2022-09-30", "strike": 145.0, "bid": 1.60, "ask": 1.70, "vol": 0.31, "delta": -0.2300},
        {"expiration": "2022-09-30", "strike": 150.0, "bid": 2.95, "ask": 3.05, "vol": 0.33, "delta": -0.3400},
        {"expiration": "2022-09-30", "strike": 140.0, "bid": 0.90, "ask": 1.00, "vol": 0.30, "delta": -0.1500},
    ]


def test_select_put_nearest_abs_delta():
    p = od.select_put(_put_chain(), date(2022, 9, 1), target_delta=0.30, dte_min=25, dte_max=35)
    assert p is not None and abs(p["strike"] - 150.0) < 1e-9   # |−0.34| nearest 0.30
    assert p["delta"] < 0 and abs(p["mid"] - 3.0) < 1e-9


def test_fetch_puts_uses_put_right_and_negative_delta_band(tmp_path):
    seen = {}

    def fake_query(sql):
        seen["sql"] = sql
        return [{"date": "2022-09-01", "act_symbol": "AAPL", "expiration": "2022-09-30",
                 "strike": 150.0, "bid": 2.95, "ask": 3.05, "vol": 0.33, "delta": -0.34}]

    out = od.fetch_puts(["AAPL"], date(2022, 9, 1), cache_dir=tmp_path, query=fake_query)
    assert set(out) == {"AAPL"} and out["AAPL"][0]["delta"] == -0.34
    assert "call_put='Put'" in seen["sql"] and "delta BETWEEN -0.60 AND -0.05" in seen["sql"]
    assert (tmp_path / "2022-09-01.put.parquet").exists()        # separate cache file from calls


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
