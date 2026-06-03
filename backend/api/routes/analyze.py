"""
On-demand single-stock analysis endpoint.

Lets the user analyze ANY NSE or BSE listed equity without first ingesting
it into the universe. Fetches live from yfinance, computes the same 48
factors the production models were trained on, runs both swing and long
LightGBM models, and returns a one-shot research view.

Two routes:

  GET  /api/analyze/{symbol}        Read-only: fetch, compute, predict.
                                    No DB writes. ~5-15s depending on
                                    yfinance latency.

  POST /api/analyze/{symbol}/pin    Same fetch + compute, then persists
                                    into universe, ohlcv, fundamentals,
                                    factor_pit, and signals tables so the
                                    symbol appears in every downstream UI
                                    afterward.

Symbol resolution: tries `.NS` (NSE) first, then `.BO` (BSE) if NSE has
no data. Bare symbols (`RELIANCE`) and qualified ones (`RELIANCE.NS`)
both work. BSE numeric codes (`500325`) work via the .BO fallback.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
import time
from datetime import date, datetime, timedelta, timezone
from typing import Any, Optional

import lightgbm as lgb
import numpy as np
import pandas as pd
from fastapi import APIRouter, HTTPException, Query
from psycopg2.extras import execute_batch

from qsde.config import settings
from qsde.db import execute_sql, get_sync_conn, read_sql, upsert_dataframe
from qsde.factors.fundamental import compute_fundamental_factors_from_filings
from qsde.factors.technical import compute_all_technical
from qsde.risk.trade_levels import compute_trade_levels

log = logging.getLogger(__name__)
router = APIRouter()


# --- yfinance fetch helpers --------------------------------------------------

def _resolve_yf_symbol(user_symbol: str) -> tuple[str, str, str]:
    """Normalize a user-typed symbol into (yfinance_symbol, internal_symbol, exchange).

    Tries .NS first, falls back to .BO. Returns the first variant whose
    yfinance call returns non-empty OHLCV history.
    """
    import yfinance as yf

    raw = user_symbol.strip().upper()
    if raw.endswith(".NS"):
        candidates = [(raw, raw[:-3], "NSE")]
    elif raw.endswith(".BO"):
        candidates = [(raw, raw[:-3], "BSE")]
    else:
        candidates = [
            (f"{raw}.NS", raw, "NSE"),
            (f"{raw}.BO", raw, "BSE"),
        ]

    for yf_sym, internal, exch in candidates:
        try:
            test = yf.Ticker(yf_sym).history(period="5d", auto_adjust=False)
            if not test.empty:
                return yf_sym, internal, exch
        except Exception as e:
            log.debug("Probe %s failed: %s", yf_sym, e)
    raise HTTPException(
        status_code=404,
        detail=f"Symbol {user_symbol!r} not found on NSE or BSE via yfinance.",
    )


def _fetch_yf_ohlcv(yf_symbol: str, years: int = 7) -> pd.DataFrame:
    """Fetch OHLCV from yfinance and return in the same shape as our DB rows.

    Columns: date (DatetimeIndex), open, high, low, close, adj_close, volume.
    """
    import yfinance as yf

    start = (date.today() - timedelta(days=365 * years)).isoformat()
    df = yf.download(
        yf_symbol, start=start, auto_adjust=False, progress=False, threads=False,
    )
    if df.empty:
        raise HTTPException(status_code=404, detail=f"No OHLCV for {yf_symbol}")

    # yfinance can return either flat or multi-index columns depending on
    # version + single vs batch. Normalize to flat lowercase.
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    df = df.rename(columns={
        "Open": "open", "High": "high", "Low": "low",
        "Close": "close", "Adj Close": "adj_close", "Volume": "volume",
    })
    df.index = pd.to_datetime(df.index)
    df.index.name = "date"
    return df[["open", "high", "low", "close", "adj_close", "volume"]].dropna(subset=["close"])


def _fetch_yf_fundamentals(yf_symbol: str) -> tuple[pd.DataFrame, dict]:
    """Fetch quarterly fundamentals + latest info snapshot from yfinance.

    Returns (filings_df, info_dict) where filings_df has one row per
    quarterly snapshot with the schema columns expected by
    compute_fundamental_factors_from_filings, and info_dict has the
    current Ticker.info snapshot (P/E, margins, etc.).

    yfinance does NOT expose historical fiscal_date-level ratios reliably
    on free tier. We approximate by using the latest ticker.info snapshot
    and synthesizing 1-4 fiscal points by replaying it backward at quarter
    boundaries. Good enough for the on-demand view -- not for backtesting.
    """
    import yfinance as yf

    tk = yf.Ticker(yf_symbol)
    info: dict = {}
    try:
        info = tk.info or {}
    except Exception as e:
        log.warning("Ticker.info failed for %s: %s", yf_symbol, e)

    # Single snapshot row dated today.
    today = pd.Timestamp.today().normalize()
    fiscal = today - pd.Timedelta(days=45)   # quarter end ~45d before "filing"
    row = {
        "fiscal_date":  fiscal,
        "filing_date":  today,
        "pe_ratio":     info.get("trailingPE"),
        "pb_ratio":     info.get("priceToBook"),
        "ev_ebitda":    info.get("enterpriseToEbitda"),
        "ev_to_revenue":info.get("enterpriseToRevenue"),
        "roe":          info.get("returnOnEquity"),
        "roa":          info.get("returnOnAssets"),
        "roce":         None,
        "roic":         None,
        "gross_margin":     info.get("grossMargins"),
        "operating_margin": info.get("operatingMargins"),
        "net_margin":       info.get("profitMargins"),
        "debt_equity":      info.get("debtToEquity"),
        "dividend_yield":   info.get("dividendYield"),
        "fcf_yield":        None,
        "revenue_growth_yoy": info.get("revenueGrowth"),
        "eps_growth_yoy":     info.get("earningsGrowth"),
        "market_cap":       info.get("marketCap"),
        "enterprise_value": info.get("enterpriseValue"),
        "revenue":          info.get("totalRevenue"),
        "net_income":       info.get("netIncomeToCommon"),
        "eps":              info.get("trailingEps"),
        "free_cash_flow":   info.get("freeCashflow"),
    }
    filings = pd.DataFrame([row])
    return filings, info


# --- Model load + predict ----------------------------------------------------

_MODEL_CACHE: dict[str, lgb.Booster] = {}


def _load_model(horizon: str) -> Optional[lgb.Booster]:
    if horizon in _MODEL_CACHE:
        return _MODEL_CACHE[horizon]
    path = os.path.join(
        os.path.dirname(__file__), "..", "..", "qsde", "models", "weights",
        f"lgbm_{horizon}.txt",
    )
    path = os.path.abspath(path)
    if not os.path.exists(path):
        return None
    booster = lgb.Booster(model_file=path)
    _MODEL_CACHE[horizon] = booster
    return booster


def _predict_with_model(
    booster: lgb.Booster,
    factors_wide: pd.DataFrame,
) -> tuple[float, list[dict]]:
    """Predict for the LAST row of factors_wide. Return (prediction, top_factors).

    Top factors come from LightGBM's pred_contrib output (Shapley values).
    """
    expected = booster.feature_name()
    # Align columns + coerce to float64. None / strings (yfinance returns
    # these for fundamentals it doesn't cover, common on BSE-only and
    # smallcap names) become NaN, which LightGBM handles natively.
    cols = {}
    for col in expected:
        if col in factors_wide.columns:
            cols[col] = pd.to_numeric(factors_wide[col], errors="coerce").values
        else:
            cols[col] = np.full(len(factors_wide), np.nan)
    aligned = pd.DataFrame(cols, index=factors_wide.index, dtype=np.float64)

    # Predict on the last row only.
    last_row = aligned.tail(1)
    pred = float(booster.predict(last_row)[0])

    # SHAP-like contributions (last column is the bias / expected value).
    contribs = booster.predict(last_row, pred_contrib=True)[0]
    pairs = list(zip(expected, contribs[:-1]))  # drop bias term
    pairs.sort(key=lambda p: abs(p[1]), reverse=True)
    top = [{"name": n, "contribution": float(c)} for n, c in pairs[:8] if not np.isnan(c)]
    return pred, top


# Per-horizon thresholds that respect the time-scaling of returns.
# A 0.3% move over 1 day is meaningful; the same move over 20 days is noise.
# These are calibrated so the threshold is roughly 1× the average daily ATR
# scaled by sqrt(horizon_days) -- the lower bound at which a directional bet
# has positive expected value after typical NSE round-trip costs (~10bps).
#
#   "threshold"  = |pred| below which we call HOLD (no edge above costs).
#   "conf_scale" = |pred| value mapped to 100% magnitude-score (saturation).
#   "atr_fallback" = used when factor_pit has no tech_atr_pct for the symbol.
_HORIZON_CALIB: dict[str, dict[str, float]] = {
    "intraday": {"threshold": 0.003, "conf_scale": 0.012, "atr_fallback": 0.010},
    "swing":    {"threshold": 0.008, "conf_scale": 0.035, "atr_fallback": 0.022},
    "long":     {"threshold": 0.020, "conf_scale": 0.080, "atr_fallback": 0.045},
}


def _classify_direction(prediction: float, horizon: str = "swing") -> int:
    """Convert a raw return prediction to a direction signal {-1, 0, +1}.

    Per-horizon thresholds (see _HORIZON_CALIB). A prediction whose magnitude
    is below the threshold is HOLD regardless of sign — it's below the noise
    floor + transaction costs for that horizon.
    """
    threshold = _HORIZON_CALIB.get(horizon, _HORIZON_CALIB["swing"])["threshold"]
    if prediction > threshold:  return 1
    if prediction < -threshold: return -1
    return 0


def _confidence(prediction: float, horizon: str = "swing") -> float:
    """Magnitude score [0, 1] — the FALLBACK confidence.

    Used only when the meta-model for `horizon` hasn't been trained yet.
    `compute_meta_confidence()` below is preferred and the call site picks
    the better of the two when both are available.
    """
    scale = _HORIZON_CALIB.get(horizon, _HORIZON_CALIB["swing"])["conf_scale"]
    return float(min(1.0, abs(prediction) / scale))


# Meta-model cache (loaded lazily on first request per horizon).
_META_MODEL_CACHE: dict[str, Any] = {}


def _load_meta_model(horizon: str):
    if horizon in _META_MODEL_CACHE:
        return _META_MODEL_CACHE[horizon]
    try:
        from qsde.models.meta_model import load_meta_model
        booster = load_meta_model(horizon)
    except Exception as e:  # noqa: BLE001
        log.debug("meta-model load failed for %s: %s", horizon, e)
        booster = None
    _META_MODEL_CACHE[horizon] = booster
    return booster


def compute_meta_confidence(
    horizon: str,
    factors_wide: pd.DataFrame,
    primary_prediction: float,
) -> Optional[float]:
    """If a trained meta-model exists for `horizon`, return P(primary correct).

    Returns None when the meta-model isn't on disk yet — caller should fall
    back to _confidence() in that case.
    """
    booster = _load_meta_model(horizon)
    if booster is None:
        return None
    try:
        from qsde.models.meta_model import meta_predict
        # The meta-model expects the same features + a column called
        # `primary_pred`. Build a one-row frame matching that shape.
        last = factors_wide.tail(1).copy()
        last["primary_pred"] = float(primary_prediction)
        proba = meta_predict(booster, last)
        return float(proba[0]) if len(proba) else None
    except Exception as e:  # noqa: BLE001
        log.debug("meta_predict failed for %s: %s", horizon, e)
        return None


def _atr_fallback_for(horizon: str) -> float:
    """Horizon-scaled ATR fallback when factor_pit has no row for this symbol.
    Scales as sqrt(horizon_days) which matches volatility's time-scaling."""
    return _HORIZON_CALIB.get(horizon, _HORIZON_CALIB["swing"])["atr_fallback"]


