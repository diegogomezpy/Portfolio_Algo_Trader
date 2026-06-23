"""FF5 factor-model covariance (DECISIONS D23a).

The optimizer needs a return covariance matrix Σ. We use the Fama-French
five-factor model rather than a raw sample covariance: each asset's excess
return is regressed on the five factors, and

    Σ = B · cov(F) · Bᵀ + diag(idiosyncratic variance)

where ``B`` is the (N×5) loading matrix and ``cov(F)`` the 5×5 factor
covariance over the estimation window. This is well-conditioned for any N
(it is a rank-5-plus-diagonal matrix, always positive-definite given positive
idiosyncratic variances) — unlike a 60-day sample covariance over dozens of
names, which is rank-deficient and unstable.

ARCHITECTURE specified sourcing the factors via ``pandas_datareader``, but that
library import-crashes under pandas 3.0. So :func:`fetch_ff5_daily` downloads
Ken French's daily 5-factor file directly from the Dartmouth data library
(stdlib ``urllib`` + ``zipfile``, cached to parquet); :func:`parse_ff5_csv` is a
pure parser unit-tested on a synthetic CSV, and :func:`factor_covariance` is the
pure estimator. Factor values are published in percent and converted to decimals.

Σ is annualized (×``periods_per_year``) so it shares units with the optimizer's
annualized-return μ proxy; λ and the μ scale are calibrated together in Phase 2
(DECISIONS D20), so the absolute scale is absorbed there either way.
"""

from __future__ import annotations

import io
import urllib.request
import zipfile
from pathlib import Path
from typing import Callable, Optional

import numpy as np
import pandas as pd

from engine import edgar
from engine.logger import get_logger

log = get_logger(__name__)

FF5_DAILY_URL = (
    "https://mba.tuck.dartmouth.edu/pages/faculty/ken.french/ftp/"
    "F-F_Research_Data_5_Factors_2x3_daily_CSV.zip"
)
DEFAULT_FF5_CACHE = Path("data/ref/ff5_daily.parquet")
FACTOR_COLUMNS = ["Mkt-RF", "SMB", "HML", "RMW", "CMA"]
TRADING_DAYS_PER_YEAR = 252


# ====================================================================== #
# Network + parsing (thin, injectable)
# ====================================================================== #
def _http_get_bytes(url: str, *, user_agent: str = edgar.DEFAULT_USER_AGENT, timeout: int = 30) -> bytes:
    """GET raw bytes (a zip) with a descriptive User-Agent."""
    req = urllib.request.Request(url, headers={"User-Agent": user_agent})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def parse_ff5_csv(text: str) -> pd.DataFrame:
    """Parse the Ken French daily 5-factor CSV text into decimal daily returns.

    The file has a description preamble, a header line ``,Mkt-RF,SMB,HML,RMW,CMA,RF``,
    then ``YYYYMMDD,<six percent values>`` rows, then a copyright trailer. Returns a
    frame indexed by date with columns ``FACTOR_COLUMNS + ['RF']`` in **decimals**
    (the source is in percent, so values are divided by 100).
    """
    lines = text.splitlines()
    start = next((i for i, ln in enumerate(lines) if "Mkt-RF" in ln), None)
    if start is None:
        raise ValueError("FF5 CSV: header row with 'Mkt-RF' not found")

    records = []
    for ln in lines[start + 1:]:
        tok = [t.strip() for t in ln.split(",")]
        if len(tok) < 7 or not (len(tok[0]) == 8 and tok[0].isdigit()):
            break  # reached the trailer (blank line / copyright)
        records.append((pd.Timestamp(tok[0]), *[float(v) for v in tok[1:7]]))

    if not records:
        raise ValueError("FF5 CSV: no data rows parsed")
    df = pd.DataFrame(records, columns=["date", *FACTOR_COLUMNS, "RF"]).set_index("date")
    return df / 100.0


def fetch_ff5_daily(
    *, url: str = FF5_DAILY_URL, fetch: Callable[[str], bytes] = _http_get_bytes
) -> pd.DataFrame:
    """Download and parse the daily FF5 file. ``fetch`` returns the raw zip bytes."""
    raw = fetch(url)
    with zipfile.ZipFile(io.BytesIO(raw)) as z:
        text = z.read(z.namelist()[0]).decode("latin-1")
    df = parse_ff5_csv(text)
    log.info("ff5 fetched", extra={"rows": len(df), "start": str(df.index.min().date()),
                                   "end": str(df.index.max().date())})
    return df


def _fetch_and_cache(cache_path: Path, url: str, fetch: Callable[[str], bytes]) -> pd.DataFrame:
    df = fetch_ff5_daily(url=url, fetch=fetch)
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    df.to_parquet(cache_path)
    return df


