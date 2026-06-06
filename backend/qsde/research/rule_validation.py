"""Tier 1 factor validation — IC, hit rate, decile-spread Sharpe.

The single most important question in factor investing is "is this factor
still working?" The composite engine currently runs equal-weight 0.25 across
JT, MOP, BAB, RSI(2) because we have no realized data. As paper sessions
accumulate, this module computes:

  1. Information Coefficient (IC) per (factor, horizon, date):
       IC[d] = Spearman correlation between factor's cross-sectional rank
       and realized horizon-forward return across the universe, net of cost.

  2. Decile hit rates:
       fraction of top-decile picks that closed positive,
       fraction of bottom-decile picks that closed negative.

  3. Decile-spread annualized Sharpe:
       Sharpe of a long-top-decile / short-bottom-decile portfolio,
       equal-weighted within decile.

These feed rule_factor_ic. The composite_weight column is then read by
qsde.research.rule_engine.load_ic_weights() so next session's composite
self-tunes: factors with positive IC get larger weight, negative-IC factors
are zeroed out (per the "never short a noisy signal" robustness principle).

Research grounding
------------------
- Grinold (1989), "The Fundamental Law of Active Management": IR = IC × √breadth.
  We track IC because it is the noise-removed measure of "how much of the
  signal predicts forward return".

- Bailey & López de Prado (2014), "Deflated Sharpe Ratio": for very small
  N (n_observations < 20), point-estimate IC is noisy and easily fooled
  by data mining. We use a 60-session rolling window and gate the
  composite_weight to 0 when n_observations < 20.

- Lo (2004), "Adaptive Markets Hypothesis": factor edges decay as
  participants discover them. Rolling IC catches that decay in real time.

Cold start
----------
At cold start (no resolved signals), every helper returns NaN/empty and the
update writes composite_weight = 0 for that (factor, horizon). The engine
falls back to equal-weight, which is exactly what we want until we have
evidence.

Hermetic: no Kite/network calls, all DB-side.
"""
from __future__ import annotations

import logging
from datetime import date, timedelta
from typing import Optional

import numpy as np
import pandas as pd
from scipy.stats import spearmanr

from qsde.db.connection import execute_sql, read_sql
from qsde.factors.rules import FACTOR_NAMES
from qsde.risk.costs import cost_bps as horizon_cost_bps

log = logging.getLogger(__name__)


# ──────────────────────────────────────────────────────────────────────
# Constants
# ──────────────────────────────────────────────────────────────────────

# Horizon -> number of trading sessions to walk forward.
HORIZON_SESSIONS: dict[str, int] = {"swing": 5, "long": 20}

# IC rolling window (sessions). 60 is the conventional sweet spot:
# - Long enough to dampen single-day noise
# - Short enough to surface regime shifts (intra-quarter)
ROLLING_WINDOW = 60

# Minimum resolved-signal sessions before we trust the composite_weight.
# Below this threshold composite_weight = 0 (engine falls back to equal-weight).
MIN_OBSERVATIONS = 20

# Decile thresholds — must match qsde.research.rule_engine for consistency.
LONG_DECILE = 0.10
SHORT_DECILE = 0.10

# 252 trading days per year (NSE convention).
TRADING_DAYS_PER_YEAR = 252


# ──────────────────────────────────────────────────────────────────────
# Data loaders
# ──────────────────────────────────────────────────────────────────────

def _load_signals(strategy: str, horizon: str, since: date) -> pd.DataFrame:
    """All signal rows for (strategy, horizon) at or after `since`."""
    df = read_sql(
        """
        SELECT date, symbol, ranking_score
          FROM signals
         WHERE strategy = :s
           AND horizon  = :h
           AND date    >= :since
           AND ranking_score IS NOT NULL
        ORDER BY date, symbol
        """,
        params={"s": strategy, "h": horizon, "since": since},
    )
    if df.empty:
        return df
    df["date"] = pd.to_datetime(df["date"])
    df["ranking_score"] = df["ranking_score"].astype(float)
    return df


def _load_close_panel(symbols: list[str], since: date) -> pd.DataFrame:
    """Close-price panel (date × symbol) for the symbols + window."""
    if not symbols:
        return pd.DataFrame()
    sym_list = ",".join(f"'{s}'" for s in symbols)
    df = read_sql(
        f"""
        SELECT symbol, date, close
          FROM ohlcv
         WHERE symbol IN ({sym_list})
           AND date >= :since
        """,
        params={"since": since},
    )
    if df.empty:
        return df
    df["date"] = pd.to_datetime(df["date"])
    df["close"] = df["close"].astype(float)
    return df.pivot(index="date", columns="symbol", values="close").sort_index()


