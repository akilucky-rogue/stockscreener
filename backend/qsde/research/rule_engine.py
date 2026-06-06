"""Tier 1 rule-based signal engine — orchestrator.

Loads OHLCV for the active liquid universe, computes 4 factor scores per
symbol per horizon, cross-sectionally ranks within the universe, builds the
IC-weighted composite, and returns a per-symbol signal frame ready for the
signal writer.

Strategies emitted per (symbol, date, horizon):
  - tier1_jt        Jegadeesh-Titman 12-1 cross-sectional momentum
  - tier1_mop       Moskowitz-Ooi-Pedersen time-series momentum (vol-scaled)
  - tier1_bab       Frazzini-Pedersen betting-against-beta tilt
  - tier1_rsi2      Connors-Alvarez RSI(2) mean reversion (swing only)
  - tier1_composite IC-weighted blend of the above

Direction (+1 / 0 / -1) is assigned by cross-sectional decile:
  top decile (rank >= 0.9)    -> +1 long
  bottom decile (rank <= 0.1) -> -1 short
  else                        -> 0 hold

Important design choices
------------------------
1. Live signals compute scores directly from raw OHLCV. We do NOT read
   factor_pit for the four Tier 1 factors — the formulas are pure and
   cheap (sub-second for 500 symbols). factor_pit is reserved for the
   weekly drift / IC backfill jobs.
2. Liquidity gate matches the ML path exactly: trailing-20d ADV >= ₹10cr
   (env-overridable). Same `LIQUIDITY_MIN_RUPEES` constant.
3. The market benchmark for BAB is an equal-weighted NIFTY 50 daily-close
   series built from OHLCV. When market_cap data is available we
   upgrade to cap-weighting; until then equal-weight is a fine first pass.
4. Composite IC weights come from `rule_factor_ic_latest`. If that view
   has no rows (cold start), we fall back to equal-weight. As paper
   sessions accumulate, the weekly validator backfills IC and the
   composite self-tunes.
"""
from __future__ import annotations

import logging
import os
from datetime import date
from typing import Optional

import numpy as np
import pandas as pd

from qsde.db.connection import read_sql
from qsde.factors.rules import (
    FACTOR_NAMES,
    HORIZON_FACTORS,
    bab_score,
    composite_rank_ic_weighted,
    connors_rsi2_score,
    cross_sectional_rank,
    jegadeesh_titman_score,
    mop_tsmom_score,
)

log = logging.getLogger(__name__)


# ──────────────────────────────────────────────────────────────────────
# Tunables (env-overridable so we can experiment without code changes)
# ──────────────────────────────────────────────────────────────────────

LIQUIDITY_MIN_RUPEES = float(os.getenv("QSDE_LIQUIDITY_MIN_CR", "10")) * 1e7

# How much OHLCV history to load. JT needs 252+21 = 273 days; we add buffer
# for warmup of vol estimation, SMA(200), etc.
OHLCV_LOOKBACK_DAYS = int(os.getenv("QSDE_TIER1_OHLCV_LOOKBACK", "400"))

# Decile thresholds for direction assignment. Default top/bottom 10%.
LONG_DECILE = float(os.getenv("QSDE_TIER1_LONG_DECILE", "0.10"))   # top 10%
SHORT_DECILE = float(os.getenv("QSDE_TIER1_SHORT_DECILE", "0.10")) # bottom 10%

# Benchmark — we use NIFTY 50 as the broad-market proxy for BAB.
BENCHMARK_INDEX = os.getenv("QSDE_TIER1_BENCHMARK", "NIFTY 50")


# ──────────────────────────────────────────────────────────────────────
# Data loaders
# ──────────────────────────────────────────────────────────────────────

def load_active_universe() -> pd.DataFrame:
    """Active symbols with their NSE/BSE-index membership.

    Returns
    -------
    pd.DataFrame
        columns: symbol, index_membership (list[str]), market_cap, sector
    """
    df = read_sql(
        """
        SELECT symbol,
               index_membership,
               market_cap,
               sector
          FROM universe
         WHERE is_active = TRUE
        """
    )
    if df.empty:
        log.warning("Active universe is empty; rule engine will produce no signals")
    return df


