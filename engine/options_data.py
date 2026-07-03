"""Real historical option chains from the free DoltHub ``post-no-preference/options`` database.

Feeds the covered-call backtest **real** historical call premiums + implied vol, replacing the
Black-Scholes model where data exists (Phase 2b refinement, DECISIONS D33). Source: the public
DoltHub SQL API (no key) over the ``option_chain`` table — ``date, act_symbol, expiration,
strike, call_put, bid, ask, vol (= implied vol), delta, …``.

The dataset has whole-day gaps (the upstream scraper's downtime), so each rebalance date is
**resolved to the nearest available trading day within ±N days**; an adjacent day's chain is
~identical for premium estimation. Resolved rows are cached per requested date to
``data/ref/dolthub_options/`` (git-ignored, one-time pull) with a manifest of attempted symbols
so confirmed-misses are never re-queried.

Pure selection / variance-risk-premium math is separated from the network/cache I/O so it is
unit-tested without hitting the API.
"""

from __future__ import annotations

import json
import time
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Iterable, Mapping, Sequence

import numpy as np
import pandas as pd

from engine.logger import get_logger

log = get_logger(__name__)

_API = "https://www.dolthub.com/api/v1alpha1/post-no-preference/options/master"
_CACHE_DIR = Path("data/ref/dolthub_options")
_OPTIONABLE_CACHE = Path("data/ref/optionable_alpaca.json")
_NUMERIC = ("strike", "bid", "ask", "vol", "delta")


# ====================================================================== #
# Pure: chain selection + variance-risk-premium math (no I/O)
# ====================================================================== #
def _dte(exp: str | date, as_of: date) -> int:
    e = exp if isinstance(exp, date) else datetime.strptime(str(exp)[:10], "%Y-%m-%d").date()
    return (e - as_of).days


def _select(chain: Sequence[Mapping], as_of: date, target_delta: float,
            dte_min: int, dte_max: int, *, use_abs: bool) -> dict | None:
    """Pick the contract nearest ``target_delta`` whose expiration is in the DTE window.

    ``chain`` is a list of contract dicts (``expiration, strike, bid, ask, vol, delta``) for one
    underlying on one date. Prefers expirations in ``[dte_min, dte_max]``; if none, falls back to
    the expiration with DTE closest to the window. ``use_abs`` matches on |delta| (puts carry a
    negative delta). Returns the chosen contract enriched with ``mid`` and ``dte``, or ``None``.
    """
    rows = [c for c in chain if c.get("delta") is not None and c.get("bid") is not None
            and c.get("ask") is not None and float(c["ask"]) > 0]
    if not rows:
        return None
    in_window = [c for c in rows if dte_min <= _dte(c["expiration"], as_of) <= dte_max]
    if in_window:
        pool = in_window
    else:                                   # nearest expiration to the window's midpoint
        mid_dte = (dte_min + dte_max) / 2
        best_exp = min({str(c["expiration"])[:10] for c in rows},
                       key=lambda e: abs(_dte(e, as_of) - mid_dte))
        pool = [c for c in rows if str(c["expiration"])[:10] == best_exp]
    norm = (lambda d: abs(d)) if use_abs else (lambda d: d)
    chosen = min(pool, key=lambda c: abs(norm(float(c["delta"])) - target_delta))
    bid, ask = float(chosen["bid"]), float(chosen["ask"])
    return {**chosen, "mid": (bid + ask) / 2.0, "dte": _dte(chosen["expiration"], as_of)}


def select_call(chain: Sequence[Mapping], as_of: date, *, target_delta: float = 0.30,
                dte_min: int = 30, dte_max: int = 45) -> dict | None:
    """Pick the call nearest ``target_delta`` whose expiration is in the DTE window."""
    return _select(chain, as_of, target_delta, dte_min, dte_max, use_abs=False)


def select_put(chain: Sequence[Mapping], as_of: date, *, target_delta: float = 0.30,
               dte_min: int = 30, dte_max: int = 45) -> dict | None:
    """Pick the put nearest ``target_delta`` in |delta| within the DTE window (puts have δ<0)."""
    return _select(chain, as_of, target_delta, dte_min, dte_max, use_abs=True)


def premium_yield(contract: Mapping, spot: float, *, slippage: float = 0.0) -> float | None:
    """Real premium yield = call mid / spot (minus an optional execution haircut)."""
    if not spot or spot <= 0 or contract.get("mid") is None:
        return None
    return float(contract["mid"]) / spot * (1.0 - slippage)