# Horizon -> (expected_hold_days, valid_until_label) used by /analyze cards.
# `hold_days` is calendar-ish trading-days; UI surfaces it as "Hold ~N sessions".
# `valid_until` tells the user how long the snapshot's recommendation stands
# before the model should be re-run -- crucial because the user complained
# BUY/SELL with no expiry is meaningless.
_HORIZON_META = {
    "intraday": {"hold_sessions": 1,  "valid_sessions": 1,
                 "valid_label": "today's close (15:30 IST)"},
    "swing":    {"hold_sessions": 5,  "valid_sessions": 1,
                 "valid_label": "next session open"},
    "long":     {"hold_sessions": 20, "valid_sessions": 5,
                 "valid_label": "next 5 sessions"},
}


def _action_tier(direction: int, predicted_return: float, horizon: str) -> str:
    """Tier the action by ABSOLUTE predicted return per horizon.

    Tiers in MULTIPLES of the horizon's direction threshold:
      |pred| >= 3.0× threshold   -> STRONG_{BUY,SELL}    big move, act now
      |pred| >= 1.5× threshold   -> {BUY, SELL}          normal sizing
      |pred| >= 1.0× threshold   -> {WATCH_LONG, WATCH_SHORT}
      below threshold            -> HOLD

    Driving the tier off |pred| (not off the magnitude-score) means a
    STRONG signal really IS a bigger predicted move — not just an
    artifact of saturation in min(1, |pred|/scale).
    """
    if direction == 0:
        return "HOLD"
    threshold = _HORIZON_CALIB.get(horizon, _HORIZON_CALIB["swing"])["threshold"]
    mag = abs(predicted_return)
    side = "BUY" if direction > 0 else "SELL"
    if mag >= 3.0 * threshold:
        return f"STRONG_{side}"
    if mag >= 1.5 * threshold:
        return side
    return "WATCH_LONG" if direction > 0 else "WATCH_SHORT"