# ──────────────────────────────────────────────────────────────────────
# Core math
# ──────────────────────────────────────────────────────────────────────

def _cross_sectional_rank_by_date(sigs: pd.DataFrame) -> pd.DataFrame:
    """Add rank_pct ∈ [0, 1] within each date."""
    sigs = sigs.copy()
    sigs["rank_pct"] = sigs.groupby("date")["ranking_score"].rank(pct=True, method="average")
    return sigs


def _attach_realized_returns(
    sigs: pd.DataFrame,
    close_panel: pd.DataFrame,
    horizon: str,
) -> pd.DataFrame:
    """Add a realized_ret_net column = horizon-forward return net of cost.

    Resolves return as close[d + H] / close[d] - 1 using SESSION offsets
    (not calendar days). If close[d + H] is missing (symbol halted, not
    yet at end of horizon, etc.) the row is dropped.
    """
    h = HORIZON_SESSIONS[horizon]
    cost_fraction = float(horizon_cost_bps(horizon, paper_default=True)) / 1e4

    # Forward return per symbol = close.shift(-h) / close - 1, indexed by date.
    fwd_return = close_panel.shift(-h) / close_panel - 1.0 - cost_fraction

    # Stack into long form (date, symbol) -> realized_ret_net.
    # Pandas 2.x and 3.0 both accept the no-arg .stack(); the old
    # future_stack=True kwarg is gone in 3.0 so don't pass it.
    stacked = fwd_return.stack().rename("realized_ret_net")
    fwd_long = stacked.reset_index()
    # After reset_index() the first two columns are the source DataFrame's
    # index axis name and columns axis name. In production (where the panel
    # was built via pivot(index='date', columns='symbol')) those are
    # 'date' and 'symbol'. In tests built via DataFrame(data, index=...)
    # they're unnamed and pandas falls back to 'level_0', 'level_1'.
    # Positional rename makes this contract explicit and bug-proof either way.
    fwd_long.columns = ["date", "symbol", "realized_ret_net"]
    fwd_long["date"] = pd.to_datetime(fwd_long["date"])

    merged = sigs.merge(
        fwd_long,
        on=["date", "symbol"],
        how="left",
    )
    return merged.dropna(subset=["realized_ret_net"])


def compute_factor_ic_history(
    factor: str,
    horizon: str,
    lookback_days: int = ROLLING_WINDOW,
    as_of_date: Optional[date] = None,
) -> pd.DataFrame:
    """Per-date Spearman IC = corr(rank_pct, realized_ret_net) across symbols.

    Parameters
    ----------
    factor : str
        One of FACTOR_NAMES ('jt', 'mop', 'bab', 'rsi2').
    horizon : str
        'swing' or 'long'.
    lookback_days : int
        Calendar-day window for fetching signals. We need lookback + horizon
        worth of OHLCV so realized returns can resolve.
    as_of_date : date, optional
        Compute IC as if today were this date. Defaults to date.today().

    Returns
    -------
    pd.DataFrame with columns: date, ic, n_symbols.
        Empty when no resolved signals exist in the window.
    """
    if factor not in FACTOR_NAMES:
        raise ValueError(f"Unknown factor {factor!r}; must be one of {FACTOR_NAMES}")
    if horizon not in HORIZON_SESSIONS:
        raise ValueError(f"Unknown horizon {horizon!r}; supported: {list(HORIZON_SESSIONS)}")

    as_of_date = as_of_date or date.today()
    h_sessions = HORIZON_SESSIONS[horizon]

    # Pull a bit more lookback than asked for so the OHLCV walk-forward has room.
    fetch_since = as_of_date - timedelta(days=lookback_days + h_sessions * 2 + 5)
    strategy = f"tier1_{factor}"

    sigs = _load_signals(strategy, horizon, fetch_since)
    if sigs.empty:
        return pd.DataFrame(columns=["date", "ic", "n_symbols"])

    symbols = sorted(sigs["symbol"].astype(str).unique().tolist())
    close_panel = _load_close_panel(symbols, fetch_since)
    if close_panel.empty:
        return pd.DataFrame(columns=["date", "ic", "n_symbols"])

    ranked = _cross_sectional_rank_by_date(sigs)
    resolved = _attach_realized_returns(ranked, close_panel, horizon)
    if resolved.empty:
        return pd.DataFrame(columns=["date", "ic", "n_symbols"])

    # IC per date. Need >= 3 symbols on a date for Spearman to be meaningful.
    out_rows: list[dict] = []
    for d, group in resolved.groupby("date"):
        if len(group) < 3:
            continue
        rho, _ = spearmanr(group["rank_pct"].values, group["realized_ret_net"].values)
        if pd.isna(rho):
            continue
        out_rows.append({"date": d.date(), "ic": float(rho), "n_symbols": len(group)})

    if not out_rows:
        return pd.DataFrame(columns=["date", "ic", "n_symbols"])

    out = pd.DataFrame(out_rows).sort_values("date").reset_index(drop=True)
    # Trim to requested lookback (we over-fetched to give walk-forward room).
    cutoff = as_of_date - timedelta(days=lookback_days)
    return out[out["date"] >= cutoff].reset_index(drop=True)


