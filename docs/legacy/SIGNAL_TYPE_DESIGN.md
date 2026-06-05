# Signal Type Design — Hybrid Ranking + Classification

**Version**: 1.0
**Date**: 2026-03-25
**Author**: Akshat
**Status**: Formalized Specification

---

## 1. Signal Architecture Overview

The engine produces signals through a **three-layer pipeline**:

```
Layer 1: RANKING (Internal Engine)
    LightGBM LambdaRank model
    Input:  150 factors per stock per date
    Output: Continuous score 0-100 (percentile within universe)

Layer 2: CLASSIFICATION (User-Facing)
    Rule-based mapping from ranking score
    Input:  Ranking score 0-100
    Output: STRONG_BUY / BUY / HOLD / SELL / STRONG_SELL

Layer 3: REGRESSION (Supplement)
    LightGBM regressor
    Input:  Same 150 factors
    Output: Predicted N-day return (for position sizing and risk)
```

This hybrid design gives us:
- **Ranking** for robustness to market regimes (the core model)
- **Classification** for actionable output (what users see)
- **Regression** for magnitude estimation (position sizing, risk management)

---

## 2. Layer 1: Ranking Model (Core)

### 2.1 Why Ranking?

| Property | Classification | Regression | Ranking |
|----------|---------------|------------|---------|
| Robust to bull/bear shifts | No (thresholds break) | No (returns shift) | **Yes** (relative order stable) |
| Natural screener output | Forced (arbitrary thresholds) | Must rank post-hoc | **Native** (stocks ordered by score) |
| Standard quant metric | Accuracy (misleading) | R² (always low) | **IC** (gold standard) |
| Handles non-stationarity | Poorly | Poorly | **Well** (cross-sectional) |
| Training signal quality | Noisy (threshold-dependent) | Noisy (fat tails) | **Clean** (pairwise ordering) |

### 2.2 Target Variable

For each date `t` and horizon `h`, the target is the **cross-sectional percentile rank** of forward returns:

```
target(stock_i, date_t, horizon_h) = percentile_rank(
    forward_return(stock_i, t, t+h),
    among all stocks in universe on date t
)
```

Where:
- `forward_return(stock_i, t, t+h) = (price[t+h] - price[t]) / price[t]`
- `percentile_rank` maps to [0, 1]: 0 = worst performer, 1 = best performer

### 2.3 Horizons and Target Definitions

| Horizon | Label | Forward Return Window | Rebalance Frequency | Universe |
|---------|-------|----------------------|---------------------|----------|
| Intraday | `intraday` | 5 trading days | Daily | Nifty 200 + S&P 100 |
| Swing | `swing` | 20 trading days | Weekly | Nifty 200 + S&P 100 |
| Long-term | `long` | 60 trading days | Monthly | Nifty 200 + S&P 100 |

### 2.4 Model: LightGBM LambdaRank

```python
import lightgbm as lgb

params = {
    'objective': 'lambdarank',
    'metric': 'ndcg',
    'ndcg_eval_at': [10, 50],          # Optimize top-10 and top-50 ranking
    'learning_rate': 0.05,
    'num_leaves': 63,
    'min_child_samples': 20,
    'feature_fraction': 0.8,           # Use 80% of factors per tree
    'bagging_fraction': 0.8,           # Use 80% of data per tree
    'bagging_freq': 1,
    'lambda_l1': 0.1,
    'lambda_l2': 0.1,
    'max_depth': 7,
    'n_estimators': 500,
    'early_stopping_rounds': 50,
    'verbose': -1,
}
```

**Training data format**: Each date is a "query group" containing all stocks in the universe. The model learns to order stocks within each date by future return rank.

```python
# Training structure
# X: (n_dates × n_stocks, n_factors) feature matrix
# y: (n_dates × n_stocks,) target percentile ranks
# group: [n_stocks, n_stocks, ...] — number of stocks per date

train_data = lgb.Dataset(
    X_train,
    label=y_train,
    group=group_train,
    feature_name=factor_names,
)
```

### 2.5 Feature Engineering for the Ranking Model

Raw factors are transformed before model input:

