# tests/test_execution_pipeline_fixes.py
"""Tests for execution pipeline fixes: min notional, PDT wiring, and sell alert timing."""

import asyncio
from unittest.mock import patch, AsyncMock
from datetime import datetime, timezone, timedelta


def run_async(coro):
    """Helper to run async functions in sync tests."""
    return asyncio.run(coro)


# --- Fix 3: Min Notional Validation ---

class TestMinNotional:
    """Tests for configurable min trade notional."""

    @patch('src.orchestration.trade_executor.send_telegram_alert', new_callable=AsyncMock)
    @patch('src.orchestration.trade_executor.is_confirmation_required', return_value=False)
    @patch('src.orchestration.trade_executor.place_order')
    @patch('src.orchestration.trade_executor.app_config')
    def test_buy_below_min_notional_skipped(self, mock_config, mock_order,
                                             mock_confirm, mock_alert):
        """BUY with notional below $5 minimum should be skipped."""
        from src.orchestration.trade_executor import execute_buy
        mock_config.get.return_value = {'min_trade_notional': 5.00}

        # With risk_pct=0.00003, balance=10000, price=100000:
        # capital_to_risk = 10000 * 0.00003 * 1.0 = 0.3
        # quantity = 0.3 / 100000 = 0.000003
        # notional = 0.000003 * 100000 = $0.30 < $5.00
        result = run_async(execute_buy(
            "BTC", {"signal": "BUY", "symbol": "BTC", "current_price": 100000},
            current_price=100000.0, current_balance=10000.0,
            risk_pct=0.00003, size_mult=1.0, trading_strategy='auto'))
        assert result is None
        mock_order.assert_not_called()

    @patch('src.orchestration.trade_executor.send_telegram_alert', new_callable=AsyncMock)
    @patch('src.orchestration.trade_executor.is_confirmation_required', return_value=False)
    @patch('src.orchestration.trade_executor.place_order')
    @patch('src.orchestration.trade_executor.app_config')
    def test_buy_above_min_notional_proceeds(self, mock_config, mock_order,
                                              mock_confirm, mock_alert):
        """BUY with notional above $5 minimum should proceed."""
        from src.orchestration.trade_executor import execute_buy
        mock_config.get.return_value = {'min_trade_notional': 5.00}
        mock_order.return_value = {'status': 'FILLED', 'order_id': 'P_1'}

        # risk_pct=0.01, balance=10000, price=50000:
        # capital = 10000 * 0.01 * 1.0 = 100
        # quantity = 100 / 50000 = 0.002
        # notional = 0.002 * 50000 = $100 > $5.00
        result = run_async(execute_buy(
            "BTC", {"signal": "BUY", "symbol": "BTC", "current_price": 50000},
            current_price=50000.0, current_balance=10000.0,
            risk_pct=0.01, size_mult=1.0, trading_strategy='auto'))
        assert result is not None
        mock_order.assert_called_once()

    def test_manual_buy_is_blocked(self):
        """Manual strategy BUY should never proceed — manual is exit-only."""
        from src.orchestration.trade_executor import execute_buy
        result = run_async(execute_buy(
            "BTC", {"signal": "BUY", "symbol": "BTC", "current_price": 50000},
            current_price=50000.0, current_balance=10000.0,
            risk_pct=0.01, size_mult=1.0, trading_strategy='manual'))
        assert result is None


# --- Fix 4: PDT Tracking ---

class TestIsSameDayTrade:
    """Tests for _is_same_day_trade helper."""

    def test_same_day_returns_true(self):
        from src.execution.stock_trader import _is_same_day_trade
        now = datetime.now(timezone.utc)
        position = {'entry_timestamp': now.isoformat()}
        assert _is_same_day_trade(position) is True

    def test_old_position_returns_false(self):
        from src.execution.stock_trader import _is_same_day_trade
        yesterday = datetime.now(timezone.utc) - timedelta(days=2)
        position = {'entry_timestamp': yesterday.isoformat()}
        assert _is_same_day_trade(position) is False

    def test_none_timestamp_returns_false(self):
        from src.execution.stock_trader import _is_same_day_trade
        assert _is_same_day_trade({}) is False
        assert _is_same_day_trade({'entry_timestamp': None}) is False

    def test_datetime_object_works(self):
        from src.execution.stock_trader import _is_same_day_trade
        now = datetime.now(timezone.utc)
        position = {'entry_timestamp': now}
        assert _is_same_day_trade(position) is True

    def test_z_suffix_parsed(self):
        from src.execution.stock_trader import _is_same_day_trade
        now = datetime.now(timezone.utc)
        position = {'entry_timestamp': now.strftime('%Y-%m-%dT%H:%M:%SZ')}
        assert _is_same_day_trade(position) is True


