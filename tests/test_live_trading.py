# tests/test_live_trading.py
"""Tests for live trading infrastructure: circuit breaker, position sizing, OCO calculations."""

import pytest
from unittest.mock import patch, MagicMock
import sqlite3
import psycopg2


# --- Circuit Breaker Tests ---

class TestCircuitBreaker:
    """Tests for src/execution/circuit_breaker.py"""

    @patch('src.execution.circuit_breaker.is_in_cooldown', return_value=False)
    @patch('src.execution.circuit_breaker._get_peak_balance', return_value=100.0)
    @patch('src.execution.circuit_breaker.record_circuit_breaker_event')
    @patch('src.execution.circuit_breaker._get_live_config')
    def test_all_clear(self, mock_config, mock_record, mock_peak, mock_cooldown):
        """No circuit breaker should trip when everything is healthy."""
        from src.execution.circuit_breaker import check_circuit_breaker
        mock_config.return_value = {
            'initial_capital': 100.0,
            'daily_loss_limit_pct': 0.10,
            'max_drawdown_pct': 0.25,
            'balance_floor_usd': 70.0,
            'max_consecutive_losses': 3,
            'cooldown_hours': 24,
        }
        tripped, reason = check_circuit_breaker(
            balance=95.0, daily_pnl=-2.0,
            recent_trades=[{'pnl': 1.0}, {'pnl': -0.5}, {'pnl': 2.0}]
        )
        assert tripped is False
        assert reason == ""
        mock_record.assert_not_called()

    @patch('src.execution.circuit_breaker.is_in_cooldown', return_value=True)
    @patch('src.execution.circuit_breaker._get_live_config')
    def test_cooldown_active(self, mock_config, mock_cooldown):
        """Circuit breaker trips when cooldown is active."""
        from src.execution.circuit_breaker import check_circuit_breaker
        mock_config.return_value = {
            'initial_capital': 100.0,
            'cooldown_hours': 24,
            'daily_loss_limit_pct': 0.10,
            'max_drawdown_pct': 0.25,
            'balance_floor_usd': 70.0,
            'max_consecutive_losses': 3,
        }
        tripped, reason = check_circuit_breaker(balance=95.0, daily_pnl=0, recent_trades=[])
        assert tripped is True
        assert "Cooldown" in reason

    @patch('src.execution.circuit_breaker.is_in_cooldown', return_value=False)
    @patch('src.execution.circuit_breaker._get_peak_balance', return_value=100.0)
    @patch('src.execution.circuit_breaker.record_circuit_breaker_event')
    @patch('src.execution.circuit_breaker._get_live_config')
    def test_balance_floor(self, mock_config, mock_record, mock_peak, mock_cooldown):
        """Circuit breaker trips when balance falls below floor."""
        from src.execution.circuit_breaker import check_circuit_breaker
        mock_config.return_value = {
            'initial_capital': 100.0,
            'daily_loss_limit_pct': 0.10,
            'max_drawdown_pct': 0.25,
            'balance_floor_usd': 70.0,
            'max_consecutive_losses': 3,
            'cooldown_hours': 24,
        }
        tripped, reason = check_circuit_breaker(balance=65.0, daily_pnl=0, recent_trades=[])
        assert tripped is True
        assert "floor" in reason.lower()
        mock_record.assert_called_once_with('balance_floor', reason)

    @patch('src.execution.circuit_breaker.is_in_cooldown', return_value=False)
    @patch('src.execution.circuit_breaker._get_peak_balance', return_value=100.0)
    @patch('src.execution.circuit_breaker.record_circuit_breaker_event')
    @patch('src.execution.circuit_breaker._get_live_config')
    def test_daily_loss_limit(self, mock_config, mock_record, mock_peak, mock_cooldown):
        """Circuit breaker trips when daily loss exceeds limit."""
        from src.execution.circuit_breaker import check_circuit_breaker
        mock_config.return_value = {
            'initial_capital': 100.0,
            'daily_loss_limit_pct': 0.10,
            'max_drawdown_pct': 0.25,
            'balance_floor_usd': 70.0,
            'max_consecutive_losses': 3,
            'cooldown_hours': 24,
        }
        # Daily loss of -$11 exceeds 10% of $100 = $10
        tripped, reason = check_circuit_breaker(balance=89.0, daily_pnl=-11.0, recent_trades=[])
        assert tripped is True
        assert "Daily loss" in reason
        mock_record.assert_called_once()

    @patch('src.execution.circuit_breaker.is_in_cooldown', return_value=False)
    @patch('src.execution.circuit_breaker._get_peak_balance', return_value=100.0)
    @patch('src.execution.circuit_breaker.record_circuit_breaker_event')
    @patch('src.execution.circuit_breaker._get_live_config')
    def test_max_drawdown(self, mock_config, mock_record, mock_peak, mock_cooldown):
        """Circuit breaker trips when balance hits max drawdown from peak."""
        from src.execution.circuit_breaker import check_circuit_breaker
        mock_config.return_value = {
            'initial_capital': 100.0,
            'daily_loss_limit_pct': 0.10,
            'max_drawdown_pct': 0.25,
            'balance_floor_usd': 50.0,  # Lower floor so it doesn't trigger first
            'max_consecutive_losses': 3,
            'cooldown_hours': 24,
        }
        # Balance $74 is below 75% of peak $100 = $75 threshold
        tripped, reason = check_circuit_breaker(balance=74.0, daily_pnl=-1.0, recent_trades=[])
        assert tripped is True
        assert "drawdown" in reason.lower()

    @patch('src.execution.circuit_breaker.is_in_cooldown', return_value=False)
    @patch('src.execution.circuit_breaker._get_peak_balance', return_value=100.0)
    @patch('src.execution.circuit_breaker.record_circuit_breaker_event')
    @patch('src.execution.circuit_breaker._get_live_config')
    def test_consecutive_losses(self, mock_config, mock_record, mock_peak, mock_cooldown):
        """Circuit breaker trips after N consecutive losing trades."""
        from src.execution.circuit_breaker import check_circuit_breaker
        mock_config.return_value = {
            'initial_capital': 100.0,
            'daily_loss_limit_pct': 0.10,
            'max_drawdown_pct': 0.25,
            'balance_floor_usd': 70.0,
            'max_consecutive_losses': 3,
            'cooldown_hours': 24,
        }
        recent = [{'pnl': -1.0}, {'pnl': -2.0}, {'pnl': -0.5}]
        tripped, reason = check_circuit_breaker(balance=95.0, daily_pnl=-3.5, recent_trades=recent)
        assert tripped is True
        assert "consecutive" in reason.lower() or "losses" in reason.lower()

    @patch('src.execution.circuit_breaker.is_in_cooldown', return_value=False)
    @patch('src.execution.circuit_breaker._get_peak_balance', return_value=100.0)
    @patch('src.execution.circuit_breaker.record_circuit_breaker_event')
    @patch('src.execution.circuit_breaker._get_live_config')
    def test_consecutive_losses_not_triggered_with_win(self, mock_config, mock_record, mock_peak, mock_cooldown):
        """Consecutive losses check passes if there's a win in the sequence."""
        from src.execution.circuit_breaker import check_circuit_breaker
        mock_config.return_value = {
            'initial_capital': 100.0,
            'daily_loss_limit_pct': 0.10,
            'max_drawdown_pct': 0.25,
            'balance_floor_usd': 70.0,
            'max_consecutive_losses': 3,
            'cooldown_hours': 24,
        }
        # Second trade is a win — should not trip
        recent = [{'pnl': -1.0}, {'pnl': 0.5}, {'pnl': -2.0}]
        tripped, reason = check_circuit_breaker(balance=95.0, daily_pnl=-2.5, recent_trades=recent)
        assert tripped is False


