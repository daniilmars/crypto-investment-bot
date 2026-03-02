"""Tests for config validation (Phase 1)."""

import pytest
from pydantic import ValidationError

from src.config_validation import (
    TradingSettings,
    LiveTradingSettings,
    StockTradingSettings,
    validate_config,
)


# --- TradingSettings ---

def test_valid_defaults():
    """Default values should pass validation."""
    ts = TradingSettings()
    assert ts.paper_trading is True
    assert ts.signal_mode.value == "sentiment"


def test_valid_full_config():
    """A fully specified config should pass."""
    ts = TradingSettings(
        paper_trading=True,
        stop_loss_percentage=0.035,
        take_profit_percentage=0.08,
        rsi_oversold_threshold=30,
        rsi_overbought_threshold=70,
        signal_mode="scoring",
    )
    assert ts.signal_mode.value == "scoring"


def test_sl_gte_tp_rejected():
    """stop_loss_percentage >= take_profit_percentage should fail."""
    with pytest.raises(ValidationError, match="stop_loss_percentage"):
        TradingSettings(stop_loss_percentage=0.10, take_profit_percentage=0.05)


def test_sl_equal_tp_rejected():
    """stop_loss_percentage == take_profit_percentage should fail."""
    with pytest.raises(ValidationError, match="stop_loss_percentage"):
        TradingSettings(stop_loss_percentage=0.05, take_profit_percentage=0.05)


def test_rsi_oversold_gte_overbought_rejected():
    """rsi_oversold >= rsi_overbought should fail."""
    with pytest.raises(ValidationError, match="rsi_oversold_threshold"):
        TradingSettings(rsi_oversold_threshold=80, rsi_overbought_threshold=70)


def test_invalid_signal_mode_rejected():
    """An invalid signal_mode should fail."""
    with pytest.raises(ValidationError):
        TradingSettings(signal_mode="invalid_mode")


def test_risk_pct_zero_rejected():
    """trade_risk_percentage = 0 should fail."""
    with pytest.raises(ValidationError, match="trade_risk_percentage"):
        TradingSettings(trade_risk_percentage=0)


def test_risk_pct_above_one_rejected():
    """trade_risk_percentage > 1 should fail."""
    with pytest.raises(ValidationError, match="trade_risk_percentage"):
        TradingSettings(trade_risk_percentage=1.5)


# --- LiveTradingSettings ---

def test_live_floor_below_capital():
    """balance_floor_usd must be < initial_capital."""
    lt = LiveTradingSettings(initial_capital=100.0, balance_floor_usd=70.0)
    assert lt.balance_floor_usd < lt.initial_capital


def test_live_floor_gte_capital_rejected():
    """balance_floor_usd >= initial_capital should fail."""
    with pytest.raises(ValidationError, match="balance_floor_usd"):
        LiveTradingSettings(initial_capital=100.0, balance_floor_usd=100.0)


def test_live_sl_gte_tp_rejected():
    """Live trading: SL >= TP should fail."""
    with pytest.raises(ValidationError, match="stop_loss_percentage"):
        LiveTradingSettings(stop_loss_percentage=0.10, take_profit_percentage=0.05)


def test_live_invalid_mode_rejected():
    """Invalid live mode should fail."""
    with pytest.raises(ValidationError):
        LiveTradingSettings(mode="invalid")


# --- StockTradingSettings ---

def test_stock_rsi_thresholds():
    """Stock RSI thresholds must be ordered correctly."""
    with pytest.raises(ValidationError, match="rsi_oversold_threshold"):
        StockTradingSettings(rsi_oversold_threshold=80, rsi_overbought_threshold=70)


# --- validate_config integration ---

def test_validate_config_valid():
    """validate_config should pass with valid settings."""
    config = {
        'settings': {
            'paper_trading': True,
            'paper_trading_initial_capital': 10000.0,
            'trade_risk_percentage': 0.03,
            'stop_loss_percentage': 0.035,
            'take_profit_percentage': 0.08,
            'max_concurrent_positions': 5,
            'sma_period': 20,
            'rsi_overbought_threshold': 70,
            'rsi_oversold_threshold': 30,
            'signal_mode': 'sentiment',
            'simulated_fee_pct': 0.001,
        }
    }
    # Should not raise
    validate_config(config)


def test_validate_config_invalid_exits():
    """validate_config should raise SystemExit on invalid config."""
    config = {
        'settings': {
            'stop_loss_percentage': 0.10,
            'take_profit_percentage': 0.05,  # SL > TP
        }
    }
    with pytest.raises(SystemExit):
        validate_config(config)


def test_validate_config_empty_settings():
    """validate_config should handle missing settings gracefully."""
    # Should not raise
    validate_config({})
    validate_config({'settings': {}})