# ── NSE trading calendar ─────────────────────────────────────────────
#
# Baseline = high-confidence NSE holidays for 2026 + 2027. Anything we're
# less sure about (Holi / Diwali / Eid — all moon-dependent) is left out
# rather than risk wrong dates.
#
# The user can override / extend by pointing QSDE_NSE_HOLIDAYS_FILE at a
# JSON file with a list of "YYYY-MM-DD" strings; those merge with the
# baseline on every call (cheap — single file open, ~30 dates).
_NSE_HOLIDAY_BASELINE: set[str] = {
    # 2026
    "2026-01-26",  # Republic Day
    "2026-04-03",  # Good Friday
    "2026-04-14",  # Dr. Ambedkar Jayanti
    "2026-05-01",  # Maharashtra Day
    "2026-08-15",  # Independence Day (Sat — but listed for safety)
    "2026-10-02",  # Gandhi Jayanti
    "2026-12-25",  # Christmas
    # 2027
    "2027-01-26",  # Republic Day
    "2027-03-26",  # Good Friday
    "2027-04-14",  # Dr. Ambedkar Jayanti
    "2027-08-15",  # Independence Day (Sun)
    "2027-10-02",  # Gandhi Jayanti (Sat)
    "2027-12-25",  # Christmas (Sat)
}


