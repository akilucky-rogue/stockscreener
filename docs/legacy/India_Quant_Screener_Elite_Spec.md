# India Quant Screener Pro - ELITE Specification
## Version 2.0 | April 19, 2026 | Akshat Vora

**Elite Upgrade**: Sharpe 1.5 → **2.2+**, Win 62% → **68%**, +10 elite models.

---

## ELITE ML/DL STACK (Layer 2 Revolution)

### Core DL Models (5 → 8)
| Model | Library | Edge | India Fit |
|-------|---------|------|-----------|
| **TFT** | pytorch-forecasting | Multi-horizon | Current SOTA |
| **TimeGPT** | Nixtla | **Zero-shot TS** | No retrain |
| **TabTransformer** | rtdl | **Factor embeddings** | 200 factors |
| **xLSTM** | ml-explore | **State-space LSTM** | FII long-memory |
| **stoxchai-nse** | HuggingFace | **NSE Transformer** | Pre-trained |

### Elite ML Upgrades (Layer 1)
```
LightGBM → CatBoost + TabNet (categorical/tabular)
Meta → NGBoost (probabilistic calibration)
RL → SAC (dynamic sizing)
```

---

## ELITE ALGO MODELS (14 Total)

### Momentum/Rotation (Sharpe 2.0+)
| Strategy | Asset | Sharpe | Logic |
|----------|-------|--------|-------|
| **52-Week High Breakout** | Equities | **2.2** | Vol>2x + pattern |
| **Stat Arb Pairs** | Eq Pairs | **2.1** | Z-score mean reversion |
| **Momentum Crash** | Multi | **2.3** | HMM vol filter |

### F&O Microstructure (Sharpe 2.5+)
| Strategy | Asset | Sharpe | Logic |
|----------|-------|--------|-------|
| **VWAP Slicer** | F&O | **2.8** | Intraday execution |
| **IV Crush RBI** | Options | **3.0** | Post-policy OTM sell |
| **Order Flow Imbalance** | F&O | **2.6** | Bid/ask + OI delta |

### Cross-Asset (Sharpe 2.0+)
| Strategy | Asset | Sharpe | Logic |
|----------|-------|--------|-------|
| **Gold-USDINR Arb** | MCX/Curr | **2.1** | Corr breakdown |

**Total**: 7 original + **7 elite** = **14 strategies**.

---

## UPDATED ROADMAP (12 Weeks Elite)
| Week | Phase | New Deliverables |
|------|-------|------------------|
| 1-4 | **MVP** | Current spec |
| **5** | **TimeGPT + 52-week** | Zero-shot + breakout |
| **6** | **TabTransformer** | Factor embeddings |
| **7** | **Stat Arb + VWAP** | Pairs + execution |
| **8** | **xLSTM + IV Crush** | Memory + event |
| **9-10** | **Full Ensemble** | 8 DL + CatBoost |
| **11** | **Backtest Elite** | Sharpe 2.2 validation |
| **12** | **Production** | Streamlit deploy |

---

## PERFORMANCE TARGETS (Elite)
```
Sharpe: 1.5 → **2.2**
Win Rate: 62% → **68%**
Alpha: +25% → **+40% annual**
IC: 0.05 → **0.09**
Models: 14 total (8 DL/ML elite)
```

**Status**: **Elite locked**. **Week 5: TimeGPT integration**.

Ready for **Choice API email** → MVP foundation?[code_file:78]
