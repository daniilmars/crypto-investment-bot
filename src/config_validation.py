"""Startup config validation using Pydantic models.

Called at the end of load_config(). Failure logs an error and exits.
Existing .get() call sites remain unchanged — validation is a startup gate only.
"""

from enum import Enum
from typing import Optional

from pydantic import BaseModel, field_validator, model_validator

from src.logger import log


class SignalMode(str, Enum):
    scoring = "scoring"
    sentiment = "sentiment"


class BrokerMode(str, Enum):
    paper_only = "paper_only"
    alpaca = "alpaca"


class LiveMode(str, Enum):
    testnet = "testnet"
    live = "live"


class TrailingStopSettings(BaseModel):
    trailing_stop_enabled: bool = True
    trailing_stop_activation: float = 0.02
    trailing_stop_distance: float = 0.015


class SignalConfirmationSettings(BaseModel):
    enabled: bool = False
    timeout_minutes: int = 30
    require_confirmation_for: list[str] = ["BUY", "SELL"]


class MarketAlertsSettings(BaseModel):
    enabled: bool = True
    daily_digest_hour_utc: int = 8
    daily_digest_lookahead_hours: int = 72


class LiveTradingSettings(BaseModel):
    enabled: bool = False
    mode: LiveMode = LiveMode.testnet
    initial_capital: float = 100.0
    max_concurrent_positions: int = 2
    trade_risk_percentage: float = 0.05
    stop_loss_percentage: float = 0.03
    take_profit_percentage: float = 0.06
    daily_loss_limit_pct: float = 0.10
    max_drawdown_pct: float = 0.25
    balance_floor_usd: float = 70.0
    max_consecutive_losses: int = 3
    cooldown_hours: int = 24

    @model_validator(mode='after')
    def validate_floor_below_capital(self):
        if self.balance_floor_usd >= self.initial_capital:
            raise ValueError(
                f"balance_floor_usd ({self.balance_floor_usd}) must be less than "
                f"initial_capital ({self.initial_capital})")
        return self

    @model_validator(mode='after')
    def validate_sl_lt_tp(self):
        if self.stop_loss_percentage >= self.take_profit_percentage:
            raise ValueError(
                f"stop_loss_percentage ({self.stop_loss_percentage}) must be less than "
                f"take_profit_percentage ({self.take_profit_percentage})")
        return self


class StockTradingSettings(BaseModel):
    enabled: bool = False
    broker: BrokerMode = BrokerMode.paper_only
    paper_trading_initial_capital: float = 10000.0
    max_concurrent_positions: int = 8
    rsi_overbought_threshold: int = 70
    rsi_oversold_threshold: int = 30

    @model_validator(mode='after')
    def validate_rsi_thresholds(self):
        if self.rsi_oversold_threshold >= self.rsi_overbought_threshold:
            raise ValueError(
                f"rsi_oversold_threshold ({self.rsi_oversold_threshold}) must be less than "
                f"rsi_overbought_threshold ({self.rsi_overbought_threshold})")
        return self


class TradingSettings(BaseModel):
    paper_trading: bool = True
    paper_trading_initial_capital: float = 10000.0
    simulated_fee_pct: float = 0.001
    trade_risk_percentage: float = 0.03
    stop_loss_percentage: float = 0.035
    take_profit_percentage: float = 0.08
    max_concurrent_positions: int = 5
    sma_period: int = 20
    rsi_overbought_threshold: int = 70
    rsi_oversold_threshold: int = 30
    signal_mode: SignalMode = SignalMode.sentiment

    trailing_stop_enabled: bool = True
    trailing_stop_activation: float = 0.02
    trailing_stop_distance: float = 0.015

    live_trading: Optional[LiveTradingSettings] = None
    stock_trading: Optional[StockTradingSettings] = None
    signal_confirmation: Optional[SignalConfirmationSettings] = None
    market_alerts: Optional[MarketAlertsSettings] = None

    @model_validator(mode='after')
    def validate_sl_lt_tp(self):
        if self.stop_loss_percentage >= self.take_profit_percentage:
            raise ValueError(
                f"stop_loss_percentage ({self.stop_loss_percentage}) must be less than "
                f"take_profit_percentage ({self.take_profit_percentage})")
        return self

    @model_validator(mode='after')
    def validate_rsi_thresholds(self):
        if self.rsi_oversold_threshold >= self.rsi_overbought_threshold:
            raise ValueError(
                f"rsi_oversold_threshold ({self.rsi_oversold_threshold}) must be less than "
                f"rsi_overbought_threshold ({self.rsi_overbought_threshold})")
        return self

    @field_validator('trade_risk_percentage')
    @classmethod
    def risk_pct_bounds(cls, v):
        if not 0 < v <= 1:
            raise ValueError(f"trade_risk_percentage must be in (0, 1], got {v}")
        return v


def validate_config(config: dict) -> None:
    """Validates the 'settings' section of the loaded config.

    Raises SystemExit(1) on validation failure.
    """
    settings = config.get('settings', {})
    if not settings:
        log.warning("No 'settings' section found in config — skipping validation.")
        return

    # Build a flat dict for TradingSettings, pulling nested sections
    flat = {k: v for k, v in settings.items()
            if k not in ('live_trading', 'stock_trading', 'signal_confirmation',
                         'market_alerts', 'watch_list', 'news_analysis',
                         'auto_trading', 'position_analyst', 'position_monitor',
                         'macro_regime', 'sector_limits', 'event_calendar',
                         'ipo_tracking', 'news_scraper_daemon', 'sentiment_signal',
                         'regular_status_update', 'status_report_hours',
                         'stoploss_cooldown_hours', 'rsi_period')}

    # Attach nested sections if present
    if 'live_trading' in settings:
        flat['live_trading'] = settings['live_trading']
    if 'stock_trading' in settings:
        flat['stock_trading'] = {k: v for k, v in settings['stock_trading'].items()
                                 if k not in ('watch_list', 'alpaca', 'sma_period',
                                              'rsi_period', 'pe_ratio_buy_threshold',
                                              'pe_ratio_sell_threshold',
                                              'earnings_growth_sell_threshold',
                                              'volume_spike_multiplier')}
    if 'signal_confirmation' in settings:
        flat['signal_confirmation'] = settings['signal_confirmation']
    if 'market_alerts' in settings:
        flat['market_alerts'] = {k: v for k, v in settings['market_alerts'].items()
                                 if k not in ('breaking_news', 'sector_moves',
                                              'event_urgency')}

    try:
        TradingSettings(**flat)
        log.info("Config validation passed.")
    except Exception as e:
        log.error(f"Config validation failed: {e}")
        raise SystemExit(1) from e