def load_ff5_daily(
    *,
    cache_path: Path | str = DEFAULT_FF5_CACHE,
    url: str = FF5_DAILY_URL,
    fetch: Callable[[str], bytes] = _http_get_bytes,
    force: bool = False,
    max_stale_days: int | None = None,
) -> pd.DataFrame:
    """Return the daily FF5 frame, from the parquet cache or a fresh download.

    History is immutable so a cache hit is normally used unconditionally. But Ken
    French publishes the daily file with a multi-week lag, so a *live* daily engine
    drifts behind the price data — and a covariance window dated near "today" can then
    overlap too few factor days to estimate Σ (it raises). ``max_stale_days`` (used by
    the live pipeline, not the in-sample backtest) triggers a best-effort refresh when
    the cached data ends more than that many days before today; if the refresh fails
    (offline / SEC hiccup) the stale cache is logged and returned rather than crashing.
    Default ``None`` keeps the cache-unconditional behavior (backtest / tests unchanged).
    """
    cache_path = Path(cache_path)
    if force or not cache_path.exists():
        return _fetch_and_cache(cache_path, url, fetch)
    cached = pd.read_parquet(cache_path)
    if max_stale_days is not None and not cached.empty:
        age = (pd.Timestamp.today().normalize() - cached.index.max()).days
        if age > max_stale_days:
            try:
                return _fetch_and_cache(cache_path, url, fetch)
            except Exception as exc:  # noqa: BLE001 — degrade to the stale cache, never crash
                log.warning("ff5 refresh failed; using stale cache",
                            extra={"age_days": int(age), "error": str(exc)})
    return cached


# ====================================================================== #
# Pure estimator — unit-tested
# ====================================================================== #
def factor_covariance(
    asset_returns: pd.DataFrame,
    ff5: pd.DataFrame,
    *,
    min_obs: int = 40,
    periods_per_year: int = TRADING_DAYS_PER_YEAR,
    resid_var_floor: float = 1e-8,
) -> pd.DataFrame:
    """Estimate an annualized FF5 factor-model covariance over the given window.

    Args:
        asset_returns: (date × symbol) **simple daily returns**, already sliced to
            the estimation window by the caller.
        ff5: daily factor frame from :func:`load_ff5_daily` (decimals; the 5
            factors + ``RF``).
        min_obs: a symbol needs at least this many aligned, non-NaN days or it is
            dropped from Σ (its history is too thin to load reliably).
        periods_per_year: annualization factor applied to the daily covariance.
        resid_var_floor: lower bound on idiosyncratic variance, keeping Σ strictly
            positive-definite for the QP solver.

    Returns:
        Annualized Σ as a (k × k) DataFrame over the retained symbols
        (k ≤ #symbols). Σ = B·cov(F)·Bᵀ + diag(resid var), always PSD.
    """
    common = asset_returns.index.intersection(ff5.index)
    if len(common) < min_obs:
        raise ValueError(f"only {len(common)} overlapping days with FF5 (need ≥ {min_obs})")

    F = ff5.loc[common, FACTOR_COLUMNS]
    rf = ff5.loc[common, "RF"]
    f_cov = np.cov(F.to_numpy(), rowvar=False, ddof=1)  # 5×5

    kept, betas, resid_vars = [], [], []
    for sym in asset_returns.columns:
        excess = (asset_returns[sym].reindex(common) - rf).dropna()
        if len(excess) < min_obs:
            continue
        X = F.loc[excess.index].to_numpy()
        design = np.column_stack([np.ones(len(excess)), X])  # intercept + 5 factors
        coef, *_ = np.linalg.lstsq(design, excess.to_numpy(), rcond=None)
        resid = excess.to_numpy() - design @ coef
        dof = max(len(excess) - design.shape[1], 1)
        kept.append(sym)
        betas.append(coef[1:])  # drop intercept
        resid_vars.append(max(float(resid @ resid) / dof, resid_var_floor))

    if not kept:
        raise ValueError("no symbols had enough history to estimate covariance")

    B = np.array(betas)                       # k×5
    sigma = B @ f_cov @ B.T + np.diag(resid_vars)
    sigma *= periods_per_year
    dropped = len(asset_returns.columns) - len(kept)
    if dropped:
        log.info("covariance: dropped thin-history names", extra={"dropped": dropped, "kept": len(kept)})
    return pd.DataFrame(sigma, index=kept, columns=kept)


def covariance_from_prices(
    price_panel: pd.DataFrame,
    ff5: pd.DataFrame,
    *,
    as_of,
    window: int,
    min_obs: int = 40,
    periods_per_year: int = TRADING_DAYS_PER_YEAR,
) -> pd.DataFrame:
    """Convenience: slice a (date × symbol) close panel to the trailing ``window``
    ending at ``as_of``, convert to simple returns, and estimate Σ.

    Point-in-time: only rows up to and including ``as_of`` are used.
    """
    panel = price_panel.loc[: pd.Timestamp(as_of)].iloc[-(window + 1):]
    returns = panel.pct_change().dropna(how="all")
    return factor_covariance(returns, ff5, min_obs=min_obs, periods_per_year=periods_per_year)