def load_close_panel(
    symbols: list[str],
    lookback_days: int = OHLCV_LOOKBACK_DAYS,
) -> pd.DataFrame:
    """OHLCV close prices for `symbols` over the last `lookback_days` sessions.

    Returns
    -------
    pd.DataFrame
        Rows = trading dates (ascending), columns = symbols, values = close.
        Missing symbol/date combos are NaN.
    """
    if not symbols:
        return pd.DataFrame()

    # Use parameter binding for the IN clause via a literal join — psycopg2
    # doesn't bind a Python list well across all driver versions.
    sym_list = ",".join(f"'{s}'" for s in symbols)
    sql = f"""
        SELECT symbol, date, close
          FROM ohlcv
         WHERE symbol IN ({sym_list})
           AND date >= (CURRENT_DATE - INTERVAL '{int(lookback_days)} days')
      ORDER BY symbol, date
    """
    long_df = read_sql(sql)
    if long_df.empty:
        return pd.DataFrame()
    long_df["date"] = pd.to_datetime(long_df["date"])
    long_df["close"] = long_df["close"].astype(float)
    wide = long_df.pivot(index="date", columns="symbol", values="close").sort_index()
    return wide


def compute_adv_map() -> dict[str, float]:
    """Trailing-20d average daily value traded (₹) per symbol.

    Mirrors signal_generator._compute_adv_map() so the rule-engine and
    ML pipeline gate liquidity identically.
    """
    ohlcv = read_sql(
        """
        SELECT symbol, date, close, volume
          FROM ohlcv
         WHERE date >= (CURRENT_DATE - INTERVAL '45 days')
        """
    )
    if ohlcv.empty:
        return {}
    ohlcv["dv"] = ohlcv["close"].astype(float) * ohlcv["volume"].astype(float)
    adv = (
        ohlcv.sort_values(["symbol", "date"])
             .groupby("symbol")["dv"]
             .apply(lambda s: s.tail(20).mean())
    )
    return {sym: float(v) for sym, v in adv.items() if pd.notna(v)}


def load_latest_atr_map() -> dict[str, float]:
    """Latest tech_atr_pct per symbol (used for trade-level computation).

    Mirrors the load pattern in models/signal_generator.py — `DISTINCT ON`
    grabs the latest valid_to=infinity row per (symbol).
    """
    df = read_sql(
        """
        SELECT DISTINCT ON (symbol)
               symbol, factor_value
          FROM factor_pit
         WHERE factor_name = 'tech_atr_pct'
           AND valid_to = 'infinity'::timestamptz
           AND as_of_date >= (CURRENT_DATE - INTERVAL '20 days')
      ORDER BY symbol, as_of_date DESC
        """
    )
    if df.empty:
        return {}
    return {str(r["symbol"]): float(r["factor_value"]) for _, r in df.iterrows()
            if pd.notna(r["factor_value"])}


def load_ic_weights(horizon: str) -> dict[str, float]:
    """Latest IC-derived composite weights from rule_factor_ic_latest view.

    Returns
    -------
    dict[factor_name, weight]
        Always contains all 4 factor names. Cold start = equal-weight 0.25.
    """
    df = read_sql(
        """
        SELECT factor_name, composite_weight
          FROM rule_factor_ic_latest
         WHERE horizon = :h
        """,
        params={"h": horizon},
    )
    if df.empty:
        return {name: 0.25 for name in FACTOR_NAMES}

    raw = {str(r["factor_name"]): float(r["composite_weight"]) for _, r in df.iterrows()}
    # Ensure all factors are represented (missing -> 0 weight).
    weights = {name: raw.get(name, 0.0) for name in FACTOR_NAMES}
    return weights


# ──────────────────────────────────────────────────────────────────────
# Benchmark construction
# ──────────────────────────────────────────────────────────────────────

def build_benchmark_series(
    close_panel: pd.DataFrame,
    universe_df: pd.DataFrame,
    index_name: str = BENCHMARK_INDEX,
) -> Optional[pd.Series]:
    """Equal-weighted close series for the constituents of `index_name`.

    Used as the market series in BAB beta estimation. We deliberately do
    NOT load a separate index quote — building the benchmark from the same
    OHLCV that drives stock scores means costs/holidays/halts are aligned.

    Returns None if no constituent symbols are present in close_panel.
    """
    # universe_df.index_membership is a JSONB list. Cast carefully.
    def _has_index(membership) -> bool:
        if not membership:
            return False
        try:
            return index_name in membership
        except TypeError:
            return False

    members = universe_df.loc[
        universe_df["index_membership"].apply(_has_index), "symbol"
    ].tolist()
    available = [s for s in members if s in close_panel.columns]
    if not available:
        log.warning("Benchmark %s has no available constituents in OHLCV panel", index_name)
        return None
    # Equal-weight returns -> reconstruct synthetic level series.
    # fill_method=None per pandas 3.0 future-proofing: we explicitly do NOT
    # forward-fill missing prices into fake "no-change" returns; halted/
    # missing days stay NaN and drop out of the mean.
    rets = close_panel[available].pct_change(fill_method=None)
    avg_ret = rets.mean(axis=1)
    synth_level = (1.0 + avg_ret.fillna(0)).cumprod() * 100.0
    synth_level.name = f"{index_name}_eq"
    return synth_level


