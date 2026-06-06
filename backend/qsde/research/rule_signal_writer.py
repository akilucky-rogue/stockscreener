"""Tier 1 rule-based signal writer.

Takes the per-strategy frame from `rule_engine.run_for_horizon` and writes
each row to the `signals` table with the appropriate strategy tag and a
fully populated trade plan (entry/target/stop/risk_reward/atr_pct/quality)
computed by `qsde.risk.trade_levels.compute_trade_levels` — the same
function the ML pipeline uses, so Tier 1 and ML signals are directly
comparable.

The strategy column was added in migration 011; PRIMARY KEY is
(strategy, symbol, date, horizon) so per-strategy upserts are safe.
"""
from __future__ import annotations

import json
import logging
from typing import Optional

import pandas as pd

from qsde.db.connection import execute_sql, read_sql
from qsde.research.rule_engine import (
    LIQUIDITY_MIN_RUPEES,
    compute_adv_map,
    load_latest_atr_map,
)
from qsde.risk.trade_levels import compute_trade_levels

log = logging.getLogger(__name__)


def _latest_close(symbol: str) -> Optional[float]:
    """Single-symbol latest close — used as entry price."""
    df = read_sql(
        "SELECT close FROM ohlcv WHERE symbol = :s ORDER BY date DESC LIMIT 1",
        params={"s": symbol},
    )
    if df.empty:
        return None
    return float(df.iloc[0]["close"])


def _latest_close_map(symbols: list[str]) -> dict[str, float]:
    """Batched latest-close lookup — avoids N round-trips for N symbols."""
    if not symbols:
        return {}
    sym_list = ",".join(f"'{s}'" for s in symbols)
    df = read_sql(f"""
        SELECT DISTINCT ON (symbol) symbol, close
          FROM ohlcv
         WHERE symbol IN ({sym_list})
      ORDER BY symbol, date DESC
    """)
    return {str(r["symbol"]): float(r["close"]) for _, r in df.iterrows()
            if pd.notna(r["close"])}


def write_rule_signals(signals_df: pd.DataFrame) -> int:
    """Write Tier 1 signals to the `signals` table.

    Parameters
    ----------
    signals_df : pd.DataFrame
        Output of rule_engine.run_for_horizon — columns:
            strategy, symbol, date, horizon, score, rank_pct,
            direction, confidence

    Returns
    -------
    int
        Number of rows upserted.

    Notes
    -----
    Per-row work:
      1. Look up entry price from latest OHLCV close
      2. Look up ATR from latest tech_atr_pct factor (fallback in trade_levels)
      3. compute_trade_levels(...) -> target/stop/R:R/quality
      4. Annotate ADV / is_liquid (mirrors ML signal_generator semantics)
      5. UPSERT into signals
    """
    if signals_df.empty:
        log.info("Tier 1 writer: empty input, nothing to write")
        return 0

    symbols = sorted(set(signals_df["symbol"].astype(str)))
    close_map = _latest_close_map(symbols)
    atr_map = load_latest_atr_map()
    adv_map = compute_adv_map()

    inserted = 0
    for _, row in signals_df.iterrows():
        symbol = str(row["symbol"])
        strategy = str(row["strategy"])
        horizon = str(row["horizon"])
        direction = int(row["direction"])
        confidence = float(row["confidence"])
        ranking_score = float(row["score"])
        rank_pct = float(row["rank_pct"])

        # Use the engine-supplied date if present, else fall back to today.
        signal_date = pd.to_datetime(row["date"]).date()

        entry_price = close_map.get(symbol)
        atr_pct_raw = atr_map.get(symbol)
        # trade_levels handles both fraction (0.018) and percent (1.8) inputs.
        atr_pct = atr_pct_raw

        levels = compute_trade_levels(
            price=entry_price,
            atr_pct=atr_pct,
            predicted_return=None,   # Tier 1 doesn't forecast magnitude
            direction=direction,
            horizon=horizon,
        )

        # `top_factors` for ML signals stores SHAP attributions as
        #   [{"name": "...", "contribution": <signed float>}, ...]
        # The dashboard renders this array (slice/map). Tier 1 must use the
        # SAME array shape — otherwise the page crashes with
        # "top_factors.slice is not a function" on tier1 rows.
        #
        # For Tier 1 we surface:
        #   - the underlying strategy name with raw score as contribution
        #   - centered rank_pct (so negative = below median, positive = above)
        # Both signed, both numeric, both renderable by the existing
        # dashboard code with zero frontend changes per row type.
        strategy_short = strategy.replace("tier1_", "")
        top_factors = [
            {"name": f"strategy:{strategy_short}", "contribution": ranking_score},
            {"name": "rank_pct_centered", "contribution": (rank_pct - 0.5) * 2.0},
        ]

        adv_20d = adv_map.get(symbol)
        is_liquid = bool(adv_20d is not None and adv_20d >= LIQUIDITY_MIN_RUPEES)

        execute_sql(
            """
            INSERT INTO signals (
                strategy, symbol, date, horizon, direction, confidence,
                predicted_return, ranking_score, top_factors,
                model_version,
                entry_price, target_price, stop_price, risk_reward,
                atr_pct, trade_quality, adv_20d, is_liquid
            ) VALUES (
                %(strategy)s, %(symbol)s, %(date)s, %(horizon)s,
                %(direction)s, %(confidence)s,
                %(predicted_return)s, %(ranking_score)s, %(top_factors)s,
                %(model_version)s,
                %(entry_price)s, %(target_price)s, %(stop_price)s, %(risk_reward)s,
                %(atr_pct)s, %(trade_quality)s, %(adv_20d)s, %(is_liquid)s
            ) ON CONFLICT (strategy, symbol, date, horizon) DO UPDATE SET
                direction        = EXCLUDED.direction,
                confidence       = EXCLUDED.confidence,
                predicted_return = EXCLUDED.predicted_return,
                ranking_score    = EXCLUDED.ranking_score,
                top_factors      = EXCLUDED.top_factors,
                model_version    = EXCLUDED.model_version,
                entry_price      = EXCLUDED.entry_price,
                target_price     = EXCLUDED.target_price,
                stop_price       = EXCLUDED.stop_price,
                risk_reward      = EXCLUDED.risk_reward,
                atr_pct          = EXCLUDED.atr_pct,
                trade_quality    = EXCLUDED.trade_quality,
                adv_20d          = EXCLUDED.adv_20d,
                is_liquid        = EXCLUDED.is_liquid
            """,
            {
                "strategy":         strategy,
                "symbol":           symbol,
                "date":             signal_date,
                "horizon":          horizon,
                "direction":        direction,
                "confidence":       confidence,
                "predicted_return": None,
                "ranking_score":    ranking_score,
                # JSONB column — Postgres accepts the JSON-string cast just
                # as cleanly as a Json adapter, and stdlib json is universal
                # across psycopg2/SQLAlchemy/pandas versions.
                "top_factors":      json.dumps(top_factors),
                "model_version":    f"{strategy}-v1",
                "entry_price":      levels.get("entry"),
                "target_price":     levels.get("target"),
                "stop_price":       levels.get("stop"),
                "risk_reward":      levels.get("risk_reward"),
                "atr_pct":          levels.get("atr_pct"),
                "trade_quality":    levels.get("quality"),
                "adv_20d":          adv_20d,
                "is_liquid":        is_liquid,
            },
        )
        inserted += 1

    log.info("Tier 1 writer: upserted %d signal rows", inserted)
    return inserted


__all__ = ["write_rule_signals"]
