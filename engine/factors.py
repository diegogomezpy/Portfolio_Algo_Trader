"""Factor scoring — composite Quality / Value / Low-Beta / Low-vol per stock.

Every stock that survives the liquidity filter gets a composite factor score on a
given date (SPEC "Factor scoring"). The composite is an equal-weighted average of
four cross-sectionally z-scored sub-scores:

* **Quality**  — double-z of ``z(ROE) + z(gross_margin)``
* **Value**    — double-z of ``z(E/P) + z(B/P)`` where E/P = 1/PE, B/P = 1/PB
* **Low-Beta** — z of ``-β`` where β is the OLS beta of daily returns vs the market
  (``settings.factors.beta_market``, SPY) over ``beta_window`` days — low beta ranks high
* **Low-vol**  — z of ``-realized_vol`` over the trailing 252 trading days

*Amendment (2026-07-02, Diego):* Momentum (12-1) was replaced by a **Low-Beta** screen —
the book targets stable, low-market-sensitivity names to pair with the covered-call income
overlay. β is measured vs SPY (already present in the price panel) over 252 trading days.

**Methodology decisions (2026-06-18, confirmed with Diego):**

1. *Value uses earnings/book yield, not ratio inversion.* ~24% of the universe has
   negative P/E; inverting (``-P/E``) would score loss-makers as "cheap". E/P and
   B/P put negatives at the bottom of the cross-section where they belong, and keep
   the name scored rather than dropped.
2. *Double-z + percentile winsorization.* Each raw metric is winsorized to its
   ``[winsor_pct, 1-winsor_pct]`` quantiles, then z-scored on the winsorized data, so
   fat tails (a stock up 5000%, a tiny-denominator E/P) can't inflate σ and crush the
   rest toward zero — every sub-score lands at ~unit variance. Quality and Value
   re-standardize the sum of their two metric z-scores so all four sub-scores share
   that scale before the equal-weight average (true equal weight). The winsor pct and
   the beta/vol windows live in ``settings.factors`` — nothing here is hardcoded
   that belongs in the YAML.
3. *Point-in-time fundamentals with a reporting lag.* ``report_date`` in the
   fundamentals store is the quarter-*end* date, not the filing date, so a quarter
   is only usable once ``report_date + report_lag_days <= as_of`` (default 45d).
   This keeps the backtest honest — no peeking at earnings before they were public.

Missing inputs are neutral-filled (z = 0) for the composite rather than dropping the
name (``settings.factors.exclude_missing_fundamentals`` is False), and the stored
sub-score column keeps NaN so callers can see what was imputed. Any name missing a
fundamental field is flagged ``stale``.

Pure compute (:func:`compute_factor_scores`) is separated from I/O (:func:`score_date`,
:func:`load_close_panel`, :func:`load_all_fundamentals`) and the loaders are injectable,
so scoring is unit-testable on synthetic frames without touching disk or Postgres.
"""

from __future__ import annotations

from datetime import date as _date
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd

from engine.logger import get_logger

log = get_logger(__name__)

DEFAULT_PRICES_DIR = Path("data/raw/equities")
DEFAULT_FUNDAMENTALS_DIR = Path("data/raw/fundamentals")

# Columns written to the factor_scores table (matches engine.db.factor_scores).
SUBSCORE_COLUMNS = ["quality_score", "value_score", "beta_score", "lowvol_score"]
FUNDAMENTAL_FIELDS = ["pe_ratio", "pb_ratio", "roe", "gross_margin"]


# ====================================================================== #
# Pure compute (no I/O, no network) — unit-testable on synthetic frames
# ====================================================================== #
def zscore(s: pd.Series, winsor_pct: float) -> pd.Series:
    """Cross-sectional z-score with percentile winsorization.

    The raw metric is first clipped to its ``[winsor_pct, 1 - winsor_pct]``
    quantiles, then standardized on the *winsorized* data. Winsorizing before
    standardizing (rather than σ-clipping after) is what makes the result robust
    to fat tails: a handful of extreme raw values can't inflate σ and crush the
    rest toward zero, so each sub-score comes out at ~unit variance — the property
    that keeps the four factors genuinely equal-weighted in the composite.

    NaN inputs stay NaN (so callers can distinguish "missing" from "average").
    A degenerate cross-section (zero or non-finite spread) maps every present
    value to 0 (neutral) rather than dividing by zero.
    """
    valid = s.dropna()
    if len(valid) < 2:
        return pd.Series(np.nan, index=s.index, dtype=float)
    lo, hi = valid.quantile(winsor_pct), valid.quantile(1.0 - winsor_pct)
    w = s.clip(lo, hi)
    wv = w.dropna()
    sd = float(wv.std(ddof=0))
    if sd == 0.0 or not np.isfinite(sd):
        return pd.Series(0.0, index=s.index).where(s.notna())
    return (w - float(wv.mean())) / sd


