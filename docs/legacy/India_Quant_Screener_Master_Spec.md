# India Quant Screener Pro - Master Specification Document
## Version 1.0 | April 19, 2026 | Akshat Vora

**Purpose**: Production-grade, dual-profile (Trader/Investor) multi-asset screener for Indian markets (NSE/BSE F&O Currency MCX). Precision ML/DL + candlesticks + visualizations.

---

## 1. PROJECT OVERVIEW

### 1.1 Core Value Proposition
- **India-Only Multi-Asset**: Equities (Nifty 200), F&O (Nifty/BankNifty), Currency (INR pairs), Commodities (MCX Gold/Crude)
- **Dual Profiles**: Trader (high-turnover, Sharpe ≥1.5) vs Investor (low-DD, compounding)
- **Precision Edge**: 4-layer ML/DL (IC ≥0.05), candlestick confirmation (+20% win rate), factor attribution
- **Visual-First**: 15+ dynamic Plotly charts per session
- **SEBI 2026 Ready**: White-box, Algo-ID, <10 OPS

### 1.2 Target Performance (Backtest Bars)
| Metric | Trader Profile | Investor Profile |
|--------|----------------|------------------|
| Sharpe Ratio | ≥1.5 | ≥1.2 |
| Win Rate | ≥60% | ≥65% |
| Max Drawdown | -25% | -15% |
| Annual Alpha | +25-35% | +15-25% |

---

## 2. ASSET UNIVERSE & DATA

### 2.1 Markets Covered
| Exchange | Asset | Universe Size | Hours |
|----------|-------|---------------|-------|
| NSE | Equities | Nifty 200 | 9:15-15:30 |
| NSE | F&O | Nifty/BankNIFTY/FINNIFTY + 50 stocks | 9:15-15:30 |
| NSE | Currency | USD/EUR/GBP/JPY-INR | 9:00-17:00 |
| MCX | Commodities | Gold/Silver/Crude/NG/Copper (20 liquid) | 9:00-23:30 |

### 2.2 Data Sources (Primary → Fallback)
1. **Choice Equity**: NSE Equities/F&O real-time (API pending confirmation)
2. **SerNet Financial**: BSE + MCX commodities
3. **Finnhub** (key: <redacted — in .env; ROTATE>): Sentiment/news
4. **FMP** (key: <redacted — in .env; ROTATE>): Fundamentals
5. **FRED** (key: <redacted — in .env; ROTATE>): Macro
6. **yfinance**: EOD backup

### 2.3 Data Pipeline
```
Dagster DAG (daily):
1. Ingest Choice/SerNet ticks → QuestDB
2. Compute 200 factors → Feast store
3. Pattern scan (TA-Lib) → Redis
4. Model inference → Signals JSON
5. Charts → PNG gallery
6. PDF report → Email/Slack
```

---

## 3. DUAL PROFILES

### 3.1 Trader Profile (Aggressive)
- **Horizons**: Intraday (1-5d), Swing (5-20d)
- **Allocation**: Equities 50%, F&O 40%, Currency 10%
- **Risk**: Max DD -25%, per-trade 5%
- **Turnover**: 15-25x
- **Focus**: Momentum, flow, vol arb

### 3.2 Investor Profile (Conservative)
- **Horizons**: Swing (20d+), Long (1-12mo)
- **Allocation**: Equities 60%, Commodities 20%, Currency 10%, Cash 10%
- **Risk**: Max DD -15%, per-stock 3%
- **Turnover**: 2-4x
- **Focus**: Quality, value, rebalancing

---

## 4. FACTOR ENGINE (200 Factors)

### 4.1 Factor Categories (Top 50 MVP)
| Category | Count | Top Examples |
|----------|-------|-------------|
| Fundamental | 38 | EV/EBITDA, ROIC>15%, FCF yield |
| Technical | 40 | VWAP dev, ATR regime, SuperTrend |
| Macro | 20 | RBI repo, FII/DII flows, monsoon |
| Sentiment | 18 | OI skew, bulk deals, news tone |
| Flow | 18 | Delivery%>85%, PCR extremes |
| Risk | 15 | India VIX, beta, VaR 95% |

**Pruning**: SHAP >0.02 + IC >0.03

---

## 5. ML/DL ARCHITECTURE (4 Layers)

### 5.1 Layer 1: LightGBM Rankers
- 3 models/horizon (direction + return)
- LambdaRank objective
- Optuna tuning (200 trials)