def forward_realized_vol(closes: pd.Series, d0, d1) -> float | None:
    """Annualized realized vol of daily log returns over the holding period ``(d0, d1]``."""
    s = closes.loc[(closes.index > pd.Timestamp(d0)) & (closes.index <= pd.Timestamp(d1))].dropna()
    if len(s) < 3:
        return None
    rets = np.log(s / s.shift(1)).dropna()
    if len(rets) < 2:
        return None
    return float(rets.std(ddof=1) * np.sqrt(252.0))


def variance_risk_premium(implied_vol: float, realized_vol: float) -> dict:
    """VRP = how much the option's implied vol exceeded what actually realized.

    Returns both the vol-point gap (``vrp_vol`` = IV − RV) and the variance-term gap
    (``vrp_var`` = IV² − RV²), the latter being the economically correct measure of the premium
    earned for bearing variance risk. ``ratio`` = IV / RV (>1 means options were 'rich').
    """
    iv, rv = float(implied_vol), float(realized_vol)
    return {"implied_vol": iv, "realized_vol": rv, "vrp_vol": iv - rv,
            "vrp_var": iv * iv - rv * rv, "ratio": (iv / rv) if rv > 0 else float("nan")}


# ====================================================================== #
# I/O: DoltHub SQL API + per-date cache
# ====================================================================== #
def _query(sql: str, *, timeout: int = 60, retries: int = 2) -> list[dict]:
    """Run one SQL query against the DoltHub API; return rows (list of dicts). Raises on failure."""
    url = _API + "?" + urllib.parse.urlencode({"q": sql})
    last = ""
    for attempt in range(retries + 1):
        try:
            with urllib.request.urlopen(url, timeout=timeout) as resp:
                payload = json.loads(resp.read())
            if payload.get("query_execution_status") == "Success":
                return payload.get("rows", []) or []
            last = payload.get("query_execution_message", "unknown error")
        except Exception as exc:  # noqa: BLE001 — network/JSON error → retry then raise
            last = str(exc)
        time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(f"DoltHub query failed after {retries + 1} tries: {last}")


def _candidate_dates(target: date, max_offset: int) -> list[str]:
    """Weekday dates within ±max_offset of ``target``, ordered nearest-first (target itself first)."""
    out: list[str] = []
    for off in [0] + [o for k in range(1, max_offset + 1) for o in (-k, k)]:
        d = target + timedelta(days=off)
        if d.weekday() < 5:                 # skip weekends (never trading days)
            out.append(d.isoformat())
    return out


def _coerce(rows: list[dict]) -> pd.DataFrame:
    df = pd.DataFrame(rows)
    if df.empty:
        return pd.DataFrame(columns=["date", "act_symbol", "expiration", *_NUMERIC])
    for col in _NUMERIC:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    return df