def _double_z(z_a: pd.Series, z_b: pd.Series, winsor_pct: float) -> pd.Series:
    """Combine two metric z-scores into a re-standardized sub-score.

    Each component is neutral-filled (missing → 0) before summing, then the sum is
    z-scored again so the sub-score is unit-variance like low-beta and low-vol.
    """
    combined = z_a.fillna(0.0) + z_b.fillna(0.0)
    return zscore(combined, winsor_pct)


def market_beta(price_panel: pd.DataFrame, as_of: _date, window: int, market: str = "SPY") -> pd.Series:
    """OLS market beta per symbol: ``β = cov(rᵢ, r_mkt) / var(r_mkt)`` over ``window`` days.

    ``price_panel`` is a (date × symbol) close matrix that must include the ``market``
    column (SPY is in the equity price store). Uses pairwise-complete daily returns over
    the trailing ``window`` days ending at ``as_of``; a symbol with < 80% of the window's
    observations — or no market data / degenerate market variance — comes back NaN (the
    composite neutral-fills it). Lower β is better, so the sub-score is ``z(-β)``.
    """
    panel = price_panel.loc[: pd.Timestamp(as_of)].iloc[-(window + 1):]
    if market not in panel.columns or len(panel) < 2:
        return pd.Series(np.nan, index=price_panel.columns, dtype=float)
    rets = panel.pct_change().iloc[1:]                       # up to ``window`` rows of daily returns
    m = rets[market]
    # Pairwise-complete: for each column keep rows where both it and the market have a return.
    present = rets.notna() & m.notna().to_numpy()[:, None]
    n = present.sum()
    r = rets.where(present)
    mm = pd.DataFrame(np.where(present, m.to_numpy()[:, None], np.nan),
                      index=rets.index, columns=rets.columns)
    r_mean, m_mean = r.sum() / n, mm.sum() / n
    cov = (r * mm).sum() / n - r_mean * m_mean
    var = (mm * mm).sum() / n - m_mean * m_mean
    beta = cov / var.where(var > 0)
    return beta.where(n >= int(window * 0.8))


def realized_vol(price_panel: pd.DataFrame, as_of: _date, window: int) -> pd.Series:
    """Trailing realized volatility (σ of daily returns) per symbol.

    Requires at least 80% of ``window`` daily observations present, else NaN.
    Not annualized — the cross-sectional z-score is scale-invariant.
    """
    panel = price_panel.loc[: pd.Timestamp(as_of)].iloc[-(window + 1):]
    rets = panel.pct_change()
    vol = rets.std(ddof=0)
    return vol.where(rets.count() >= int(window * 0.8))


def compute_factor_scores(
    as_of: _date,
    *,
    settings,
    price_panel: pd.DataFrame,
    fundamentals_pit: pd.DataFrame,
    universe_snapshot: pd.DataFrame,
    eligible_symbols: set | None = None,
) -> pd.DataFrame:
    """Compute composite factor scores for the filtered universe on ``as_of``.

    Args:
        settings: namespace from ``load_settings()`` (uses ``.universe`` + ``.factors``).
        price_panel: (date × symbol) close matrix, last row == ``as_of``, with at
            least ``beta_window + 1`` rows of history and the ``beta_market`` column present.
        fundamentals_pit: point-in-time fundamentals indexed by symbol, columns
            ``pe_ratio, pb_ratio, roe, gross_margin`` (see :func:`point_in_time_fundamentals`).
        universe_snapshot: ``as_of`` equity snapshot indexed by symbol with
            ``close`` and ``adv_20d`` columns (for the liquidity filter).

    Returns:
        One row per surviving symbol with the four sub-scores, composite, and
        ``stale`` flag. Sub-scores keep NaN where inputs were missing; the
        composite neutral-fills missing sub-scores to 0.
    """
    fac = settings.factors
    wp = fac.winsor_pct

    # --- Universe filter (cross-section is defined *after* this) ---------------
    snap = universe_snapshot
    keep = snap[(snap["close"] > settings.universe.min_price)
                & (snap["adv_20d"] > settings.universe.min_adv_usd)]
    if eligible_symbols is not None:  # e.g. only US-GAAP filers with reliable data (D28)
        keep = keep[keep.index.isin(eligible_symbols)]
    symbols = keep.index
    if len(symbols) == 0:
        return _empty_scores()

    f = fundamentals_pit.reindex(symbols)

    # --- Value: earnings yield (E/P) and book yield (B/P) ----------------------
    ep = 1.0 / f["pe_ratio"].where(f["pe_ratio"] != 0)
    bp = 1.0 / f["pb_ratio"].where(f["pb_ratio"] != 0)
    value = _double_z(zscore(ep, wp), zscore(bp, wp), wp)

    # --- Quality: ROE and gross margin -----------------------------------------
    quality = _double_z(zscore(f["roe"], wp), zscore(f["gross_margin"], wp), wp)

    # --- Low-Beta: -β vs the market (SPY) -------------------------------------
    beta = market_beta(price_panel, as_of, fac.beta_window, getattr(fac, "beta_market", "SPY"))
    lowbeta = zscore(-beta.reindex(symbols), wp)

    # --- Low volatility: -realized vol ----------------------------------------
    vol = realized_vol(price_panel, as_of, fac.vol_window)
    lowvol = zscore(-vol.reindex(symbols), wp)

    # --- Composite: equal-weight average, missing sub-scores neutral-filled ----
    w = fac.weights
    composite = (w.quality * quality.fillna(0.0)
                 + w.value * value.fillna(0.0)
                 + w.low_beta * lowbeta.fillna(0.0)
                 + w.low_vol * lowvol.fillna(0.0))

    stale = f[FUNDAMENTAL_FIELDS].isna().any(axis=1)

    out = pd.DataFrame({
        "date": pd.Timestamp(as_of).date(),
        "symbol": symbols,
        "quality_score": quality.to_numpy(),
        "value_score": value.to_numpy(),
        "beta_score": lowbeta.to_numpy(),
        "lowvol_score": lowvol.to_numpy(),
        "composite_score": composite.to_numpy(),
        "stale": stale.to_numpy(),
    })
    return out.reset_index(drop=True)


