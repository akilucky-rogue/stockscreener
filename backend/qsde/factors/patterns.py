"""
Candlestick pattern factors.

Ported into QSDE from StockTrack/stocktrack/factors/patterns.py (Phase 0.3).
Uses pandas-ta's `cdl_pattern` family when available for a curated subset with
statistical edge on Indian equities; falls back to hand-coded detectors for the
top patterns so the factor surface is stable even without pandas-ta.

Each `pattern_*` column is int: +1 bullish, -1 bearish, 0 none. Complements the
candle primitives already emitted by qsde/factors/smc.py with multi-bar patterns
(morning/evening star, three soldiers/crows). Wired into factors/engine.py.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

try:
    import pandas_ta as pta
    _HAS_PTA = True
except Exception:  # noqa: BLE001
    pta = None  # type: ignore
    _HAS_PTA = False

_PATTERNS_PTA = {
    "engulfing":            "engulfing",
    "hammer":               "hammer",
    "shooting_star":        "shootingstar",
    "three_white_soldiers": "3whitesoldiers",
    "three_black_crows":    "3blackcrows",
    "doji":                 "doji",
    "morning_star":         "morningstar",
    "evening_star":         "eveningstar",
}


def _fallback_engulfing(df: pd.DataFrame) -> pd.Series:
    prev_body = df["close"].shift() - df["open"].shift()
    curr_body = df["close"] - df["open"]
    bullish = (
        (prev_body < 0)
        & (curr_body > 0)
        & (df["open"] < df["close"].shift())
        & (df["close"] > df["open"].shift())
    )
    bearish = (
        (prev_body > 0)
        & (curr_body < 0)
        & (df["open"] > df["close"].shift())
        & (df["close"] < df["open"].shift())
    )
    return bullish.astype(int) - bearish.astype(int)


def _fallback_hammer(df: pd.DataFrame) -> pd.Series:
    body = (df["close"] - df["open"]).abs()
    lower_shadow = df[["open", "close"]].min(axis=1) - df["low"]
    upper_shadow = df["high"] - df[["open", "close"]].max(axis=1)
    return (
        (lower_shadow >= 2 * body)
        & (upper_shadow <= body)
        & (body > 0)
    ).astype(int)


def _fallback_doji(df: pd.DataFrame) -> pd.Series:
    rng = df["high"] - df["low"]
    body = (df["close"] - df["open"]).abs()
    return (body <= 0.1 * rng.replace(0, np.nan)).astype(int)


def compute_patterns(df: pd.DataFrame) -> pd.DataFrame:
    """Return a DataFrame with one `pattern_*` column per pattern in {-1, 0, +1}."""
    if df is None or df.empty:
        return pd.DataFrame()

    df = df.rename(columns=str.lower)
    out = pd.DataFrame(index=df.index)

    if _HAS_PTA:
        for friendly, pta_name in _PATTERNS_PTA.items():
            col = f"pattern_{friendly}"
            try:
                series = pta.cdl_pattern(
                    df["open"], df["high"], df["low"], df["close"], name=pta_name
                )
                if isinstance(series, pd.DataFrame) and not series.empty:
                    series = series.iloc[:, 0]
                out[col] = np.sign(series.fillna(0)).astype(int)
            except Exception:  # noqa: BLE001
                out[col] = 0
    else:
        out["pattern_engulfing"] = _fallback_engulfing(df)
        out["pattern_hammer"] = _fallback_hammer(df)
        out["pattern_doji"] = _fallback_doji(df)

    bull = (out > 0).sum(axis=1)
    bear = (out < 0).sum(axis=1)
    out["pattern_bull_count"] = bull.astype(int)
    out["pattern_bear_count"] = bear.astype(int)
    out["pattern_net"] = (bull - bear).astype(int)

    return out


__all__ = ["compute_patterns"]
