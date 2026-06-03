import logging
import json
import os
import pandas as pd
import lightgbm as lgb
from datetime import date
from typing import Literal
from qsde.db.connection import read_sql, execute_sql
from qsde.risk.trade_levels import compute_trade_levels

log = logging.getLogger(__name__)

# Minimum trailing-20d average daily value traded (rupees) for a name to be
# considered TRADEABLE. The intraday stress test proved the model's edge is
# fill-fiction below ~Rs 10 crore/day ADV. Override via env.
LIQUIDITY_MIN_RUPEES = float(os.getenv("QSDE_LIQUIDITY_MIN_CR", "10")) * 1e7


def _compute_adv_map() -> dict[str, float]:
    """Trailing-20d average daily value traded (rupees) per symbol.

    value_traded = close * volume. Returns {symbol: latest_adv20}. Used to
    annotate each signal with liquidity so the serve layer can surface only
    tradeable names.
    """
    ohlcv = read_sql(
        """SELECT symbol, date, close, volume
             FROM ohlcv
            WHERE date >= (CURRENT_DATE - INTERVAL '45 days')
         ORDER BY symbol, date"""
    )
    if ohlcv.empty:
        return {}
    ohlcv["dv"] = ohlcv["close"].astype(float) * ohlcv["volume"].astype(float)
    adv = (
        ohlcv.groupby("symbol")["dv"]
             .apply(lambda s: s.tail(20).mean())   # last 20 sessions
    )
    return {sym: float(v) for sym, v in adv.items() if pd.notna(v)}