def _empty_scores() -> pd.DataFrame:
    cols = ["date", "symbol", *SUBSCORE_COLUMNS, "composite_score", "stale"]
    return pd.DataFrame(columns=cols)


# ====================================================================== #
# I/O — Parquet loaders + Postgres writer (injectable into the above)
# ====================================================================== #
def _date_from_name(p: Path) -> _date | None:
    """Parse a ``YYYY-MM-DD`` filename stem to a date, or ``None`` when it isn't a dated
    snapshot — e.g. a macOS ``._`` AppleDouble shadow, a ``.DS_Store``, or any stray file in
    the data dir. Returning ``None`` (rather than raising) means one junk file copied in
    alongside the real Parquet can't crash the whole rebalance."""
    try:
        return _date.fromisoformat(p.stem)
    except ValueError:
        return None


def load_close_panel(prices_dir: Path | str, end: _date, lookback: int) -> pd.DataFrame:
    """Build a (date × symbol) close matrix from per-date equity Parquet files.

    Loads the ``lookback`` most recent trading-day files with date ``<= end``. Files whose name
    isn't a ``YYYY-MM-DD`` date (dotfiles / stray copies) are skipped, not parsed.
    """
    files = sorted(Path(prices_dir).glob("*.parquet"))
    dated = [(f, d) for f in files if (d := _date_from_name(f)) is not None]
    dated = [(f, d) for f, d in dated if d <= end][-lookback:]
    closes = {pd.Timestamp(d): pd.read_parquet(f, columns=["close"])["close"]
              for f, d in dated}
    if not closes:
        return pd.DataFrame()
    return pd.DataFrame(closes).T.sort_index()


def load_all_fundamentals(fundamentals_dir: Path | str, *, source: str | None = None) -> pd.DataFrame:
    """Load every quarterly fundamentals file into one long frame.

    Returns columns ``symbol, pe_ratio, pb_ratio, roe, gross_margin, report_date``
    with ``report_date`` parsed to datetime. Cheap enough to load once and reuse
    across an entire backtest.

    ``source`` filters to one data source (e.g. ``"sec_edgar"`` to trade only US-GAAP
    filers and drop the unreliable yfinance ADR fallback — DECISIONS D28).
    """
    frames = []
    for p in sorted(Path(fundamentals_dir).glob("*.parquet")):
        if p.name.startswith("."):             # skip macOS '._' shadows / .DS_Store / stray dotfiles
            continue
        df = pd.read_parquet(p).reset_index()  # symbol is the index on disk
        frames.append(df)
    if not frames:
        return pd.DataFrame(columns=["symbol", *FUNDAMENTAL_FIELDS, "report_date"])
    allf = pd.concat(frames, ignore_index=True)
    if source is not None and "source" in allf.columns:
        allf = allf[allf["source"] == source].reset_index(drop=True)
    allf["report_date"] = pd.to_datetime(allf["report_date"])
    # Per-quarter files can disagree on dtype (a stray None makes a column object);
    # concat then poisons it. Coerce the numeric fields so downstream math is float.
    for col in FUNDAMENTAL_FIELDS:
        allf[col] = pd.to_numeric(allf[col], errors="coerce")
    return allf