def fetch_calls(symbols: Iterable[str], target: date, *, max_offset: int = 3,
                dte_min: int = 30, dte_max: int = 45, right: str = "Call",
                cache_dir: Path | None = None, query=_query) -> dict[str, list[dict]]:
    """Real option chains for ``symbols`` near ``target`` (nearest available trading day, ±max_offset).

    One API query per requested date covering all (candidate dates × missing symbols), narrowed
    server-side to a ``right`` (``Call``/``Put``) / expiration / delta band to stay under the API
    timeout. Each symbol resolves to the candidate date closest to ``target`` that actually has
    data. Cached per ``(target, right)`` (rows parquet + attempted-symbols manifest) so a re-run
    hits disk, not the network. Puts carry a negative delta, so the band flips sign accordingly.

    Returns ``{symbol: [contract dicts]}`` (only symbols with data; empty list omitted). ``query``
    is injectable for tests. (Name kept ``fetch_calls`` for back-compat; ``right`` selects puts.)
    """
    symbols = sorted({s.upper() for s in symbols})
    cdir = cache_dir or _CACHE_DIR
    cdir.mkdir(parents=True, exist_ok=True)
    tag = "" if right == "Call" else f".{right.lower()}"      # calls keep the original filename
    rows_path = cdir / f"{target.isoformat()}{tag}.parquet"
    manifest_path = cdir / f"{target.isoformat()}{tag}.attempted.json"

    cached = _coerce([]) if not rows_path.exists() else pd.read_parquet(rows_path)
    attempted = set(json.loads(manifest_path.read_text())) if manifest_path.exists() else set()
    missing = [s for s in symbols if s not in attempted]

    if missing:
        cands = _candidate_dates(target, max_offset)
        in_list = ",".join(f"'{c}'" for c in cands)
        sym_list = ",".join(f"'{s}'" for s in missing)
        exp_lo = (target + timedelta(days=dte_min - 7)).isoformat()
        exp_hi = (target + timedelta(days=dte_max + 7)).isoformat()
        delta_band = "delta BETWEEN -0.60 AND -0.05" if right == "Put" else "delta BETWEEN 0.05 AND 0.60"
        sql = (f"SELECT date, act_symbol, expiration, strike, bid, ask, vol, delta "
               f"FROM option_chain WHERE date IN ({in_list}) AND act_symbol IN ({sym_list}) "
               f"AND call_put='{right}' AND expiration BETWEEN '{exp_lo}' AND '{exp_hi}' "
               f"AND {delta_band}")
        fetched = _coerce(query(sql))
        # resolve each symbol to its nearest-to-target available date
        rank = {c: i for i, c in enumerate(cands)}             # 0 = target, then nearest
        keep = []
        for sym, g in fetched.groupby("act_symbol"):
            best = min(g["date"].astype(str).unique(), key=lambda d: rank.get(d, 99))
            keep.append(g[g["date"].astype(str) == best])
        resolved = pd.concat(keep, ignore_index=True) if keep else _coerce([])
        cached = pd.concat([cached, resolved], ignore_index=True).drop_duplicates(
            subset=["act_symbol", "expiration", "strike"])
        attempted |= set(missing)
        cached.to_parquet(rows_path, index=False)
        manifest_path.write_text(json.dumps(sorted(attempted)))
        log.info("dolthub fetch %s: %d/%d symbols had data", target, cached["act_symbol"].nunique(),
                 len(symbols))

    out: dict[str, list[dict]] = {}
    if not cached.empty:
        for sym in symbols:
            g = cached[cached["act_symbol"] == sym]
            if not g.empty:
                out[sym] = g.to_dict("records")
    return out


def fetch_puts(symbols: Iterable[str], target: date, **kw) -> dict[str, list[dict]]:
    """Real put chains near ``target`` (see :func:`fetch_calls`; sets ``right='Put'``)."""
    return fetch_calls(symbols, target, right="Put", **kw)


def optionable_underlyings(symbols: Iterable[str], *, probe=None,
                           cache_path: Path | None = None) -> set[str]:
    """Symbols with listed options **on Alpaca** (our broker) — cached per symbol in JSON.

    Used to split DoltHub-missing names: optionable-but-missing is a *data gap* (BS-impute it);
    NOT-optionable means we genuinely can't write a call live, so the backtest leaves it
    **unhedged**. ``probe(sym)->bool`` is injectable for tests; the default queries Alpaca's
    option-contracts endpoint (``expiration_date_gte=today`` — without it the endpoint can omit a
    name's nearest contracts and falsely read as non-optionable).
    """
    path = cache_path or _OPTIONABLE_CACHE
    syms = sorted({s.upper() for s in symbols})
    cache = json.loads(path.read_text()) if path.exists() else {}
    todo = [s for s in syms if s not in cache]
    if todo:
        probe = probe or _alpaca_optionable_probe()
        for s in todo:
            cache[s] = bool(probe(s))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(cache, sort_keys=True))
    return {s for s in syms if cache.get(s)}


def _alpaca_optionable_probe():
    """Default optionability probe: Alpaca option-contracts endpoint (≥1 active contract)."""
    import os
    key, sec = os.environ["ALPACA_API_KEY"], os.environ["ALPACA_SECRET_KEY"]
    base = os.environ.get("ALPACA_BASE_URL", "https://paper-api.alpaca.markets").rstrip("/")
    today = date.today().isoformat()

    def probe(sym: str) -> bool:
        url = base + "/v2/options/contracts?" + urllib.parse.urlencode(
            {"underlying_symbols": sym, "expiration_date_gte": today, "limit": 1})
        req = urllib.request.Request(url, headers={"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": sec})
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                return len(json.loads(resp.read()).get("option_contracts", [])) > 0
        except Exception:  # noqa: BLE001 — unknown → not-optionable (conservative: leave unhedged)
            return False
    return probe