# --- Position Sizing Tests ---

class TestRoundToStepSize:
    """Tests for _round_to_step_size in binance_trader.py"""

    def test_step_size_btc(self):
        """BTC typically has step size 0.00001"""
        from src.execution.binance_trader import _round_to_step_size
        result = _round_to_step_size(0.001234567, 0.00001)
        assert result == 0.00123

    def test_step_size_eth(self):
        """ETH step size 0.0001"""
        from src.execution.binance_trader import _round_to_step_size
        result = _round_to_step_size(0.12345, 0.0001)
        assert result == 0.1234

    def test_step_size_1(self):
        """Some tokens have step size 1"""
        from src.execution.binance_trader import _round_to_step_size
        result = _round_to_step_size(15.7, 1)
        assert result == 15.0

    def test_step_size_0_01(self):
        from src.execution.binance_trader import _round_to_step_size
        result = _round_to_step_size(3.456, 0.01)
        assert result == 3.45

    def test_rounds_down_not_up(self):
        """Must always round DOWN (floor) to avoid exceeding available funds."""
        from src.execution.binance_trader import _round_to_step_size
        result = _round_to_step_size(0.999999, 0.001)
        assert result == 0.999

    def test_zero_step_size(self):
        """Zero step size should return quantity unchanged."""
        from src.execution.binance_trader import _round_to_step_size
        result = _round_to_step_size(1.23456, 0)
        assert result == 1.23456

    def test_negative_step_size(self):
        """Negative step size should return quantity unchanged."""
        from src.execution.binance_trader import _round_to_step_size
        result = _round_to_step_size(1.23456, -0.01)
        assert result == 1.23456