def _load_nse_holidays() -> set[str]:
    """Baseline ∪ user-supplied JSON (env: QSDE_NSE_HOLIDAYS_FILE)."""
    extra: set[str] = set()
    path = os.getenv("QSDE_NSE_HOLIDAYS_FILE")
    if path and os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, list):
                extra = {str(d) for d in data}
        except Exception as e:  # noqa: BLE001
            log.warning("Failed to read NSE holidays from %s: %s", path, e)
    return _NSE_HOLIDAY_BASELINE | extra


def _next_trading_session_iso(n_sessions: int) -> str:
    """Skip weekends AND NSE holidays. Falls back to Mon-Fri-only when the
    holiday file is missing or empty -- always safe direction (never
    promises a date that lands on a known closed day)."""
    import datetime as _dt
    holidays = _load_nse_holidays()
    d = _dt.date.today()
    added = 0
    # Guard against pathological holiday clusters: cap at 90 calendar days.
    for _ in range(90):
        if added >= n_sessions:
            break
        d = d + _dt.timedelta(days=1)
        if d.weekday() >= 5:
            continue   # Sat/Sun
        if d.isoformat() in holidays:
            continue   # NSE holiday
        added += 1
    return d.isoformat()


# --- Cross-sectional rank (honest direction in the triple-barrier era) -------
#
# After AFML triple-barrier labeling, the model output is a cross-sectional
# SCORE, not a return. Its absolute sign/magnitude is uninformative (the
# intraday model emits all-negative scores). Direction must come from where
# the score RANKS against the rest of the universe today — not from an
# absolute threshold. We read the universe's score distribution from the
# signals table (populated by signal_generator across all names).

def _cross_sectional_rank(pred: float, horizon: str) -> Optional[float]:
    """Percentile [0,1] of `pred` within today's universe scores for this
    horizon. None if no distribution is available (e.g. signals not yet
    generated)."""
    try:
        df = read_sql(
            """SELECT predicted_return
                 FROM signals
                WHERE horizon = :h
                  AND date = (SELECT MAX(date) FROM signals WHERE horizon = :h)""",
            params={"h": horizon},
        )
        if df.empty or len(df) < 20:
            return None
        scores = pd.to_numeric(df["predicted_return"], errors="coerce").dropna().to_numpy()
        if scores.size < 20:
            return None
        return float((scores < pred).mean())
    except Exception as e:  # noqa: BLE001
        log.debug("cross-sectional rank failed for %s: %s", horizon, e)
        return None


def _direction_from_rank(rank_pct: Optional[float]) -> int:
    """Top decile -> +1 (BUY candidate), bottom decile -> -1, else HOLD."""
    if rank_pct is None:
        return 0
    if rank_pct >= 0.90:
        return 1
    if rank_pct <= 0.10:
        return -1
    return 0


def _action_tier_from_rank(direction: int, rank_pct: Optional[float]) -> str:
    """Tier the action by how extreme the cross-sectional rank is — the
    honest analogue of _action_tier for a score model.

      BUY  side: rank>=0.98 STRONG_BUY, >=0.90 BUY, else WATCH_LONG
      SELL side: rank<=0.02 STRONG_SELL, <=0.10 SELL, else WATCH_SHORT
    """
    if direction == 0 or rank_pct is None:
        return "HOLD"
    if direction > 0:
        if rank_pct >= 0.98:
            return "STRONG_BUY"
        if rank_pct >= 0.90:
            return "BUY"
        return "WATCH_LONG"
    if rank_pct <= 0.02:
        return "STRONG_SELL"
    if rank_pct <= 0.10:
        return "SELL"
    return "WATCH_SHORT"