| Transform | What | Why |
|-----------|------|-----|
| **Cross-sectional rank** | Rank each factor across stocks on same date → [0, 1] | Removes scale differences, handles outliers |
| **Z-score (within date)** | (value - mean) / std across stocks on same date | Alternative to rank for continuous factors |
| **Lag features** | Factor value at t-5, t-20 | Capture factor momentum |
| **Factor change** | Factor[t] - Factor[t-20] | Rate of change signals |
| **Interaction (sparingly)** | RSI × Volume_ratio, P/E × EPS_growth | Only 5-10 hand-picked interactions |

**Feature matrix per stock per date**: ~200-250 columns (150 raw + lags + changes + interactions).

### 2.6 Ranking Model Output

```python
# Inference: score every stock in universe on date t
raw_scores = model.predict(X_today)                     # Raw LightGBM scores
percentile_scores = rank_percentile(raw_scores) * 100   # 0-100 scale
```

The output is a **daily cross-sectional ranking** of all stocks, scored 0-100.

---

## 3. Layer 2: Classification (User-Facing Labels)

### 3.1 Score-to-Label Mapping

| Percentile Range | Label | Color | Description |
|-----------------|-------|-------|-------------|
| 90-100 (top decile) | **STRONG_BUY** | Deep green | Top 10% of universe by predicted performance |
| 70-90 | **BUY** | Green | Above-average expected performance |
| 30-70 | **HOLD** | Yellow/neutral | Average expected performance |
| 10-30 | **SELL** | Orange/red | Below-average expected performance |
| 0-10 (bottom decile) | **STRONG_SELL** | Deep red | Bottom 10% of universe |

### 3.2 Backward Compatibility with Existing -14 to +14 System

The current AlgoTrader_Finnhub.jsx uses a 7-indicator consensus scored -14 to +14:

```
Current system:          New system:
Score ≥ +10  → STRONG BUY    Percentile 90-100 → STRONG_BUY
Score +4 to +9  → BUY        Percentile 70-90  → BUY
Score -3 to +3  → HOLD       Percentile 30-70  → HOLD
Score -9 to -4  → SELL       Percentile 10-30  → SELL
Score ≤ -10 → STRONG SELL    Percentile 0-10   → STRONG_SELL
```

**Migration strategy**: During transition, show both scores side-by-side. The old rule-based score becomes one of 150 input factors to the new ML model (factor name: `legacy_consensus_score`).

### 3.3 Confidence Score

Confidence is derived from **model agreement and factor alignment**:

```python
def compute_confidence(percentile_score, factor_values, model):
    """0-1 confidence score for the signal."""

    # Component 1: Distance from decision boundary (0.4 weight)
    # Stocks near 30/70 thresholds have low confidence
    boundary_dist = min(
        abs(percentile_score - 30),
        abs(percentile_score - 70),
        abs(percentile_score - 10),
        abs(percentile_score - 90),
    ) / 50  # normalize to 0-1
    boundary_confidence = min(boundary_dist, 1.0)

    # Component 2: Factor agreement (0.3 weight)
    # What % of top-10 important factors agree with the signal direction?
    top_factors = get_top_shap_factors(model, factor_values, n=10)
    agreeing = sum(1 for f in top_factors if f.direction == signal_direction)
    factor_agreement = agreeing / 10

    # Component 3: Historical IC stability (0.3 weight)
    # Is the model performing well recently?
    recent_ic = get_rolling_ic(last_20_days)
    ic_confidence = min(max(recent_ic / 0.05, 0), 1)  # IC=0.05 → full confidence

    confidence = (
        0.4 * boundary_confidence +
        0.3 * factor_agreement +
        0.3 * ic_confidence
    )
    return round(confidence, 3)
```

### 3.4 Risk Score

Risk score is independent of signal direction — it measures **how risky this stock is right now**:

```python
def compute_risk_score(factor_values):
    """0-1 risk score. Higher = more risky."""

    components = {
        'volatility':    normalize(factor_values['realized_vol_20d'], 0, 80),
        'drawdown':      normalize(abs(factor_values['max_dd_60d']), 0, 40),
        'vix_exposure':  normalize(factor_values['beta_to_benchmark'], 0, 2.5),
        'tail_risk':     normalize(factor_values['rolling_kurtosis'], 3, 15),
        'vol_regime':    1.0 if factor_values['vol_regime_flag'] > 1.2 else 0.3,
    }

    risk_score = (
        0.30 * components['volatility'] +
        0.25 * components['drawdown'] +
        0.20 * components['vix_exposure'] +
        0.15 * components['tail_risk'] +
        0.10 * components['vol_regime']
    )
    return round(min(risk_score, 1.0), 3)
```

