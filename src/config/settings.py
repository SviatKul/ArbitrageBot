"""Application settings loaded from environment / .env via pydantic-settings."""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

_PROJECT_ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    """Central configuration for the arbitrage bot."""

    model_config = SettingsConfigDict(
        env_file=str(_PROJECT_ROOT / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    dry_run: bool = Field(default=True, validation_alias="DRY_RUN")
    log_level: str = Field(default="INFO", validation_alias="LOG_LEVEL")

    http_timeout_seconds: float = Field(default=30.0, validation_alias="HTTP_TIMEOUT_SECONDS")
    retry_max_attempts: int = Field(default=5, ge=1, validation_alias="RETRY_MAX_ATTEMPTS")
    retry_wait_min_seconds: float = Field(default=1.0, ge=0.0, validation_alias="RETRY_WAIT_MIN_SECONDS")
    retry_wait_max_seconds: float = Field(default=30.0, ge=0.0, validation_alias="RETRY_WAIT_MAX_SECONDS")

    kalshi_base_url: str = Field(
        default="https://api.elections.kalshi.com/trade-api/v2",
        validation_alias="KALSHI_BASE_URL",
    )
    kalshi_api_key_id: Optional[str] = Field(default=None, validation_alias="KALSHI_API_KEY_ID")
    kalshi_private_key_path: Optional[str] = Field(default=None, validation_alias="KALSHI_PRIVATE_KEY_PATH")
    kalshi_private_key_pem: Optional[str] = Field(default=None, validation_alias="KALSHI_PRIVATE_KEY_PEM")

    polymarket_clob_host: str = Field(
        default="https://clob.polymarket.com",
        validation_alias="POLYMARKET_CLOB_HOST",
    )

    market_sample_limit: int = Field(default=10, ge=1, le=1000, validation_alias="MARKET_SAMPLE_LIMIT")

    min_profit_percent: float = Field(
        default=0.05,
        ge=0.0,
        description="Minimum profit_percent (see ArbitrageDetector) to emit an opportunity.",
        validation_alias="MIN_PROFIT_PERCENT",
    )
    min_leg_liquidity: float = Field(
        default=1.0,
        ge=0.0,
        description="Each leg's best-ask size must be strictly greater than this (same units as quote sizes).",
        validation_alias="MIN_LEG_LIQUIDITY",
    )
    kalshi_taker_fee_rate: float = Field(
        default=0.07,
        ge=0.0,
        description="Kalshi taker fee as a fraction of notional (0.07 = 7%).",
        validation_alias="KALSHI_TAKER_FEE_RATE",
    )
    polymarket_taker_fee_rate: float = Field(
        default=0.02,
        ge=0.0,
        description="Polymarket taker fee fraction (0.02 = 2%).",
        validation_alias="POLYMARKET_TAKER_FEE_RATE",
    )

    max_order_contracts: int = Field(
        default=100,
        ge=1,
        description="Safety cap per arb leg (contracts).",
        validation_alias="MAX_ORDER_CONTRACTS",
    )
    order_fill_timeout_seconds: float = Field(
        default=30.0,
        ge=1.0,
        description="Wall-clock wait for full fills before cancelling resting quantity.",
        validation_alias="ORDER_FILL_TIMEOUT_SECONDS",
    )
    execution_poll_interval_seconds: float = Field(
        default=0.25,
        ge=0.05,
        description="Delay between order-status polls while waiting for fills.",
        validation_alias="EXECUTION_POLL_INTERVAL_SECONDS",
    )

    poll_interval_seconds: float = Field(
        default=60.0,
        ge=1.0,
        description="Main bot loop sleep between iterations.",
        validation_alias="POLL_INTERVAL_SECONDS",
    )
    circuit_breaker_wait_seconds: float = Field(
        default=30.0,
        ge=1.0,
        description="Sleep when circuit breaker blocks trading.",
        validation_alias="CIRCUIT_BREAKER_WAIT_SECONDS",
    )
    kalshi_markets_limit: int = Field(
        default=500,
        ge=1,
        le=1000,
        description="Max Kalshi markets per GET /markets page for the main loop.",
        validation_alias="KALSHI_MARKETS_LIMIT",
    )
    polymarket_markets_max_pages: int = Field(
        default=1,
        ge=1,
        le=50,
        description="How many CLOB /markets pages to concatenate per loop iteration.",
        validation_alias="POLYMARKET_MARKETS_MAX_PAGES",
    )
    positions_snapshot_path: str = Field(
        default="data/positions.json",
        validation_alias="POSITIONS_SNAPSHOT_PATH",
    )
    log_directory: str = Field(
        default="logs",
        validation_alias="LOG_DIRECTORY",
    )
    log_retention_days: int = Field(
        default=14,
        ge=1,
        validation_alias="LOG_RETENTION_DAYS",
    )
    market_match_min_fuzzy_score: float = Field(
        default=65.0,
        ge=0.0,
        le=100.0,
        description="Minimum rapidfuzz token_set score for Polymarket↔Kalshi title pairing.",
        validation_alias="MARKET_MATCH_MIN_FUZZY_SCORE",
    )

    dashboard_html_path: str = Field(
        default="data/dashboard.html",
        validation_alias="DASHBOARD_HTML_PATH",
    )
    dashboard_flask_enabled: bool = Field(default=False, validation_alias="DASHBOARD_FLASK_ENABLED")
    dashboard_flask_host: str = Field(default="127.0.0.1", validation_alias="DASHBOARD_FLASK_HOST")
    dashboard_flask_port: int = Field(default=8765, ge=1, le=65535, validation_alias="DASHBOARD_FLASK_PORT")

    # --- Betfair ---
    betfair_username: Optional[str] = Field(default=None, validation_alias="BETFAIR_USERNAME")
    betfair_password: Optional[str] = Field(default=None, validation_alias="BETFAIR_PASSWORD")
    betfair_app_key: Optional[str] = Field(default=None, validation_alias="BETFAIR_APP_KEY")
    betfair_commission: float = Field(
        default=0.05, ge=0.0, le=1.0,
        description="Betfair commission on net winnings (default 5%).",
        validation_alias="BETFAIR_COMMISSION",
    )
    betfair_markets_limit: int = Field(
        default=200, ge=1, le=1000,
        validation_alias="BETFAIR_MARKETS_LIMIT",
    )

    # --- Smarkets ---
    smarkets_api_token: Optional[str] = Field(default=None, validation_alias="SMARKETS_API_TOKEN")
    smarkets_commission: float = Field(
        default=0.02, ge=0.0, le=1.0,
        description="Smarkets commission rate (default 2%).",
        validation_alias="SMARKETS_COMMISSION",
    )

    # --- Betdaq ---
    betdaq_username: Optional[str] = Field(default=None, validation_alias="BETDAQ_USERNAME")
    betdaq_password: Optional[str] = Field(default=None, validation_alias="BETDAQ_PASSWORD")
    betdaq_api_key: Optional[str] = Field(default=None, validation_alias="BETDAQ_API_KEY")
    betdaq_commission: float = Field(
        default=0.02, ge=0.0, le=1.0,
        description="Betdaq commission on net winnings (default 2%).",
        validation_alias="BETDAQ_COMMISSION",
    )

    # --- Matchbook ---
    matchbook_username: Optional[str] = Field(default=None, validation_alias="MATCHBOOK_USERNAME")
    matchbook_password: Optional[str] = Field(default=None, validation_alias="MATCHBOOK_PASSWORD")
    matchbook_commission: float = Field(
        default=0.015, ge=0.0, le=1.0,
        description="Matchbook commission on net winnings (default 1.5%).",
        validation_alias="MATCHBOOK_COMMISSION",
    )

    telegram_bot_token: Optional[str] = Field(default=None, validation_alias="TELEGRAM_BOT_TOKEN")
    telegram_chat_id: Optional[str] = Field(default=None, validation_alias="TELEGRAM_CHAT_ID")
    telegram_daily_report_hour_utc: int = Field(
        default=22,
        ge=0,
        le=23,
        description="Earliest UTC hour for the once-per-day PnL Telegram digest.",
        validation_alias="TELEGRAM_DAILY_REPORT_HOUR_UTC",
    )

    auto_close_stop_loss_per_contract: float = Field(
        default=0.08,
        ge=0.0,
        description="Close when adverse move per contract exceeds this (probability points).",
        validation_alias="AUTO_CLOSE_STOP_LOSS_PER_CONTRACT",
    )
    auto_close_take_profit_fraction_of_peak: float = Field(
        default=0.5,
        ge=0.05,
        le=1.0,
        description="Trailing take-profit: close if unrealized < fraction × peak unrealized (after peak armed).",
        validation_alias="AUTO_CLOSE_TP_FRACTION_OF_PEAK",
    )
    auto_close_take_profit_fraction_of_theoretical: float = Field(
        default=0.5,
        ge=0.05,
        le=1.0,
        description="Fixed take-profit: close when unrealized ≥ fraction × max move to $1 per contract bundle.",
        validation_alias="AUTO_CLOSE_TP_FRACTION_OF_THEORETICAL",
    )
    auto_close_min_peak_pnl: float = Field(
        default=0.02,
        ge=0.0,
        description="Minimum peak unrealized PnL before trailing take-profit rule arms.",
        validation_alias="AUTO_CLOSE_MIN_PEAK_PNL",
    )
    auto_close_spread_convergence_epsilon: float = Field(
        default=0.015,
        ge=0.0,
        description="Treat YES+NO mids as converged when sum ≥ 1 − epsilon (paired auto-close).",
        validation_alias="AUTO_CLOSE_SPREAD_EPS",
    )

    # --- Защита от рисков ---
    max_daily_loss_usd: float = Field(
        default=0.0, ge=0.0,
        description="Стоп торговли при суточном убытке свыше этой суммы (USD). 0 = выкл.",
        validation_alias="MAX_DAILY_LOSS_USD",
    )
    max_daily_loss_pct: float = Field(
        default=20.0, ge=0.0, le=100.0,
        description="Стоп торговли при суточном убытке свыше X% от банкролла. 0 = выкл.",
        validation_alias="MAX_DAILY_LOSS_PCT",
    )
    quote_max_age_seconds: float = Field(
        default=8.0, ge=1.0,
        description="Котировка старше этого числа секунд считается устаревшей и отклоняется.",
        validation_alias="QUOTE_MAX_AGE_SECONDS",
    )
    max_execution_slippage_pct: float = Field(
        default=1.5, ge=0.0,
        description="Максимальное проскальзывание от котировки при исполнении (в %). Отмена при превышении.",
        validation_alias="MAX_EXECUTION_SLIPPAGE_PCT",
    )
    amm_slippage_buffer: float = Field(
        default=0.02, ge=0.0,
        description="Буфер на проскальзывание AMM Polymarket (прибавляется к цене при расчёте).",
        validation_alias="AMM_SLIPPAGE_BUFFER",
    )
    min_fill_ratio: float = Field(
        default=0.80, ge=0.0, le=1.0,
        description="Минимальная доля заполнения ордера. Ниже — хеджируем незаполненный остаток.",
        validation_alias="MIN_FILL_RATIO",
    )
    max_expiry_diff_days: int = Field(
        default=14, ge=0,
        description="Максимальная разница в датах истечения при матчинге рынков (дней).",
        validation_alias="MAX_EXPIRY_DIFF_DAYS",
    )

    # --- Kelly criterion position sizing ---
    kelly_enabled: bool = Field(
        default=False,
        description="Use Kelly criterion to compute optimal bet size instead of fixed max_order_contracts.",
        validation_alias="KELLY_ENABLED",
    )
    kelly_fraction: float = Field(
        default=0.25,
        ge=0.01,
        le=1.0,
        description="Fraction of full-Kelly to apply (0.25 = quarter-Kelly, safer).",
        validation_alias="KELLY_FRACTION",
    )
    kelly_bankroll: float = Field(
        default=100.0,
        ge=1.0,
        description="Current bankroll in USD used for Kelly position sizing.",
        validation_alias="KELLY_BANKROLL",
    )

    # --- Multi-opportunity concurrency ---
    max_concurrent_opportunities: int = Field(
        default=3,
        ge=1,
        le=20,
        description="Execute top-N opportunities per iteration simultaneously.",
        validation_alias="MAX_CONCURRENT_OPPORTUNITIES",
    )

    # --- Latency optimisation ---
    market_cache_ttl_seconds: float = Field(
        default=60.0, ge=10.0,
        description="Список рынков кэшируется на это время. В каждой итерации берётся из кэша; "
                    "тяжёлый GET /markets повторяется только по истечении TTL.",
        validation_alias="MARKET_CACHE_TTL_SECONDS",
    )

    # --- Semantic market matching ---
    semantic_matching_enabled: bool = Field(
        default=False,
        description="Use sentence-transformers embeddings for title matching (requires pip install sentence-transformers).",
        validation_alias="SEMANTIC_MATCHING_ENABLED",
    )

    @field_validator("kalshi_base_url", "polymarket_clob_host")
    @classmethod
    def strip_trailing_slash(cls, v: str) -> str:
        return v.rstrip("/")

    @property
    def kalshi_signing_configured(self) -> bool:
        has_id = bool(self.kalshi_api_key_id and self.kalshi_api_key_id.strip())
        has_secret = bool(
            (self.kalshi_private_key_pem and self.kalshi_private_key_pem.strip())
            or (self.kalshi_private_key_path and self.kalshi_private_key_path.strip())
        )
        return has_id and has_secret


def get_settings() -> Settings:
    """Factory used by entrypoints for a single settings instance."""
    return Settings()