# --- Core analyze pipeline (shared by GET and POST routes) -------------------

def _try_db_ohlcv(internal_symbol: str, years: int = 7) -> Optional[pd.DataFrame]:
    """Fast path for pinned symbols: read OHLCV straight from the local
    `ohlcv` table. If the symbol isn't pinned (or has < 252 rows in DB),
    return None so the caller falls through to Kite/yfinance.

    This avoids the 5-15s yfinance roundtrip on every /analyze call for
    symbols that already live in the system. Returns the same DataFrame
    shape as the other fetchers: DatetimeIndex named 'date', columns
    open/high/low/close/adj_close/volume.
    """
    try:
        cutoff = date.today() - timedelta(days=365 * years)
        df = read_sql(
            """SELECT date, open, high, low, close, adj_close, volume
                 FROM ohlcv
                WHERE symbol = :sym AND date >= :cutoff
             ORDER BY date""",
            params={"sym": internal_symbol, "cutoff": cutoff},
        )
        if df.empty or len(df) < 252:
            return None
        df["date"] = pd.to_datetime(df["date"])
        df = df.set_index("date")
        return df[["open", "high", "low", "close", "adj_close", "volume"]]
    except Exception as e:  # noqa: BLE001
        log.debug("DB OHLCV read failed for %s: %s", internal_symbol, e)
        return None


def _try_kite_ohlcv(internal_symbol: str, years: int = 7) -> Optional[pd.DataFrame]:
    """Best-effort OHLCV pull from Kite. Returns None if the data source
    is set to yfinance, the symbol isn't found in kite_instruments, or
    Kite isn't authenticated. Caller falls back to yfinance on None.
    """
    if settings.market_data_source.lower() != "kite":
        return None
    try:
        from qsde.ingestion.kite_client import get_kite_client
        client = get_kite_client()
        if not client.is_authenticated:
            log.info("Kite not authenticated; falling back to yfinance.")
            return None
        if client.get_instrument_token(internal_symbol) is None:
            log.info("%s not in kite_instruments; falling back to yfinance.", internal_symbol)
            return None
        df = client.historical_ohlcv(
            symbol=internal_symbol,
            from_date=(date.today() - timedelta(days=365 * years)),
            to_date=date.today(),
            interval="day",
        )
        return df if not df.empty else None
    except Exception as e:
        log.warning("Kite OHLCV fetch failed for %s: %s; falling back to yfinance.",
                    internal_symbol, e)
        return None


