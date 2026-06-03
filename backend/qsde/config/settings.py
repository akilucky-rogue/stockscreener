"""
QSDE centralized configuration via Pydantic BaseSettings.

Loads from .env file and environment variables. All config access
goes through the singleton `settings` object.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Optional

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


PROJECT_ROOT = Path(__file__).resolve().parents[3]  # qsde/
BACKEND_ROOT = Path(__file__).resolve().parents[2]   # qsde/backend/


class Settings(BaseSettings):
    """Application settings loaded from .env and environment."""

    model_config = SettingsConfigDict(
        env_file=str(PROJECT_ROOT / ".env"),
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ── Database ──────────────────────────────────────────────
    # Host port is 5433 (not 5432) to dodge a native Windows PostgreSQL
    # that may own 0.0.0.0:5432. docker-compose maps host 5433 -> container
    # 5432. IPv4 explicit to avoid 'localhost' resolving to ::1.
    database_url: str = "postgresql://qsde:qsde_dev_2026@127.0.0.1:5433/qsde"
    database_url_async: str = "postgresql+asyncpg://qsde:qsde_dev_2026@127.0.0.1:5433/qsde"

    # ── Redis ─────────────────────────────────────────────────
    redis_url: str = "redis://localhost:6379/0"

    # ── API Keys ──────────────────────────────────────────────
    fmp_api_key: str = ""
    finnhub_api_key: str = ""
    fred_api_key: str = ""

    # ── Choice Equity Broker ──────────────────────────────────
    choice_client_id: str = ""
    choice_api_key: str = ""

    # ── Zerodha Kite Connect (primary live-data source) ──────
    # Get these from https://developers.kite.trade after subscribing.
    # The api_secret is SECRET -- if it leaks, regenerate immediately
    # and update the .env file. Never commit either to git.
    kite_api_key: str = ""
    kite_api_secret: str = ""
    kite_redirect_url: str = "http://127.0.0.1:8000/api/kite/callback"
    # Either "kite" or "yfinance". Lets the ingestion layer fall back
    # to yfinance if a Kite outage hits or you let the subscription lapse.
    market_data_source: str = "yfinance"

    # ── Telegram ──────────────────────────────────────────────
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""

    # ── Application ───────────────────────────────────────────
    app_env: str = "local"
    log_level: str = "INFO"
    timezone: str = "Asia/Kolkata"

    # ── Data Paths ────────────────────────────────────────────
    cache_dir: Path = Field(default_factory=lambda: PROJECT_ROOT / "data" / "cache")
    model_dir: Path = Field(default_factory=lambda: PROJECT_ROOT / "data" / "models")

    # ── API Base URLs ─────────────────────────────────────────
    fmp_base_url: str = "https://financialmodelingprep.com/api/v3"
    finnhub_base_url: str = "https://finnhub.io/api/v1"
    fred_base_url: str = "https://api.stlouisfed.org/fred"
    nse_base_url: str = "https://www.nseindia.com"

    # ── Rate Limits (requests per second) ─────────────────────
    finnhub_rps: float = 1.0
    fmp_rpd: int = 250
    fred_rps: float = 10.0
    nse_rps: float = 0.5

    # ── Signal Horizons ───────────────────────────────────────
    @property
    def horizons(self) -> dict:
        return {
            "intraday": {"forward_days": 5, "rebalance": "daily"},
            "swing": {"forward_days": 20, "rebalance": "weekly"},
            "long": {"forward_days": 60, "rebalance": "monthly"},
        }

    # ── FRED Macro Series ─────────────────────────────────────
    @property
    def fred_series(self) -> dict:
        return {
            "us_10y_yield": "DGS10",
            "cboe_vix": "VIXCLS",
            "fed_funds_rate": "FEDFUNDS",
            "india_cpi": "INDCPIALLMINMEI",
            "us_unemployment": "UNRATE",
            "dxy_dollar_index": "DTWEXBGS",
            "brent_crude": "DCOILBRENTEU",
            "us_cpi": "CPIAUCSL",
            "india_10y_yield": "INDIRLTLT01STM",
        }

    # ── LightGBM Parameters ───────────────────────────────────
    @property
    def lgbm_dart_params(self) -> dict:
        """LightGBM DART (dropout regularization) parameters."""
        return {
            "objective": "regression",
            "metric": "mae",
            "boosting_type": "dart",
            "learning_rate": 0.05,
            "num_leaves": 63,
            "min_child_samples": 20,
            "feature_fraction": 0.8,
            "bagging_fraction": 0.8,
            "bagging_freq": 1,
            "lambda_l1": 0.1,
            "lambda_l2": 0.1,
            "max_depth": 7,
            "n_estimators": 500,
            "drop_rate": 0.1,
            "skip_drop": 0.5,
            "verbose": -1,
        }

    # ── Transaction Cost Surface (bps round-trip) ─────────────
    @property
    def transaction_costs(self) -> dict:
        return {
            "large_cap": {"normal": 7, "high_vol": 18},     # Nifty 50
            "mid_cap": {"normal": 16, "high_vol": 40},      # Nifty 150
            "small_cap": {"normal": 35, "high_vol": 80},    # expansion
        }


@lru_cache()
def get_settings() -> Settings:
    """Return cached settings singleton."""
    return Settings()


settings = get_settings()