### 5.2 Layer 2: Temporal Fusion Transformer
- PyTorch Forecasting TFT
- Multi-horizon heads (1d/5d/20d/60d)
- Quantile loss (q=0.1,0.5,0.9)

### 5.3 Layer 3: Meta-Ensemble
- Logistic NN on base scores + regime
- Conformal prediction bands

### 5.4 Layer 4: Policy Layer
- Kelly sizing (quarter-Kelly)
- Profile-adjusted thresholds

**Target**: IC 0.05+, Precision(BUY) 65%+

---

## 6. CANDLESTICK PATTERNS (TA-Lib 60+)

### 6.1 Top 8 Precision Patterns (India Backtested)
| Pattern | Win Rate | Context | TA-Lib |
|---------|----------|---------|--------|
| Bullish Engulfing | 64% | Downtrend | CDLENGULFING |
| Hammer | 60% | Support | CDLHAMMER |
| Three Soldiers | 62% | Pullback | CDL3WHITESOLDIERS |

**Filter**: Volume>1.5x + regime match + model confirm

---

## 7. ALGO TRADING MODELS (7 High-Value)

| Model | Asset | Sharpe | Win Rate | Hold Time |
|-------|-------|--------|----------|-----------|
| FII Momentum | Equities | 1.4 | 61% | 5d |
| OI Reversal | F&O | 1.8 | 64% | 1-3d |
| Delivery Breakout | Equities | 1.2 | 60% | 3d |
| Portfolio Rebalancer | Multi | 2.0 | 65% | Monthly |

**SEBI 2026**: White-box, Algo-ID, audit trail [web:26]

---

## 8. VISUALIZATION SYSTEM (15+ Charts)

### 8.1 Per-Signal (3 Charts)
1. **Price + Patterns** (Plotly candlestick)
2. **SHAP Waterfall** (Factor attribution)
3. **Multi-Horizon Forecast** (TFT bands)

### 8.2 Dashboard Charts
- P&L Heatmap, Sharpe Evolution, Allocation Pie
- Regime HMM Pie, Model Matrix Heatmap

### 8.3 Exports
- EOD PDF (15 pages)
- Pattern Scanner Map
- Weekly Model Review Deck

---

## 9. TECHNOLOGY STACK

| Layer | Tech |
|-------|------|
| Data | QuestDB (ticks), Feast (features), Redis (live) |
| ML | LightGBM, PyTorch Forecasting, MLflow |
| Patterns | TA-Lib (pandas-ta wrapper) |
| Orchestration | Dagster |
| UI | Streamlit + Plotly (perplexity theme) |
| Export | WeasyPrint PDF |

---

## 10. PHASED RESEARCH ROADMAP (8 Weeks Pure Planning)

| Week | Phase | Deliverable |
|------|-------|-------------|
| 1 | Data Spec | Broker API map, universe list |
| 2 | Factor Ranking | Top-50 w/ IC evidence |
| 3 | MVP Models | LightGBM + top patterns |
| 4 | Profiles | Trader/Investor logic |
| 5 | Algo Models | 7 strategies backtest spec |
| 6 | Visuals | 15 chart blueprints |
| 7 | SEBI Compliance | Algo-ID + audit plan |
| 8 | Full Blueprint | Production doc freeze |

---

## 11. VALIDATION PROTOCOL

### 11.1 Backtesting
- **Walk-Forward**: 756d train / 252d test
- **CPCV**: 6-fold, PBO ≤0.30
- **Regimes**: Bull/Bear/Sideways split

### 11.2 Live Validation
- 90d paper trading (Choice sandbox)
- Sharpe ≥0.8 mandatory for live

---

## 12. DEPLOYMENT & WORKFLOW

### 12.1 Daily Trader Workflow
```
9:00 → Data sync (Choice/SerNet)
9:15 → Screener + signals (15min charts)
9:30 → Execute top-5 (F&O focus)
15:30 → EOD report PDF
```

### 12.2 Weekly Investor Workflow
```
Mon EOD → Rebalance signals
Fri → Model review + pattern IC
Monthly → Full portfolio audit
```

---

## 13. RISKS & MITIGATION

| Risk | Mitigation |
|------|------------|
| Broker API Limits | Multi-source fallback |
| Model Decay | Weekly IC monitoring |
| SEBI Changes | White-box design |
| Data Gaps | yfinance backup |

---

**Status**: Research-Ready. Next: Week 1 Broker API confirmation.

**Reviewers**: Claude/Codex - Flag gaps, suggest refinements.