def _analyze_pipeline(user_symbol: str) -> dict:
    """End-to-end: fetch + factor + predict. Used by both GET and pin POST.

    OHLCV source order:
      1. local DB (`ohlcv`)  -- pinned symbols, instant
      2. Kite               -- if MARKET_DATA_SOURCE=kite and authenticated
      3. yfinance           -- final fallback

    Fundamentals always come from yfinance because Kite doesn't expose them.
    """
    yf_symbol, internal, exchange = _resolve_yf_symbol(user_symbol)

    log.info("Analyzing %s -> %s (%s)", user_symbol, yf_symbol, exchange)
    ohlcv = _try_db_ohlcv(internal)
    data_source_used = "db" if ohlcv is not None else None
    if ohlcv is None:
        ohlcv = _try_kite_ohlcv(internal)
        if ohlcv is not None:
            data_source_used = "kite"
    if ohlcv is None:
        data_source_used = "yfinance"
        ohlcv = _fetch_yf_ohlcv(yf_symbol)
    if len(ohlcv) < 252:
        raise HTTPException(
            status_code=400,
            detail=(
                f"{yf_symbol}: only {len(ohlcv)} days of OHLCV. "
                "Need at least 252 (1 trading year) to compute factors."
            ),
        )

    filings, info = _fetch_yf_fundamentals(yf_symbol)

    # Compute the same factor set the model was trained on.
    tech = compute_all_technical(ohlcv)
    fund = compute_fundamental_factors_from_filings(filings, ohlcv.index)
    factors_wide = pd.concat([tech, fund], axis=1)

    # Latest close drives both the response payload and the trade-level entry.
    latest_close = float(ohlcv["close"].iloc[-1])

    # Pull ATR % from the computed factor frame -- it's the same column the
    # production models use, so the level math here is consistent with what
    # the user sees on /factors and /signals.
    atr_series = factors_wide.get("tech_atr_pct")
    if atr_series is not None and len(atr_series) > 0:
        atr_pct_value = atr_series.iloc[-1]
        atr_pct_for_levels: Optional[float] = (
            float(atr_pct_value) if pd.notna(atr_pct_value) else None
        )
    else:
        atr_pct_for_levels = None

    # Predict for all three horizons.
    signals = {}
    for horizon in ("intraday", "swing", "long"):
        booster = _load_model(horizon)
        if booster is None:
            signals[horizon] = {"error": "model not trained yet"}
            continue
        pred, top = _predict_with_model(booster, factors_wide)
        # Direction from CROSS-SECTIONAL RANK (score era), not the raw score's
        # sign. Falls back to the legacy return-threshold only when no
        # universe distribution exists (e.g. signals not generated yet).
        rank_pct = _cross_sectional_rank(pred, horizon)
        if rank_pct is not None:
            direction = _direction_from_rank(rank_pct)
        else:
            direction = _classify_direction(pred, horizon=horizon)
        # `pred` is a triple-barrier SCORE, not a return — using it as a
        # return magnitude produced absurd targets (+25% intraday). Targets/
        # stops are volatility-based (ATR per horizon); direction comes from
        # the cross-sectional rank above.
        levels = compute_trade_levels(
            price=latest_close,
            atr_pct=atr_pct_for_levels,
            predicted_return=None,
            direction=direction,
            horizon=horizon,
        )
        # Prefer the meta-model's calibrated probability when available;
        # fall back to the magnitude score otherwise.
        meta_conf = compute_meta_confidence(horizon, factors_wide, pred)
        conf = meta_conf if meta_conf is not None else _confidence(pred, horizon=horizon)
        conf_source = "meta_model" if meta_conf is not None else "magnitude_score"
        meta = _HORIZON_META[horizon]
        # Rank-based action tier when we have a cross-section; else legacy.
        action = (_action_tier_from_rank(direction, rank_pct)
                  if rank_pct is not None
                  else _action_tier(direction, pred, horizon=horizon))
        from qsde.models.edge_stats import horizon_edge
        signals[horizon] = {
            "horizon":          horizon,
            "predicted_return": pred,
            "rank_pct":         rank_pct,            # cross-sectional percentile [0,1]
            "edge":             horizon_edge(horizon),  # validated net-Sharpe band
            "direction":        direction,
            "confidence":       conf,
            "confidence_source": conf_source,                 # 'meta_model' or 'magnitude_score'
            "action":           action,                       # tiered (STRONG_BUY, BUY, WATCH_LONG, ...)
            "hold_sessions":    meta["hold_sessions"],        # expected hold (NSE sessions)
            "valid_sessions":   meta["valid_sessions"],       # how long this snapshot stands
            "valid_until_label": meta["valid_label"],         # human label for the UI
            "valid_until_date":  _next_trading_session_iso(meta["valid_sessions"]),
            "exit_by_date":      _next_trading_session_iso(meta["hold_sessions"]),
            "top_factors":      top,
            "model_version":    f"lgbm_{horizon}_purgedcv",
            "entry_price":      levels["entry"],
            "target_price":     levels["target"],
            "stop_price":       levels["stop"],
            "risk_reward":      levels["risk_reward"],
            "atr_pct":          levels["atr_pct"],
            "trade_quality":    levels["quality"],
            "trade_notes":      levels["notes"],
        }

    week_change = float((ohlcv["close"].iloc[-1] / ohlcv["close"].iloc[-6] - 1) * 100) if len(ohlcv) >= 6 else None
    month_change = float((ohlcv["close"].iloc[-1] / ohlcv["close"].iloc[-22] - 1) * 100) if len(ohlcv) >= 22 else None
    year_change = float((ohlcv["close"].iloc[-1] / ohlcv["close"].iloc[-252] - 1) * 100) if len(ohlcv) >= 252 else None

    return {
        "input_symbol":     user_symbol,
        "yf_symbol":        yf_symbol,
        "internal_symbol":  internal,
        "exchange":         exchange,
        "company_name":     info.get("longName") or info.get("shortName"),
        "sector":           info.get("sector"),
        "industry":         info.get("industry"),
        "latest_close":     latest_close,
        "latest_date":      str(ohlcv.index[-1].date()),
        "price_changes":    {
            "1_week":  week_change,
            "1_month": month_change,
            "1_year":  year_change,
        },
        "fundamentals":     {
            "market_cap":         info.get("marketCap"),
            "enterprise_value":   info.get("enterpriseValue"),
            "trailing_pe":        info.get("trailingPE"),
            "price_to_book":      info.get("priceToBook"),
            "ev_to_ebitda":       info.get("enterpriseToEbitda"),
            "roe":                info.get("returnOnEquity"),
            "operating_margin":   info.get("operatingMargins"),
            "net_margin":         info.get("profitMargins"),
            "debt_to_equity":     info.get("debtToEquity"),
            "dividend_yield":     info.get("dividendYield"),
            "revenue_growth_yoy": info.get("revenueGrowth"),
            "earnings_growth":    info.get("earningsGrowth"),
            "total_revenue":      info.get("totalRevenue"),
            "free_cashflow":      info.get("freeCashflow"),
        },
        "signals":          signals,
        "n_ohlcv_rows":     int(len(ohlcv)),
        "n_factors":        int(factors_wide.shape[1]),
        "data_source":      data_source_used,
    }