def load_scored_fundamentals(fundamentals_dir: Path | str, settings):
    """Load fundamentals and the eligible-symbol set per the universe policy (D28).

    When ``settings.universe.require_edgar_fundamentals`` is set, only US-GAAP
    (``sec_edgar``) fundamentals are loaded and the tradable universe is restricted
    to those names — excluding the foreign/ADR names whose only data is the
    unreliable yfinance fallback. Returns ``(all_fundamentals, eligible_symbols)``;
    ``eligible_symbols`` is ``None`` when the policy is off (no restriction).
    """
    if getattr(settings.universe, "require_edgar_fundamentals", False):
        allf = load_all_fundamentals(fundamentals_dir, source="sec_edgar")
        return allf, set(allf["symbol"])
    return load_all_fundamentals(fundamentals_dir), None


def point_in_time_fundamentals(all_fundamentals: pd.DataFrame, as_of: _date, lag_days: int) -> pd.DataFrame:
    """Latest fundamentals per symbol that were public on ``as_of``.

    A quarter is eligible only once ``report_date + lag_days <= as_of`` — the
    reporting-lag rule that prevents look-ahead (report_date is quarter-end).
    """
    if all_fundamentals.empty:
        return pd.DataFrame(columns=FUNDAMENTAL_FIELDS).rename_axis("symbol")
    cutoff = pd.Timestamp(as_of)
    avail = all_fundamentals[
        all_fundamentals["report_date"] + pd.Timedelta(days=lag_days) <= cutoff
    ]
    if avail.empty:
        return pd.DataFrame(columns=FUNDAMENTAL_FIELDS).rename_axis("symbol")
    # Latest available quarter per symbol, picked by filing (report) date. Edge case:
    # a late-filed restatement (e.g. a 10-K/A for an older year filed after a newer
    # quarter) would have the latest report_date and could win over that newer quarter.
    # Rare, and the EDGAR loader already dedups to one row per period end (earliest
    # filing), so in practice report_date order == period_end order. Left as-is.
    latest = avail.sort_values("report_date").groupby("symbol").tail(1).set_index("symbol")
    return latest[[*FUNDAMENTAL_FIELDS, "report_date"]]


def score_date(
    as_of: _date,
    *,
    settings=None,
    prices_dir: Path | str = DEFAULT_PRICES_DIR,
    fundamentals_dir: Path | str = DEFAULT_FUNDAMENTALS_DIR,
    price_panel: Optional[pd.DataFrame] = None,
    all_fundamentals: Optional[pd.DataFrame] = None,
    eligible_symbols: set | None = None,
) -> pd.DataFrame:
    """Load inputs for ``as_of`` from disk and compute factor scores.

    ``price_panel`` and ``all_fundamentals`` may be passed pre-loaded (the backtest
    loads them once and reuses across every month); otherwise they are read here.
    ``eligible_symbols`` further restricts the tradable universe (e.g. US-GAAP filers).
    """
    if settings is None:
        from engine.config import load_settings
        settings = load_settings()

    snap_path = Path(prices_dir) / f"{as_of.isoformat()}.parquet"
    if not snap_path.exists():
        raise FileNotFoundError(f"no equity snapshot for {as_of} at {snap_path}")
    universe_snapshot = pd.read_parquet(snap_path, columns=["close", "adv_20d"])

    if price_panel is None:
        fac = settings.factors
        lookback = max(fac.beta_window, fac.vol_window) + 5
        price_panel = load_close_panel(prices_dir, end=as_of, lookback=lookback)
    else:
        price_panel = price_panel.loc[: pd.Timestamp(as_of)]

    if all_fundamentals is None:
        all_fundamentals = load_all_fundamentals(fundamentals_dir)
    pit = point_in_time_fundamentals(all_fundamentals, as_of, settings.factors.report_lag_days)

    scores = compute_factor_scores(
        as_of,
        settings=settings,
        price_panel=price_panel,
        fundamentals_pit=pit,
        universe_snapshot=universe_snapshot,
        eligible_symbols=eligible_symbols,
    )
    log.info(
        "factor scores computed",
        extra={"date": str(as_of), "universe": int(len(scores)),
               "stale": int(scores["stale"].sum()) if len(scores) else 0},
    )
    return scores


def write_factor_scores(scores: pd.DataFrame, engine) -> int:
    """Write factor scores to the ``factor_scores`` table, idempotent per date.

    Existing rows for each date in ``scores`` are deleted first, so re-running a
    date overwrites cleanly rather than duplicating. NaN sub-scores become SQL NULL.
    """
    from sqlalchemy import delete, insert
    from engine.db import factor_scores

    if scores.empty:
        return 0
    clean = scores.astype(object).where(pd.notna(scores), None)
    records = clean.to_dict("records")
    with engine.begin() as conn:
        for d in scores["date"].unique():
            conn.execute(delete(factor_scores).where(factor_scores.c.date == d))
        conn.execute(insert(factor_scores), records)
    return len(records)