def generate_signals(horizon: Literal["intraday", "swing", "long"] = "swing"):
    """
    Generate today's trading signals using the trained LightGBM model.
    Reads the latest factors, predicts, and saves to the signals table.
    """
    import os
    model_path = os.path.join(os.path.dirname(__file__), "weights", f"lgbm_{horizon}.txt")
    if not os.path.exists(model_path):
        log.error(f"Model file not found: {model_path}. Train the model first.")
        return 0
        
    model = lgb.Booster(model_file=model_path)
    
    # 1. Fetch latest factors
    log.info("Fetching latest point-in-time factors...")
    # CRITICAL: take the LATEST value per (symbol, factor_name), not every
    # historical row. `valid_to = 'infinity'` is true for EVERY uncorrected
    # historical date (87M rows), so the old query fetched the whole panel and
    # the default pivot_table aggfunc=mean AVERAGED each factor across all
    # history — both catastrophically slow (~20 min) AND wrong (signals on
    # time-averaged factors, not current ones). DISTINCT ON + a recent
    # as_of_date window fixes both: ~53k rows in seconds, latest values only.
    factors = read_sql(
        """
        SELECT DISTINCT ON (symbol, factor_name)
               symbol, factor_name, factor_value
          FROM factor_pit
         WHERE valid_to = 'infinity'::timestamptz
           AND as_of_date >= (CURRENT_DATE - INTERVAL '20 days')
      ORDER BY symbol, factor_name, as_of_date DESC
        """
    )

    if factors.empty:
        log.warning("No factors found.")
        return 0

    # One value per (symbol, factor_name) now, so this is a pure reshape
    # (mean over a single value is a no-op) — no cross-time averaging.
    factors_wide = factors.pivot_table(
        index="symbol",
        columns="factor_name",
        values="factor_value",
        aggfunc="last",
    ).reset_index()
    
    # Fill missing with 0
    factors_wide = factors_wide.fillna(0)
    
    # Check features expected by model
    model_features = model.feature_name()
    missing_features = [f for f in model_features if f not in factors_wide.columns]
    for f in missing_features:
        factors_wide[f] = 0.0
        
    X_pred = factors_wide[model_features]
    
    # 2. Predict
    log.info("Running predictions...")
    preds = model.predict(X_pred)
    factors_wide["predicted_return"] = preds
    
    # Rank predictions to create signals
    # Top 10% = 1 (BUY), Bottom 10% = -1 (SELL), Rest = 0 (HOLD)
    factors_wide["rank_pct"] = factors_wide["predicted_return"].rank(pct=True)
    factors_wide["direction"] = 0
    factors_wide.loc[factors_wide["rank_pct"] > 0.9, "direction"] = 1
    factors_wide.loc[factors_wide["rank_pct"] < 0.1, "direction"] = -1
    
    # Confidence is scaled based on percentile distance from median
    factors_wide["confidence"] = abs(factors_wide["rank_pct"] - 0.5) * 2
    
    # 3. Factor attribution. We don't compute per-instance SHAP here, so we
    # surface the model's top features by GLOBAL gain importance, normalized
    # to a share (0-1). The previous code multiplied the raw factor value by
    # importance, which produced absurd numbers for level-form features
    # (e.g. tech_obv_slope = -150,733,221) — fixed.
    global_importances = dict(zip(model.feature_name(), model.feature_importance(importance_type="gain")))
    _imp_total = sum(global_importances.values()) or 1.0
    _ranked = sorted(global_importances.items(), key=lambda kv: kv[1], reverse=True)
    top_factors_global = [
        {"name": k, "contribution": round(v / _imp_total, 4)} for k, v in _ranked[:5]
    ]

    # Pull latest close per symbol once. Drives entry price for trade levels.
    closes_df = read_sql(
        """SELECT DISTINCT ON (symbol) symbol, close
             FROM ohlcv
         ORDER BY symbol, date DESC"""
    )
    close_map = {r.symbol: float(r.close) for r in closes_df.itertuples()}

    # Trailing-20d ADV per symbol -> liquidity flag. The stress test proved
    # the edge is fill-fiction below ~Rs 10cr/day, so we annotate every
    # signal and let the serve layer surface only tradeable names.
    adv_map = _compute_adv_map()
    n_liquid = sum(1 for v in adv_map.values() if v >= LIQUIDITY_MIN_RUPEES)
    log.info("Liquidity: %d/%d symbols >= Rs %.0fcr/day ADV",
             n_liquid, len(adv_map), LIQUIDITY_MIN_RUPEES / 1e7)

    today = date.today().isoformat()
    inserted = 0
    
    for _, row in factors_wide.iterrows():
        # Model's top features by normalized global importance (same per row;
        # honest and bounded — see note above).
        top_factors = top_factors_global

        # Compute trade levels from latest close + tech_atr_pct factor.
        sym = row["symbol"]
        price = close_map.get(sym)
        atr_val = row.get("tech_atr_pct")
        atr_pct = float(atr_val) if atr_val is not None and pd.notna(atr_val) and float(atr_val) > 0 else None
        # predicted_return is a triple-barrier SCORE, not a return — feeding it
        # as a return produced absurd targets (e.g. +25% intraday). Targets/
        # stops are purely volatility-based (ATR multiples per horizon); the
        # model's contribution is the DIRECTION (rank), not a magnitude.
        levels = compute_trade_levels(
            price=price,
            atr_pct=atr_pct,
            predicted_return=None,
            direction=int(row["direction"]),
            horizon=horizon,
        )

        adv_20d = adv_map.get(sym)
        is_liquid = bool(adv_20d is not None and adv_20d >= LIQUIDITY_MIN_RUPEES)

        execute_sql(
            """
            INSERT INTO signals (
                symbol, date, horizon, direction, confidence,
                predicted_return, ranking_score, top_factors,
                entry_price, target_price, stop_price, risk_reward,
                atr_pct, trade_quality, adv_20d, is_liquid
            ) VALUES (
                %(symbol)s, %(date)s, %(horizon)s, %(direction)s, %(confidence)s,
                %(predicted_return)s, %(ranking_score)s, %(top_factors)s,
                %(entry_price)s, %(target_price)s, %(stop_price)s, %(risk_reward)s,
                %(atr_pct)s, %(trade_quality)s, %(adv_20d)s, %(is_liquid)s
            ) ON CONFLICT (symbol, date, horizon) DO UPDATE SET
                direction = EXCLUDED.direction,
                confidence = EXCLUDED.confidence,
                predicted_return = EXCLUDED.predicted_return,
                ranking_score = EXCLUDED.ranking_score,
                top_factors = EXCLUDED.top_factors,
                entry_price = EXCLUDED.entry_price,
                target_price = EXCLUDED.target_price,
                stop_price = EXCLUDED.stop_price,
                risk_reward = EXCLUDED.risk_reward,
                atr_pct = EXCLUDED.atr_pct,
                trade_quality = EXCLUDED.trade_quality,
                adv_20d = EXCLUDED.adv_20d,
                is_liquid = EXCLUDED.is_liquid
            """,
            {
                "symbol": sym,
                "date": today,
                "horizon": horizon,
                "direction": int(row["direction"]),
                "confidence": float(row["confidence"]),
                "predicted_return": float(row["predicted_return"]),
                "ranking_score": float(row["rank_pct"]),
                "top_factors": json.dumps(top_factors),
                "entry_price":   levels["entry"],
                "target_price":  levels["target"],
                "stop_price":    levels["stop"],
                "risk_reward":   levels["risk_reward"],
                "atr_pct":       levels["atr_pct"],
                "trade_quality": levels["quality"],
                "adv_20d":       adv_20d,
                "is_liquid":     is_liquid,
            }
        )
        inserted += 1
        
    log.info(f"Generated and saved {inserted} {horizon} signals.")
    return inserted
