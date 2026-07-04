"""Unit tests for engine.benchmarks — CBOE CSV parse, alignment, registry, cached fetch."""

from __future__ import annotations

from engine import benchmarks as B


def test_parse_cboe_csv():
    txt = "DATE,BXMD\n06/20/1986,100.000000\n06/23/1986,101.5\nbad,row\n,\n"
    assert B._parse_cboe_csv(txt) == {"1986-06-20": 100.0, "1986-06-23": 101.5}


def test_align_forward_fills():
    closes = {"2026-06-01": 10.0, "2026-06-03": 12.0}
    assert B.align(closes, ["2026-06-01", "2026-06-02", "2026-06-03", "2026-06-04"]) == [10.0, 10.0, 12.0, 12.0]
    assert B.align(closes, ["2026-05-30"]) == [10.0]      # before first key → first value
    assert B.align({}, ["x"]) == [None]


def test_describe_known_and_unknown():
    assert "BuyWrite" in B.describe("BXMD")["name"] and B.describe("BXMD")["desc"]
    assert "Russell" in B.describe("BXRD")["desc"]
    assert B.describe("ZZZ") == {"name": "ZZZ", "desc": ""}


def test_closes_caches_and_injects(tmp_path, monkeypatch):
    monkeypatch.setattr(B, "_CACHE", tmp_path)
    calls = {"n": 0}

    def fake_cboe(ticker):
        calls["n"] += 1
        return {"2001-01-31": 100.0, "2001-02-01": 99.0}

    out = B.closes("BXMD", "2001-01-01", fetch_cboe=fake_cboe)
    assert out["2001-01-31"] == 100.0 and calls["n"] == 1
    out2 = B.closes("BXMD", "2001-01-01", fetch_cboe=fake_cboe)
    assert out2 == out and calls["n"] == 1                 # second call served from cache, no refetch
    assert B.closes("UNKNOWN", "2001-01-01", fetch_cboe=fake_cboe) == {}   # not in registry


def test_fetch_closes_multi(tmp_path, monkeypatch):
    monkeypatch.setattr(B, "_CACHE", tmp_path)
    res = B.fetch_closes(["SPY", "BXMD"], "2021-01-01",
                         fetch_cboe=lambda t: {"2021-01-04": 100.0},
                         fetch_yf=lambda t, s: {"2021-01-04": 370.0})
    assert res["SPY"]["2021-01-04"] == 370.0 and res["BXMD"]["2021-01-04"] == 100.0


def test_new_peer_benchmarks_registered():
    # JEPI (the real-world peer fund) + USMV (the passive min-vol sleeve check), 2026-07-03.
    assert B.BENCHMARKS["JEPI"]["source"] == "yf"
    assert "Premium Income" in B.describe("JEPI")["name"] and B.describe("JEPI")["desc"]
    assert B.BENCHMARKS["USMV"]["source"] == "yf"
    assert "Min Vol" in B.describe("USMV")["name"]


def test_every_configured_live_benchmark_is_registered():
    # The YAML comment's rule, enforced: a symbol in dashboard.live_benchmarks that nobody
    # registered would render as a silent empty curve.
    import yaml
    from pathlib import Path
    cfg = yaml.safe_load((Path(__file__).resolve().parents[2] / "config" / "settings.yaml").read_text())
    for sym in cfg["dashboard"]["live_benchmarks"]:
        assert sym in B.BENCHMARKS, f"{sym} in live_benchmarks but not registered in engine/benchmarks.py"