# ──────────────────────────────────────────────────────────────────────
# Per-factor score computation across the universe
# ──────────────────────────────────────────────────────────────────────

def compute_jt_panel(close_panel: pd.DataFrame) -> pd.DataFrame:
    """Apply Jegadeesh-Titman to every column, return wide DataFrame."""
    return close_panel.apply(jegadeesh_titman_score, axis=0)


def compute_mop_panel(close_panel: pd.DataFrame) -> pd.DataFrame:
    return close_panel.apply(mop_tsmom_score, axis=0)


def compute_bab_panel(close_panel: pd.DataFrame, benchmark: pd.Series) -> pd.DataFrame:
    """BAB per symbol vs the synthetic benchmark."""
    return close_panel.apply(lambda s: bab_score(s, benchmark), axis=0)


def compute_rsi2_panel(close_panel: pd.DataFrame) -> pd.DataFrame:
    return close_panel.apply(connors_rsi2_score, axis=0)


# ──────────────────────────────────────────────────────────────────────
# Main entry: per-horizon orchestrator
# ──────────────────────────────────────────────────────────────────────

def run_for_horizon(
    horizon: str,
    as_of_date: Optional[date] = None,
) -> pd.DataFrame:
    """Generate Tier 1 signals for `horizon` as of `as_of_date` (default today).

    Returns
    -------
    pd.DataFrame
        One row per (strategy, symbol) for symbols with a non-NaN score.
        Columns: strategy, symbol, date, horizon, score, rank_pct,
                 direction, confidence.

        Empty for intraday (Tier 1 deliberately does not trade intraday —
        daily-bar factors are unsuitable for sub-day horizons).
    """
    if horizon not in HORIZON_FACTORS:
        raise ValueError(f"Unknown horizon: {horizon!r}")
    if not HORIZON_FACTORS[horizon]:
        log.info("Tier 1 has no factors for horizon=%s; skipping", horizon)
        return pd.DataFrame()

    as_of_date = as_of_date or date.today()

    # ── 1. Load data ─────────────────────────────────────────────────
    universe_df = load_active_universe()
    if universe_df.empty:
        return pd.DataFrame()

    symbols = universe_df["symbol"].tolist()
    close_panel = load_close_panel(symbols)
    if close_panel.empty:
        log.warning("No OHLCV loaded; rule engine returning empty")
        return pd.DataFrame()

    # Restrict to the most recent session present in the panel — we score
    # signals AS OF the last bar.
    latest_session = close_panel.index.max().date()
    log.info("Tier 1 engine: horizon=%s, latest_session=%s, symbols=%d",
             horizon, latest_session, close_panel.shape[1])

    # ── 2. Liquidity gate ────────────────────────────────────────────
    adv_map = compute_adv_map()
    liquid_symbols = [
        s for s in close_panel.columns
        if adv_map.get(s, 0.0) >= LIQUIDITY_MIN_RUPEES
    ]
    if not liquid_symbols:
        log.warning("No liquid symbols after ADV gate (>= ₹%.0fcr); empty signals",
                    LIQUIDITY_MIN_RUPEES / 1e7)
        return pd.DataFrame()
    log.info("Liquid universe: %d of %d symbols", len(liquid_symbols), close_panel.shape[1])

    # ── 3. Benchmark for BAB ─────────────────────────────────────────
    benchmark = build_benchmark_series(close_panel, universe_df)

    # ── 4. Per-factor score panels (full universe, then we slice) ────
    factor_panels: dict[str, pd.DataFrame] = {}
    horizon_factors = HORIZON_FACTORS[horizon]

    if "jt" in horizon_factors:
        factor_panels["jt"] = compute_jt_panel(close_panel[liquid_symbols])
    if "mop" in horizon_factors:
        factor_panels["mop"] = compute_mop_panel(close_panel[liquid_symbols])
    if "bab" in horizon_factors and benchmark is not None:
        factor_panels["bab"] = compute_bab_panel(close_panel[liquid_symbols], benchmark)
    if "rsi2" in horizon_factors:
        factor_panels["rsi2"] = compute_rsi2_panel(close_panel[liquid_symbols])

    if not factor_panels:
        log.warning("No factor panels computed; empty signals")
        return pd.DataFrame()

    # ── 5. Cross-sectional rank per factor, latest session only ──────
    ranked_panels: dict[str, pd.DataFrame] = {
        name: cross_sectional_rank(panel) for name, panel in factor_panels.items()
    }

    # Get latest-session row per ranked factor (Series indexed by symbol).
    latest_idx = close_panel.index.max()
    latest_ranks_per_factor: dict[str, pd.Series] = {}
    for name, panel in ranked_panels.items():
        if latest_idx in panel.index:
            row = panel.loc[latest_idx].dropna()
            if not row.empty:
                latest_ranks_per_factor[name] = row

    if not latest_ranks_per_factor:
        log.warning("No per-factor scores on latest session %s; empty signals", latest_idx)
        return pd.DataFrame()

    # ── 6. Composite via IC-weighted blend ───────────────────────────
    ic_weights = load_ic_weights(horizon)
    log.info("Composite IC weights (horizon=%s): %s", horizon,
             {k: round(v, 3) for k, v in ic_weights.items()})

    # Build a single-row DataFrame per factor for the composite helper.
    one_row_panels = {
        name: pd.DataFrame([series]) for name, series in latest_ranks_per_factor.items()
    }
    composite_df = composite_rank_ic_weighted(one_row_panels, ic_weights)
    composite_series = composite_df.iloc[0].dropna()

    # ── 7. Assemble per-strategy frames ──────────────────────────────
    horizon_str = horizon  # for clarity
    frames: list[pd.DataFrame] = []

    # signals.date is the CALENDAR date the signal is observable, not the
    # OHLCV bar date. This matches signal_generator.py (ML) so the downstream
    # paper_journal taker queries `date = today` and finds both ML and Tier 1
    # rows. On weekends/holidays latest_session lags today by 1-3 days; the
    # signal still "applies" today even though it was computed off Friday's
    # close.
    signal_calendar_date = as_of_date

    def _assemble(name: str, scores: pd.Series, strategy: str) -> pd.DataFrame:
        """Build a per-strategy DataFrame with direction + confidence."""
        if scores.empty:
            return pd.DataFrame()
        df = pd.DataFrame({
            "symbol": scores.index,
            "score": scores.values,
        })
        # rank_pct in [0, 1] (1 = best). We re-rank within liquid universe.
        df["rank_pct"] = df["score"].rank(pct=True, method="average")
        # Direction by decile.
        df["direction"] = 0
        df.loc[df["rank_pct"] >= (1.0 - LONG_DECILE), "direction"] = 1
        df.loc[df["rank_pct"] <= SHORT_DECILE, "direction"] = -1
        # Confidence: distance from neutral.
        df["confidence"] = (df["rank_pct"] - 0.5).abs() * 2.0  # [0, 1]
        df["strategy"] = strategy
        df["date"] = signal_calendar_date
        df["horizon"] = horizon_str
        return df[["strategy", "symbol", "date", "horizon",
                   "score", "rank_pct", "direction", "confidence"]]

    # Per-factor streams: tier1_jt, tier1_mop, tier1_bab, tier1_rsi2
    for name, series in latest_ranks_per_factor.items():
        frames.append(_assemble(name, series, f"tier1_{name}"))
    # Composite
    frames.append(_assemble("composite", composite_series, "tier1_composite"))

    out = pd.concat([f for f in frames if not f.empty], ignore_index=True)
    log.info("Tier 1 engine produced %d signal rows across %d strategies",
             len(out), out["strategy"].nunique() if not out.empty else 0)
    return out


__all__ = [
    "LIQUIDITY_MIN_RUPEES",
    "OHLCV_LOOKBACK_DAYS",
    "LONG_DECILE",
    "SHORT_DECILE",
    "BENCHMARK_INDEX",
    "load_active_universe",
    "load_close_panel",
    "compute_adv_map",
    "load_latest_atr_map",
    "load_ic_weights",
    "build_benchmark_series",
    "run_for_horizon",
]