def compute_factor_hit_rates(
    factor: str,
    horizon: str,
    lookback_days: int = ROLLING_WINDOW,
    as_of_date: Optional[date] = None,
) -> dict:
    """Top + bottom decile hit rates for the factor.

    Returns
    -------
    dict with keys: hit_rate_top, hit_rate_bot, n_top, n_bot.
        NaN values + n=0 when insufficient resolved signals.
    """
    as_of_date = as_of_date or date.today()
    h_sessions = HORIZON_SESSIONS[horizon]
    fetch_since = as_of_date - timedelta(days=lookback_days + h_sessions * 2 + 5)
    strategy = f"tier1_{factor}"

    sigs = _load_signals(strategy, horizon, fetch_since)
    if sigs.empty:
        return {"hit_rate_top": float("nan"), "hit_rate_bot": float("nan"),
                "n_top": 0, "n_bot": 0}

    symbols = sorted(sigs["symbol"].astype(str).unique().tolist())
    close_panel = _load_close_panel(symbols, fetch_since)
    if close_panel.empty:
        return {"hit_rate_top": float("nan"), "hit_rate_bot": float("nan"),
                "n_top": 0, "n_bot": 0}

    ranked = _cross_sectional_rank_by_date(sigs)
    resolved = _attach_realized_returns(ranked, close_panel, horizon)
    cutoff = as_of_date - timedelta(days=lookback_days)
    resolved = resolved[pd.to_datetime(resolved["date"]).dt.date >= cutoff]
    if resolved.empty:
        return {"hit_rate_top": float("nan"), "hit_rate_bot": float("nan"),
                "n_top": 0, "n_bot": 0}

    top = resolved[resolved["rank_pct"] >= (1.0 - LONG_DECILE)]
    bot = resolved[resolved["rank_pct"] <= SHORT_DECILE]
    n_top, n_bot = len(top), len(bot)
    hit_top = float((top["realized_ret_net"] > 0).mean()) if n_top else float("nan")
    hit_bot = float((bot["realized_ret_net"] < 0).mean()) if n_bot else float("nan")
    return {"hit_rate_top": hit_top, "hit_rate_bot": hit_bot,
            "n_top": int(n_top), "n_bot": int(n_bot)}


def compute_decile_spread_sharpe(
    factor: str,
    horizon: str,
    lookback_days: int = ROLLING_WINDOW,
    as_of_date: Optional[date] = None,
) -> dict:
    """Annualized Sharpe of long-top-decile / short-bot-decile spread portfolio.

    Daily portfolio return = mean(top realized_ret_net) - mean(bot realized_ret_net)
    Sharpe_ann = (mean / std) * sqrt(252)

    Returns
    -------
    dict with sharpe_ann (float | NaN), n_observations (int).
    """
    as_of_date = as_of_date or date.today()
    h_sessions = HORIZON_SESSIONS[horizon]
    fetch_since = as_of_date - timedelta(days=lookback_days + h_sessions * 2 + 5)
    strategy = f"tier1_{factor}"

    sigs = _load_signals(strategy, horizon, fetch_since)
    if sigs.empty:
        return {"sharpe_ann": float("nan"), "n_observations": 0}

    symbols = sorted(sigs["symbol"].astype(str).unique().tolist())
    close_panel = _load_close_panel(symbols, fetch_since)
    if close_panel.empty:
        return {"sharpe_ann": float("nan"), "n_observations": 0}

    ranked = _cross_sectional_rank_by_date(sigs)
    resolved = _attach_realized_returns(ranked, close_panel, horizon)
    cutoff = as_of_date - timedelta(days=lookback_days)
    resolved = resolved[pd.to_datetime(resolved["date"]).dt.date >= cutoff]
    if resolved.empty:
        return {"sharpe_ann": float("nan"), "n_observations": 0}

    daily_spread: list[float] = []
    for d, group in resolved.groupby("date"):
        top = group[group["rank_pct"] >= (1.0 - LONG_DECILE)]["realized_ret_net"]
        bot = group[group["rank_pct"] <= SHORT_DECILE]["realized_ret_net"]
        if top.empty or bot.empty:
            continue
        daily_spread.append(float(top.mean()) - float(bot.mean()))

    n = len(daily_spread)
    if n < 2:
        return {"sharpe_ann": float("nan"), "n_observations": n}

    arr = np.asarray(daily_spread, dtype=float)
    mean_ret = arr.mean()
    std_ret = arr.std(ddof=1)
    if std_ret == 0 or not np.isfinite(std_ret):
        return {"sharpe_ann": float("nan"), "n_observations": n}
    sharpe = (mean_ret / std_ret) * np.sqrt(TRADING_DAYS_PER_YEAR)
    return {"sharpe_ann": float(sharpe), "n_observations": n}