---

## 4. Layer 3: Regression Supplement

### 4.1 Purpose

The regression model predicts **actual forward returns** (not just rank). This is used for:
- **Position sizing**: Larger predicted return → larger allocation
- **Risk-adjusted ranking**: Combine predicted return with risk score
- **Threshold calibration**: "Is the top-decile stock expected to return 5% or 0.5%?"

### 4.2 Model

```python
params_regressor = {
    'objective': 'regression',
    'metric': 'mae',
    'learning_rate': 0.03,
    'num_leaves': 31,
    'min_child_samples': 50,         # More conservative than ranker
    'feature_fraction': 0.7,
    'bagging_fraction': 0.7,
    'bagging_freq': 1,
    'lambda_l1': 0.5,               # Stronger regularization
    'lambda_l2': 0.5,
    'max_depth': 5,                  # Shallower trees (returns are noisy)
    'n_estimators': 300,
    'early_stopping_rounds': 30,
}
```

**Target**: Raw forward N-day return (not percentile). Winsorized at 1st/99th percentile to reduce outlier impact.

### 4.3 Output

```python
predicted_return = regressor.predict(X_today)  # e.g., 0.032 = +3.2% expected
```

This is shown alongside the signal:
```
RELIANCE.NS | STRONG_BUY | Score: 94 | Confidence: 0.82 | Risk: 0.35 | Expected: +4.1% (20d)
```

---

## 5. Signal Output Schema

### 5.1 Per-Stock Signal Record

```json
{
    "symbol": "RELIANCE.NS",
    "date": "2026-03-25",
    "horizon": "swing",
    "ranking_score": 94.2,
    "label": "STRONG_BUY",
    "confidence": 0.82,
    "risk_score": 0.35,
    "predicted_return_pct": 4.1,
    "top_factors": [
        {"name": "fii_cumulative_20d", "value": 12500, "direction": "bullish", "shap": 0.034},
        {"name": "earnings_surprise_pct", "value": 8.2, "direction": "bullish", "shap": 0.028},
        {"name": "rsi_14", "value": 42, "direction": "bullish", "shap": 0.022},
        {"name": "price_sma50_pct", "value": -3.1, "direction": "bullish", "shap": 0.019},
        {"name": "india_vix_5d_change", "value": -2.3, "direction": "bullish", "shap": 0.015}
    ],
    "legacy_consensus_score": 8,
    "model_version": "v1.0.0_swing_20260320"
}
```

### 5.2 Universe Summary

```json
{
    "date": "2026-03-25",
    "horizon": "swing",
    "universe_size": 300,
    "distribution": {
        "STRONG_BUY": 30,
        "BUY": 60,
        "HOLD": 120,
        "SELL": 60,
        "STRONG_SELL": 30
    },
    "model_health": {
        "rolling_20d_ic": 0.042,
        "rolling_20d_ic_ir": 0.28,
        "last_retrain": "2026-03-01",
        "data_freshness": "2026-03-25T17:30:00+05:30"
    }
}
```

---

## 6. Training and Retraining Protocol

### 6.1 Initial Training

```
Universe:     Nifty 200 (India) + S&P 100 (US) = ~300 stocks
History:      2016-01-01 to 2025-12-31 (10 years)
Train split:  Walk-forward, 5 folds (see Architecture doc)
Validation:   Last fold's test set (2025)
```

### 6.2 Monthly Retraining Schedule

```
1st of each month:
    1. Expand training window to include last month's data
    2. Retrain ranking model (LightGBM LambdaRank)
    3. Retrain regression model (LightGBM regressor)
    4. Evaluate on recent 20-day IC
    5. If IC drops below 0.01 for 3 consecutive months → trigger investigation
    6. Log model version, params, metrics to model_runs table
    7. Deploy new model for daily signal generation
```

### 6.3 Model Versioning

```
Format: v{major}.{minor}.{patch}_{horizon}_{train_end_date}

Examples:
    v1.0.0_swing_20260301      # First production model, swing horizon
    v1.1.0_swing_20260401      # Monthly retrain
    v1.2.0_swing_20260501      # Monthly retrain
    v2.0.0_swing_20260601      # Major change (new factors, architecture)
```

---

## 7. Evaluation Framework

### 7.1 Primary Metrics (Ranking)