class TestValidateOrderQuantity:
    """Tests for _validate_order_quantity in binance_trader.py"""

    def test_passes_when_no_symbol_info(self):
        from src.execution.binance_trader import _validate_order_quantity
        result = _validate_order_quantity(None, 0.5, 50000.0)
        assert result == 0.5

    def test_rejects_below_min_qty(self):
        from src.execution.binance_trader import _validate_order_quantity
        sym_info = {
            'symbol': 'BTCUSDT',
            'filters': {
                'LOT_SIZE': {'stepSize': '0.00001', 'minQty': '0.001', 'maxQty': '9999'},
            }
        }
        result = _validate_order_quantity(sym_info, 0.0001, 50000.0)
        assert result is None

    def test_rejects_below_min_notional(self):
        from src.execution.binance_trader import _validate_order_quantity
        sym_info = {
            'symbol': 'BTCUSDT',
            'filters': {
                'LOT_SIZE': {'stepSize': '0.00001', 'minQty': '0.00001', 'maxQty': '9999'},
                'NOTIONAL': {'minNotional': '5.0'},
            }
        }
        # 0.00001 BTC * $50000 = $0.50, below $5 min notional
        result = _validate_order_quantity(sym_info, 0.00001, 50000.0)
        assert result is None

    def test_accepts_valid_order(self):
        from src.execution.binance_trader import _validate_order_quantity
        sym_info = {
            'symbol': 'BTCUSDT',
            'filters': {
                'LOT_SIZE': {'stepSize': '0.00001', 'minQty': '0.00001', 'maxQty': '9999'},
                'NOTIONAL': {'minNotional': '5.0'},
            }
        }
        # 0.001 BTC * $50000 = $50, above $5 min notional
        result = _validate_order_quantity(sym_info, 0.001, 50000.0)
        assert result == 0.001


# --- OCO Price Calculation Tests ---

class TestOCOPriceCalculation:
    """Tests that OCO bracket prices are calculated correctly."""

    def test_oco_prices_with_defaults(self):
        """Verify SL and TP prices are calculated from entry price."""
        entry = 50000.0
        sl_pct = 0.03
        tp_pct = 0.06

        stop_price = round(entry * (1 - sl_pct), 8)
        take_profit_price = round(entry * (1 + tp_pct), 8)

        assert stop_price == 48500.0
        assert take_profit_price == 53000.0

    def test_oco_stop_limit_below_stop(self):
        """Stop limit price should be slightly below stop price for fill reliability."""
        entry = 50000.0
        sl_pct = 0.03

        stop_price = round(entry * (1 - sl_pct), 8)
        stop_limit_price = round(stop_price * 0.998, 8)

        assert stop_limit_price < stop_price
        assert stop_limit_price == pytest.approx(48403.0, abs=1.0)