# ── /analyze response cache ──────────────────────────────────────────
#
# /analyze is heavy: yfinance + ~50 technical factors + 3 model predictions.
# Within a single trading session, repeated views of the same symbol return
# identical results (until end-of-day prices change). Cache by (symbol, today)
# with a small TTL so the panel + research page + screener-Custom don't each
# re-fetch on every navigation.
_ANALYZE_CACHE: dict[tuple[str, str], tuple[float, dict]] = {}
_ANALYZE_CACHE_LOCK = threading.Lock()
_ANALYZE_TTL_SEC = float(os.getenv("QSDE_ANALYZE_TTL_SEC", "300"))  # 5 min default


def _analyze_cache_get(key: tuple[str, str]) -> Optional[dict]:
    with _ANALYZE_CACHE_LOCK:
        entry = _ANALYZE_CACHE.get(key)
        if entry is None:
            return None
        ts, payload = entry
        if time.monotonic() - ts > _ANALYZE_TTL_SEC:
            _ANALYZE_CACHE.pop(key, None)
            return None
        return payload


def _analyze_cache_put(key: tuple[str, str], payload: dict) -> None:
    with _ANALYZE_CACHE_LOCK:
        # Bound the cache so it can't grow unbounded across a long uvicorn run.
        if len(_ANALYZE_CACHE) > 500:
            _ANALYZE_CACHE.clear()
        _ANALYZE_CACHE[key] = (time.monotonic(), payload)


@router.get("/analyze/{symbol}")
def analyze(symbol: str):
    """On-demand analyze of any NSE / BSE listed equity. No DB writes.

    Cached for QSDE_ANALYZE_TTL_SEC (default 300s) keyed on (symbol, today)
    so repeat views are sub-second. Pin still calls _analyze_pipeline
    directly so pin writes are never stale.
    """
    cache_key = (symbol.upper().strip(), str(date.today()))
    cached = _analyze_cache_get(cache_key)
    if cached is not None:
        return {**cached, "_cache": "hit"}
    try:
        payload = _analyze_pipeline(symbol)
    except HTTPException:
        raise
    except Exception as e:
        log.exception("analyze failed for %s", symbol)
        raise HTTPException(status_code=500, detail=f"Analyze failed: {e}")
    _analyze_cache_put(cache_key, payload)
    return {**payload, "_cache": "miss"}


# --- Pin: persist the analyzed symbol into the regular pipeline --------------