| Metric | Formula | What It Measures |
|--------|---------|-----------------|
| **Spearman IC** | `corr(predicted_rank, actual_rank)` per date | Daily ranking quality |
| **IC_IR** | `mean(IC) / std(IC)` over rolling window | Consistency of ranking quality |
| **NDCG@10** | Normalized discounted cumulative gain for top 10 | Are the best stocks ranked highest? |
| **Top-quintile return** | Mean return of stocks in top 20% by score | Does buying the top work? |
| **Long-short spread** | Top quintile return - Bottom quintile return | Signal profitability |

### 7.2 Secondary Metrics (Classification)

| Metric | How Computed | Target |
|--------|-------------|--------|
| Directional accuracy | % of BUY signals where actual return > 0 | > 52% (intraday), > 55% (long) |
| Precision (STRONG_BUY) | % of STRONG_BUY where actual top-quintile | > 40% |
| Label confusion matrix | 5×5 matrix of predicted vs actual quintile | Diagonal-heavy |

### 7.3 Portfolio Metrics (Trading)

| Metric | Formula | Target |
|--------|---------|--------|
| Annualized Sharpe | `mean(daily_return) / std(daily_return) × √252` | > 0.8 |
| Annualized Sortino | `mean(daily_return) / downside_std × √252` | > 1.0 |
| Max drawdown | Largest peak-to-trough decline | < 25% |
| Calmar ratio | `annualized_return / max_drawdown` | > 0.5 |
| Win rate | % of trades with positive return | > 50% |
| Profit factor | `gross_profit / gross_loss` | > 1.2 |
| Monthly turnover | % of portfolio changed per month | < 100% (swing) |

### 7.4 Factor-Level Analysis

For each of the 150 factors:
- **Univariate IC**: How predictive is this factor alone?
- **IC decay**: How quickly does predictive power fade? (1d, 5d, 20d, 60d)
- **Turnover**: How often does the factor signal change?
- **SHAP importance**: How much does this factor contribute in the ensemble model?
- **Regime stability**: Is IC positive in both bull and bear markets?

Factors with IC < 0.005 univariate AND low SHAP importance are candidates for removal.

---

## 8. Risk Management Integration

### 8.1 Position Sizing Rules

```python
def compute_position_size(signal, portfolio_value, max_positions=20):
    """Risk-adjusted position sizing."""

    base_weight = 1.0 / max_positions  # Equal weight = 5% each

    # Scale by confidence
    confidence_multiplier = 0.5 + signal.confidence  # Range: 0.5 to 1.5

    # Scale inversely by risk
    risk_multiplier = 1.5 - signal.risk_score  # Range: 0.5 to 1.5

    # Scale by predicted return magnitude (regression layer)
    return_multiplier = min(max(signal.predicted_return_pct / 3.0, 0.5), 1.5)

    weight = base_weight * confidence_multiplier * risk_multiplier * return_multiplier

    # Hard caps
    weight = min(weight, 0.10)       # Never more than 10% in one stock
    weight = max(weight, 0.02)       # Never less than 2% if in portfolio

    return round(weight * portfolio_value, 2)
```

### 8.2 Exposure Limits

| Limit | Value | Enforced By |
|-------|-------|-------------|
| Max single stock | 10% of portfolio | Position sizing |
| Max sector | 30% of portfolio | Post-signal filter |
| Max India exposure | 70% of portfolio | Geography filter |
| Max US exposure | 70% of portfolio | Geography filter |
| Max STRONG_BUY positions | 10 stocks | Top-N filter |
| Max total positions | 20 stocks | Portfolio construction |
| Min holding period | 5 days (swing) | Rebalance frequency lock |

### 8.3 Stop-Loss and Exit Rules

| Rule | Trigger | Action |
|------|---------|--------|
| Hard stop | Position drops > 2 × ATR(14) | Exit immediately |
| Signal reversal | Label changes to SELL or STRONG_SELL | Exit at next rebalance |
| Confidence collapse | Confidence drops below 0.3 | Reduce to half position |
| Time exit | Held for 2× horizon without rebalance signal | Review and exit |

---

## 9. User-Facing Signal Display

### 9.1 Signal Card (Dashboard)