# ──────────────────────────────────────────────────────────────────────
# Orchestrator: write rule_factor_ic + composite_weight
# ──────────────────────────────────────────────────────────────────────

def update_composite_weights(as_of_date: Optional[date] = None) -> int:
    """Compute IC + hit rates + Sharpe for every (factor, horizon) and persist.

    For each combination of FACTOR_NAMES × HORIZON_SESSIONS keys:
      1. compute_factor_ic_history -> ic_60d = mean(ic) over the rolling window
      2. compute_factor_hit_rates -> hit_rate_top, hit_rate_bot
      3. compute_decile_spread_sharpe -> sharpe_ann
      4. composite_weight = max(ic_60d, 0) when n_observations >= MIN_OBSERVATIONS
         else 0 (engine falls back to equal-weight at load time)
      5. UPSERT into rule_factor_ic with today's as_of_date

    Returns the number of (factor, horizon) rows written.
    """
    as_of_date = as_of_date or date.today()
    rows_written = 0

    for factor in FACTOR_NAMES:
        for horizon in HORIZON_SESSIONS:
            # RSI(2) only applies to swing (per HORIZON_FACTORS in rules.py);
            # we still record a row for completeness so the API has a slot.
            ic_history = compute_factor_ic_history(factor, horizon, as_of_date=as_of_date)
            hit = compute_factor_hit_rates(factor, horizon, as_of_date=as_of_date)
            sp = compute_decile_spread_sharpe(factor, horizon, as_of_date=as_of_date)

            n_obs = int(ic_history["n_symbols"].sum()) if not ic_history.empty else 0
            ic_60d = float(ic_history["ic"].mean()) if not ic_history.empty else float("nan")

            # Composite weight: trust IC only with sufficient n.
            if np.isfinite(ic_60d) and n_obs >= MIN_OBSERVATIONS:
                composite_weight = max(ic_60d, 0.0)
            else:
                composite_weight = 0.0

            execute_sql(
                """
                INSERT INTO rule_factor_ic (
                    factor_name, horizon, as_of_date,
                    ic_60d, hit_rate_top, hit_rate_bot, sharpe_ann,
                    n_observations, composite_weight
                ) VALUES (
                    %(factor)s, %(horizon)s, %(as_of)s,
                    %(ic_60d)s, %(ht)s, %(hb)s, %(sh)s,
                    %(n)s, %(w)s
                ) ON CONFLICT (factor_name, horizon, as_of_date) DO UPDATE SET
                    ic_60d           = EXCLUDED.ic_60d,
                    hit_rate_top     = EXCLUDED.hit_rate_top,
                    hit_rate_bot     = EXCLUDED.hit_rate_bot,
                    sharpe_ann       = EXCLUDED.sharpe_ann,
                    n_observations   = EXCLUDED.n_observations,
                    composite_weight = EXCLUDED.composite_weight,
                    computed_at      = NOW()
                """,
                {
                    "factor": factor,
                    "horizon": horizon,
                    "as_of": as_of_date,
                    "ic_60d": None if not np.isfinite(ic_60d) else ic_60d,
                    "ht": None if not np.isfinite(hit["hit_rate_top"]) else hit["hit_rate_top"],
                    "hb": None if not np.isfinite(hit["hit_rate_bot"]) else hit["hit_rate_bot"],
                    "sh": None if not np.isfinite(sp["sharpe_ann"]) else sp["sharpe_ann"],
                    "n": n_obs,
                    "w": composite_weight,
                },
            )
            rows_written += 1
            log.info(
                "rule_factor_ic: %s/%s ic_60d=%s n_obs=%d w=%.3f hit_top=%s",
                factor, horizon,
                "nan" if not np.isfinite(ic_60d) else f"{ic_60d:.4f}",
                n_obs, composite_weight,
                "nan" if not np.isfinite(hit["hit_rate_top"]) else f"{hit['hit_rate_top']:.2f}",
            )

    return rows_written


__all__ = [
    "HORIZON_SESSIONS",
    "ROLLING_WINDOW",
    "MIN_OBSERVATIONS",
    "compute_factor_ic_history",
    "compute_factor_hit_rates",
    "compute_decile_spread_sharpe",
    "update_composite_weights",
]