def _persist_pinned(payload: dict) -> dict:
    """Write a pinned symbol's data into universe, ohlcv, fundamentals,
    factor_pit, and signals so it appears alongside the seeded Nifty 200.

    Re-fetches OHLCV + fundamentals to get the long-form data
    (the analyze response only carries the latest snapshot).
    """
    import json as _json

    internal = payload["internal_symbol"]
    yf_symbol = payload["yf_symbol"]

    # 1. Universe row.
    upsert_dataframe(
        pd.DataFrame([{
            "symbol":          internal,
            "company_name":    payload.get("company_name"),
            "sector":          payload.get("sector"),
            "industry":        payload.get("industry"),
            "index_membership": _json.dumps([payload.get("exchange") or "UNKNOWN", "MANUAL_PIN"]),
            "is_active":       True,
        }]),
        table="universe",
        conflict_columns=["symbol"],
        update_columns=["company_name", "sector", "industry", "index_membership", "is_active"],
    )

    # 2. Full OHLCV history.
    ohlcv = _fetch_yf_ohlcv(yf_symbol)
    ohlcv = ohlcv.reset_index().rename(columns={"index": "date"})
    if "date" not in ohlcv.columns:
        ohlcv = ohlcv.rename(columns={ohlcv.columns[0]: "date"})
    ohlcv["symbol"] = internal
    ohlcv["source"] = "yfinance_pin"
    ohlcv["date"] = pd.to_datetime(ohlcv["date"]).dt.date
    upsert_dataframe(
        ohlcv[["symbol", "date", "open", "high", "low", "close", "adj_close", "volume", "source"]],
        table="ohlcv",
        conflict_columns=["symbol", "date"],
        update_columns=["open", "high", "low", "close", "adj_close", "volume", "source"],
    )

    # 3. Latest fundamentals snapshot.
    filings, info = _fetch_yf_fundamentals(yf_symbol)
    fund_row = filings.iloc[0].to_dict()
    fund_row["symbol"] = internal
    fund_row["fiscal_date"] = pd.to_datetime(fund_row["fiscal_date"]).date()
    fund_row["filing_date"] = pd.to_datetime(fund_row["filing_date"]).date()
    fund_row["source"] = "yfinance_pin"
    # Add the extra schema columns the regular ingestion populates.
    for col in ("market_cap", "enterprise_value"):
        if col not in fund_row and col in info:
            fund_row[col] = info.get(col)
    upsert_dataframe(
        pd.DataFrame([fund_row]),
        table="fundamentals",
        conflict_columns=["symbol", "fiscal_date", "filing_date"],
    )

    # 4. Compute + persist factors into factor_pit.
    ohlcv_indexed = ohlcv.set_index(pd.to_datetime(ohlcv["date"]))
    ohlcv_indexed = ohlcv_indexed[["open", "high", "low", "close", "volume"]]
    tech = compute_all_technical(ohlcv_indexed)
    fund = compute_fundamental_factors_from_filings(filings, ohlcv_indexed.index)
    factors_wide = pd.concat([tech, fund], axis=1)
    factors_wide["symbol"] = internal

    from qsde.factors.pit_writer import write_factors_to_pit
    n_written = write_factors_to_pit(factors_wide, data_source="analyze_pin")

    # 5. Insert signals for all horizons (today's date), incl. trade levels.
    today = date.today()
    rows = []
    for horizon, sig in payload.get("signals", {}).items():
        if "predicted_return" not in sig: continue
        top_factors = sig.get("top_factors") or []
        rows.append({
            "symbol":             internal,
            "date":               today,
            "horizon":            horizon,
            "direction":          sig["direction"],
            "confidence":         sig["confidence"],
            "predicted_return":   sig["predicted_return"],
            "ranking_score":      sig["predicted_return"],   # use raw pred as score
            "factor_attribution": _json.dumps(top_factors),
            "top_factors":        _json.dumps(top_factors[:5]),
            "model_version":      sig.get("model_version"),
            "model_hash":         hashlib.sha256(
                f"{sig.get('model_version','')}-{today.isoformat()}".encode()
            ).hexdigest(),
            "entry_price":        sig.get("entry_price"),
            "target_price":       sig.get("target_price"),
            "stop_price":         sig.get("stop_price"),
            "risk_reward":        sig.get("risk_reward"),
            "atr_pct":            sig.get("atr_pct"),
            "trade_quality":      sig.get("trade_quality"),
        })
    if rows:
        upsert_dataframe(
            pd.DataFrame(rows),
            table="signals",
            conflict_columns=["symbol", "date", "horizon"],
            update_columns=[
                "direction", "confidence", "predicted_return", "ranking_score",
                "factor_attribution", "top_factors", "model_version", "model_hash",
                "entry_price", "target_price", "stop_price", "risk_reward",
                "atr_pct", "trade_quality",
            ],
        )

    return {
        "pinned":             True,
        "symbol":             internal,
        "ohlcv_rows_written": int(len(ohlcv)),
        "factor_rows_written": int(n_written),
        "signals_written":    len(rows),
    }


@router.post("/analyze/{symbol}/pin")
def analyze_pin(symbol: str):
    """Analyze the symbol AND persist into the regular pipeline.

    The symbol will appear in /signals, /screener, /watchlist (if added),
    and /research/{symbol} after this call. Also busts the /analyze cache
    for this symbol so the next GET reflects the freshly-pinned DB state.
    """
    try:
        payload = _analyze_pipeline(symbol)
        meta = _persist_pinned(payload)
        # Invalidate any cached pre-pin view of this symbol.
        cache_key = (symbol.upper().strip(), str(date.today()))
        with _ANALYZE_CACHE_LOCK:
            _ANALYZE_CACHE.pop(cache_key, None)
        return {**payload, "persistence": meta}
    except HTTPException:
        raise
    except Exception as e:
        log.exception("pin failed for %s", symbol)
        raise HTTPException(status_code=500, detail=f"Pin failed: {e}")
