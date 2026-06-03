"""
Factor engine orchestrator — computes all factors for the universe.

Calls technical, fundamental, flow, and macro sub-modules.
Writes through PIT schema. Tracks rolling 63-day IC per factor.
"""

from __future__ import annotations

import logging
from datetime import date
from typing import Optional

import pandas as pd

from qsde.db import read_sql, get_sync_engine
from qsde.factors.technical import compute_all_technical
from qsde.factors.fundamental import compute_all_fundamental
from qsde.factors.flow import compute_all_flow
from qsde.factors.smc import compute_smc_features
from qsde.factors.patterns import compute_patterns
from qsde.factors.macro import compute_all_macro
from qsde.factors.sentiment import compute_all_sentiment

log = logging.getLogger(__name__)


def load_ohlcv(symbol: str, start: str = "2006-01-01", end: Optional[str] = None) -> pd.DataFrame:
    """Load OHLCV data for a symbol from the database."""
    if end is None:
        end = date.today().isoformat()
    df = read_sql(
        """SELECT date, open, high, low, close, volume
           FROM ohlcv WHERE symbol = :symbol AND date BETWEEN :start AND :end
           ORDER BY date""",
        params={"symbol": symbol, "start": start, "end": end},
    )
    if not df.empty:
        df["date"] = pd.to_datetime(df["date"])
        df = df.set_index("date")
    return df


def compute_factors_for_symbol(symbol: str, start: str = "2006-01-01") -> pd.DataFrame:
    """
    Compute all factors for a single symbol -- technical + fundamental + flow.

    Returns:
        DataFrame indexed by date with one column per factor and a `symbol`
        column. Empty if the symbol has < 252 days of OHLCV.
    """
    df = load_ohlcv(symbol, start=start)
    if df.empty or len(df) < 252:
        log.warning("Insufficient data for %s (%d rows)", symbol, len(df))
        return pd.DataFrame()

    # 1. Technical factors (~33 columns prefixed tech_)
    tech = compute_all_technical(df)

    # 1b. SMC / price-action structure factors (~35 columns prefixed smc_):
    #     liquidity sweeps, volume profile (POC/VAH/VAL), fair-value gaps,
    #     order blocks, BOS/CHoCH, supply/demand, EMA stack, trendline breaks,
    #     candle primitives. Lookback-safe. Never let one module break the panel.
    try:
        smc = compute_smc_features(df)
    except Exception as e:
        log.warning("SMC factors failed for %s: %s", symbol, e)
        smc = pd.DataFrame(index=df.index)

    # 1c. Candlestick pattern factors (~11 columns prefixed pattern_):
    #     engulfing, hammer, stars, soldiers/crows, doji + net counts.
    try:
        pat = compute_patterns(df)
    except Exception as e:
        log.warning("pattern factors failed for %s: %s", symbol, e)
        pat = pd.DataFrame(index=df.index)

    # 2. Fundamental factors (~15 columns prefixed fund_), PIT-correct
    #    via merge_asof on filing_date <= ohlcv_date.
    fund = compute_all_fundamental(symbol, df.index)

    # 3. Flow factors (4 columns prefixed flow_) from bulk_deals, lagged
    #    one day to avoid same-day lookahead.
    flow = compute_all_flow(symbol, df.index)

    # 4. Macro factors (market-wide FRED) — same per date, joined onto the index.
    try:
        macro = compute_all_macro(df.index)
    except Exception as e:
        log.warning("macro factors failed for %s: %s", symbol, e)
        macro = pd.DataFrame(index=df.index)

    # 5. Sentiment factors (Finnhub company-news buzz + polarity), per symbol.
    try:
        senti = compute_all_sentiment(symbol, df.index)
    except Exception as e:
        log.warning("sentiment factors failed for %s: %s", symbol, e)
        senti = pd.DataFrame(index=df.index)

    # Concat side-by-side along the date index. Empty frames from
    # missing fundamentals/bulk-deals just contribute no columns.
    parts = [tech]
    if not smc.empty:
        parts.append(smc)
    if not pat.empty:
        parts.append(pat)
    if not fund.empty:
        parts.append(fund)
    if not flow.empty:
        parts.append(flow)
    if not macro.empty:
        parts.append(macro)
    if not senti.empty:
        parts.append(senti)
    factors = pd.concat(parts, axis=1)
    factors["symbol"] = symbol

    return factors


def compute_factors_batch(
    symbols: list[str],
    start: str = "2006-01-01",
    write_tail_days: "int | None" = None,
) -> pd.DataFrame:
    """
    Compute factors for all symbols in the universe.

    Args:
        symbols:         universe to compute.
        start:           earliest OHLCV date to load (needs >= 252d before the
                         first date you care about so rolling factors are
                         correct).
        write_tail_days: if set, only PERSIST the last N calendar days of
                         factors to factor_pit (the freshly-changed rows),
                         while still computing the full series for correctness.
                         For a daily EOD refresh this turns a 14M-row rewrite
                         into a ~0.5M-row append — minutes -> seconds. None
                         (default) writes everything, as a full backfill wants.

    Returns:
        Wide DataFrame (date-indexed) of all computed factors (the FULL
        series, regardless of write_tail_days).
    """
    all_frames = []
    for i, sym in enumerate(symbols):
        if (i + 1) % 20 == 0:
            log.info("Computing factors: %d/%d symbols", i + 1, len(symbols))
        factors = compute_factors_for_symbol(sym, start=start)
        if not factors.empty:
            all_frames.append(factors)

    if not all_frames:
        return pd.DataFrame()

    combined = pd.concat(all_frames)
    log.info("Computed factors for %d symbols, %d total rows", len(all_frames), len(combined))

    # Decide what to persist. Default = everything; daily refresh = tail only.
    to_write = combined
    if write_tail_days is not None:
        cutoff = pd.Timestamp.today().normalize() - pd.Timedelta(days=write_tail_days)
        if isinstance(combined.index, pd.DatetimeIndex):
            to_write = combined[combined.index >= cutoff]
        elif "date" in combined.columns:
            to_write = combined[pd.to_datetime(combined["date"]) >= cutoff]
        log.info("write_tail_days=%d -> persisting %d of %d rows (last %dd only)",
                 write_tail_days, len(to_write), len(combined), write_tail_days)

    from qsde.factors.pit_writer import write_factors_to_pit
    written = write_factors_to_pit(to_write)
    log.info(f"Persisted {written} individual factor records to factor_pit.")

    return combined


def compute_rolling_ic(
    factors_df: pd.DataFrame,
    forward_returns: pd.Series,
    window: int = 63,
) -> pd.DataFrame:
    """
    Compute rolling 63-day Spearman IC per factor.

    Args:
        factors_df: Wide DataFrame of factor values indexed by date.
        forward_returns: Series of forward returns aligned by date.
        window: Rolling window in trading days.

    Returns:
        DataFrame with one column per factor, values are rolling IC.
    """
    from scipy.stats import spearmanr

    factor_cols = [c for c in factors_df.columns if c.startswith(("tech_", "smc_", "pattern_", "fund_", "flow_", "macro_", "sentiment_"))]
    ic_results = {}

    for col in factor_cols:
        aligned = pd.DataFrame({"factor": factors_df[col], "fwd_ret": forward_returns}).dropna()
        if len(aligned) < window:
            continue

        ic_series = aligned["factor"].rolling(window).corr(aligned["fwd_ret"])
        ic_results[col] = ic_series

    return pd.DataFrame(ic_results)