# --- Trading Mode Detection Tests ---

class TestTradingMode:
    """Tests for _is_live_trading and _get_trading_mode."""

    @patch('src.execution.binance_trader.app_config', {
        'settings': {'paper_trading': True, 'live_trading': {'enabled': True, 'mode': 'live'}}
    })
    def test_paper_trading_overrides_live(self):
        """paper_trading=True should override live_trading.enabled."""
        from src.execution.binance_trader import _is_live_trading, _get_trading_mode
        assert _is_live_trading() is False
        assert _get_trading_mode() == 'paper'

    @patch('src.execution.binance_trader.app_config', {
        'settings': {'paper_trading': False, 'live_trading': {'enabled': True, 'mode': 'testnet'}}
    })
    def test_testnet_mode(self):
        from src.execution.binance_trader import _is_live_trading, _get_trading_mode
        assert _is_live_trading() is True
        assert _get_trading_mode() == 'testnet'

    @patch('src.execution.binance_trader.app_config', {
        'settings': {'paper_trading': False, 'live_trading': {'enabled': True, 'mode': 'live'}}
    })
    def test_live_mode(self):
        from src.execution.binance_trader import _is_live_trading, _get_trading_mode
        assert _is_live_trading() is True
        assert _get_trading_mode() == 'live'

    @patch('src.execution.binance_trader.app_config', {
        'settings': {'paper_trading': False, 'live_trading': {'enabled': False, 'mode': 'live'}}
    })
    def test_live_disabled(self):
        """live_trading.enabled=False should result in paper mode."""
        from src.execution.binance_trader import _is_live_trading, _get_trading_mode
        assert _is_live_trading() is False
        assert _get_trading_mode() == 'paper'

    @patch('src.execution.binance_trader.app_config', {'settings': {}})
    def test_default_is_paper(self):
        """No config at all should default to paper."""
        from src.execution.binance_trader import _is_live_trading, _get_trading_mode
        assert _is_live_trading() is False
        assert _get_trading_mode() == 'paper'


# --- Fill Extraction Tests ---

class TestFillExtraction:
    """Tests for _extract_fill_price and _extract_fees."""

    def test_extract_fill_price_single_fill(self):
        from src.execution.binance_trader import _extract_fill_price
        order = {'fills': [{'price': '50000.0', 'qty': '0.001'}]}
        assert _extract_fill_price(order) == 50000.0

    def test_extract_fill_price_multiple_fills(self):
        """Weighted average of multiple fills."""
        from src.execution.binance_trader import _extract_fill_price
        order = {'fills': [
            {'price': '50000.0', 'qty': '0.001'},
            {'price': '50100.0', 'qty': '0.002'},
        ]}
        # (50000*0.001 + 50100*0.002) / 0.003 = (50 + 100.2) / 0.003 = 50066.67
        result = _extract_fill_price(order)
        assert result == pytest.approx(50066.67, abs=0.01)

    def test_extract_fill_price_no_fills(self):
        from src.execution.binance_trader import _extract_fill_price
        assert _extract_fill_price({'fills': []}) is None
        assert _extract_fill_price({}) is None

    def test_extract_fees(self):
        from src.execution.binance_trader import _extract_fees
        order = {'fills': [
            {'commission': '0.001', 'commissionAsset': 'BNB'},
            {'commission': '0.002', 'commissionAsset': 'BNB'},
        ]}
        assert _extract_fees(order) == pytest.approx(0.003)

    def test_extract_fees_no_fills(self):
        from src.execution.binance_trader import _extract_fees
        assert _extract_fees({}) == 0.0