```
┌─────────────────────────────────────────────────────────┐
│  RELIANCE.NS                          Score: 94 / 100   │
│  Reliance Industries                  ████████████████░░ │
│                                                          │
│  ┌──────────┐  Confidence: ████████░░ 82%               │
│  │ STRONG   │  Risk:       ███░░░░░░░ 35%               │
│  │  BUY     │  Expected:   +4.1% (20d)                  │
│  └──────────┘                                           │
│                                                          │
│  Why this signal:                                        │
│  ↑ FII buying strongly (₹12,500 Cr net in 20d)         │
│  ↑ Earnings beat estimates by 8.2%                       │
│  ↑ RSI at 42 (room to run, not overbought)              │
│  ↑ Price 3.1% below SMA50 (mean reversion setup)        │
│  ↑ India VIX declining (risk appetite improving)         │
│                                                          │
│  Legacy score: +8 / 14 (BUY)                            │
│  Model: v1.0.0_swing_20260320                            │
└─────────────────────────────────────────────────────────┘
```

### 9.2 Universe Screener View

```
┌────────────────────────────────────────────────────────────────┐
│  SIGNAL SCREENER  │ Horizon: Swing (20d) │ Date: 2026-03-25   │
├────────┬──────────┬───────┬──────┬──────┬─────────┬───────────┤
│ Symbol │ Label    │ Score │ Conf │ Risk │ Exp Ret │ Top Factor│
├────────┼──────────┼───────┼──────┼──────┼─────────┼───────────┤
│ INFY   │ STR BUY  │  97   │ 0.91 │ 0.22 │  +5.8%  │ EPS beat  │
│ RELI.. │ STR BUY  │  94   │ 0.82 │ 0.35 │  +4.1%  │ FII flow  │
│ TCS    │ BUY      │  78   │ 0.65 │ 0.28 │  +2.4%  │ ROE high  │
│ HDFC.. │ BUY      │  72   │ 0.58 │ 0.41 │  +1.9%  │ P/B low   │
│ ...    │          │       │      │      │         │           │
│ ADANI  │ SELL     │  18   │ 0.71 │ 0.78 │  -2.3%  │ D/E high  │
│ COALIN │ STR SELL │   4   │ 0.85 │ 0.82 │  -4.7%  │ Vol spike │
└────────┴──────────┴───────┴──────┴──────┴─────────┴───────────┘
│ Universe: 300 stocks │ Model IC (20d): 0.042 │ Freshness: 17:30│
└────────────────────────────────────────────────────────────────┘
```

---

## 10. Implementation Sequence

| Step | What | Depends On | Output |
|------|------|-----------|--------|
| 1 | Build target variable computation | Raw price data | `target_percentile_rank` per stock per date |
| 2 | Build feature matrix pipeline | Factor engine (Phase B in Architecture doc) | `(n_dates × n_stocks, n_factors)` matrix |
| 3 | Train LightGBM ranker (single fold) | Steps 1-2 | Trained model, IC on validation set |
| 4 | Walk-forward backtest (5+ folds) | Step 3 | IC, Sharpe, quintile spreads per fold |
| 5 | Add classification layer | Step 3 | Label mapping + confidence + risk score |
| 6 | Add regression supplement | Steps 1-2 | Predicted returns model |
| 7 | Add SHAP explanations | Step 3 | Top-5 factor explanations per signal |
| 8 | Build FastAPI endpoints | Steps 5-7 | `/api/signals`, `/api/factors` |
| 9 | Build React signal dashboard | Step 8 | Signal cards, screener table |
| 10 | Monthly retrain automation | Step 4 | Scheduled model updates |

---

## 11. Summary of Decisions

| Decision | Choice |
|----------|--------|
| Primary model type | **Ranking** (LightGBM LambdaRank) |
| User-facing output | **5-label classification** mapped from ranking percentile |
| Supplementary model | **Regression** (predicted return) for position sizing |
| Target variable | Cross-sectional percentile rank of forward N-day return |
| Horizons | Intraday (5d), Swing (20d), Long (60d) |
| Universe | Nifty 200 + S&P 100 (~300 stocks MVP) |
| Rebalance | Daily (intraday), Weekly (swing), Monthly (long) |
| Retraining | Monthly expanding-window retrain |
| Confidence | 3-component: boundary distance + factor agreement + IC stability |
| Risk score | 5-component: vol + drawdown + beta + tail risk + vol regime |
| Backward compat | Legacy -14/+14 score becomes an input factor |
| Explainability | SHAP top-5 factors per signal |
| Position sizing | Confidence × inverse risk × predicted return, capped at 10% |