class TestPDTRecordedOnStockExit:
    """Tests that PDT is recorded when stock positions exit same-day."""

    @patch('src.orchestration.position_monitor.process_closed_trade')
    @patch('src.orchestration.position_monitor.send_telegram_alert', new_callable=AsyncMock)
    @patch('src.orchestration.position_monitor.save_stoploss_cooldown', new_callable=AsyncMock)
    @patch('src.orchestration.position_monitor.place_order')
    @patch('src.orchestration.position_monitor.bot_state')
    def test_pdt_recorded_on_stock_stop_loss(self, mock_state, mock_order,
                                              mock_cooldown, mock_alert,
                                              mock_feedback):
        """PDT recorded when stock position hits stop-loss on same day."""
        from src.orchestration.position_monitor import monitor_position
        from src.execution.stock_trader import _day_trades
        _day_trades.clear()

        mock_state.update_trailing_stop.return_value = 100.0

        position = {
            'symbol': 'AAPL', 'entry_price': 150.0, 'order_id': 'P_1',
            'quantity': 10, 'status': 'OPEN',
            'entry_timestamp': datetime.now(timezone.utc).isoformat(),
        }

        result = run_async(monitor_position(
            position, current_price=140.0,  # -6.7% loss
            stop_loss_pct=0.05, take_profit_pct=0.10,
            trailing_stop_enabled=False,
            trailing_stop_activation=0.02, trailing_stop_distance=0.015,
            stoploss_cooldown_hours=6, asset_type='stock'))

        assert result == 'stop_loss'
        assert len(_day_trades) == 1

    @patch('src.orchestration.position_monitor.process_closed_trade')
    @patch('src.orchestration.position_monitor.send_telegram_alert', new_callable=AsyncMock)
    @patch('src.orchestration.position_monitor.place_order')
    @patch('src.orchestration.position_monitor.bot_state')
    def test_pdt_not_recorded_on_old_position(self, mock_state, mock_order,
                                               mock_alert, mock_feedback):
        """PDT NOT recorded when stock position opened 2 days ago hits stop-loss."""
        from src.orchestration.position_monitor import monitor_position
        from src.execution.stock_trader import _day_trades
        _day_trades.clear()

        mock_state.update_trailing_stop.return_value = 100.0

        old_date = datetime.now(timezone.utc) - timedelta(days=2)
        position = {
            'symbol': 'AAPL', 'entry_price': 150.0, 'order_id': 'P_2',
            'quantity': 10, 'status': 'OPEN',
            'entry_timestamp': old_date.isoformat(),
        }

        result = run_async(monitor_position(
            position, current_price=140.0,
            stop_loss_pct=0.05, take_profit_pct=0.10,
            trailing_stop_enabled=False,
            trailing_stop_activation=0.02, trailing_stop_distance=0.015,
            asset_type='stock'))

        assert result == 'stop_loss'
        assert len(_day_trades) == 0


# --- Fix 5: Sell Alert Timing ---

class TestSellAlertTiming:
    """Tests that sell alert only fires after successful order."""

    @patch('src.orchestration.trade_executor.bot_state')
    @patch('src.orchestration.trade_executor.send_telegram_alert', new_callable=AsyncMock)
    @patch('src.orchestration.trade_executor.is_confirmation_required', return_value=False)
    @patch('src.orchestration.trade_executor.place_order')
    @patch('src.orchestration.trade_executor.app_config')
    def test_sell_alert_not_sent_on_failure(self, mock_config, mock_order,
                                             mock_confirm, mock_alert,
                                             mock_state):
        """SELL alert should NOT be sent when order fails."""
        from src.orchestration.trade_executor import execute_sell
        mock_config.get.return_value = {}
        mock_order.return_value = {'status': 'FAILED', 'message': 'Insufficient balance'}

        position = {'symbol': 'BTC', 'order_id': 'P_1', 'quantity': 0.01,
                     'status': 'OPEN', 'entry_price': 50000}
        signal = {'signal': 'SELL', 'symbol': 'BTC', 'current_price': 48000}

        run_async(execute_sell("BTC", signal, position, 48000.0))
        mock_alert.assert_not_called()
        # Trailing stop should NOT be cleared on failure
        mock_state.clear_trailing_stop.assert_not_called()

    @patch('src.orchestration.trade_executor.bot_state')
    @patch('src.orchestration.trade_executor.send_telegram_alert', new_callable=AsyncMock)
    @patch('src.orchestration.trade_executor.is_confirmation_required', return_value=False)
    @patch('src.orchestration.trade_executor.place_order')
    @patch('src.orchestration.trade_executor.app_config')
    def test_sell_alert_sent_on_success(self, mock_config, mock_order,
                                         mock_confirm, mock_alert,
                                         mock_state):
        """SELL alert should be sent when order succeeds (CLOSED)."""
        from src.orchestration.trade_executor import execute_sell
        mock_config.get.return_value = {}
        mock_order.return_value = {'status': 'CLOSED', 'pnl': 50.0}

        position = {'symbol': 'BTC', 'order_id': 'P_1', 'quantity': 0.01,
                     'status': 'OPEN', 'entry_price': 50000}
        signal = {'signal': 'SELL', 'symbol': 'BTC', 'current_price': 55000}

        run_async(execute_sell("BTC", signal, position, 55000.0))
        mock_alert.assert_called_once()
        mock_state.clear_trailing_stop.assert_called_once_with('P_1')
