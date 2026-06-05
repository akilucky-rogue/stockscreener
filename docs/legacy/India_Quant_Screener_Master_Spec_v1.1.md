# India Quant Screener Pro - Master Specification Document
## Version 1.1 | April 19, 2026 | Akshat Vora

**Purpose**: Production-grade, dual-profile (Trader/Investor) multi-asset screener for Indian markets. Precision ML/DL + candlesticks + visualizations + SEBI compliance.

---

## 0. REQUIREMENTS & PREREQUISITES

### 0.1 Hardware Requirements
| Component | Minimum | Recommended |
|-----------|---------|-------------|
| **CPU** | 8 cores | i9/AMD 16+ cores |
| **RAM** | 32GB | 64GB |
| **GPU** | NVIDIA RTX 3060 (8GB) | RTX 4070+ (12GB+) |
| **Storage** | 1TB SSD | 2TB NVMe |
| **Internet** | 100Mbps | 1Gbps (Choice WS) |

### 0.2 Software Requirements
```
Python 3.11+
Docker 24+
CUDA 12.1 (GPU training)
Static IP (SEBI broker APIs)
```

### 0.3 API Keys & Accounts (Critical)
| Service | Key Required | Status |
|---------|--------------|--------|
| Choice Equity | API Key/Token | ⏳ Confirm endpoints |
| SerNet Financial | API Key | ⏳ MCX coverage? |
| Finnhub | `<redacted — in .env; ROTATE>` | ✅ Active |
| FMP | `<redacted — in .env; ROTATE>` | ✅ Active |
| FRED | `<redacted — in .env; ROTATE>` | ✅ Active |

### 0.4 Broker Accounts
- Choice Equity: NSE Equities/F&O primary
- SerNet: BSE/MCX backup
- **Action**: Confirm REST/WS APIs + historical depth

### 0.5 Python Dependencies (requirements.txt)
```
lightgbm==4.3.0
pytorch-forecasting==1.1.0
pandas-ta==0.3.14b
ta-lib==0.4.28
plotly==5.22.0
questdb==0.1.0
feast==0.38.1
dagster==1.7.8
mlflow==2.12.1
streamlit==1.38.0
weasyprint==62.3
nseindia-api==0.2.1
yfinance==0.2.40
```

---

## 1. PROJECT OVERVIEW

[... rest of document remains identical to v1.0 ...]

