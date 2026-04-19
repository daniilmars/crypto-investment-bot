# tests/test_position_analyst_auto.py
"""Tests for position analyst wired into auto trading loops."""

import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import patch, AsyncMock


def run_async(coro):
    """Helper to run async functions in sync tests."""
    return asyncio.run(coro)


def _make_position(symbol='BTC', order_id='A_1', entry_price=50000.0,
                   quantity=0.01, hours_ago=6):
    entry_ts = datetime.now(timezone.utc) - timedelta(hours=hours_ago)
    return {
        'symbol': symbol, 'order_id': order_id, 'entry_price': entry_price,
        'quantity': quantity, 'status': 'OPEN',
        'entry_timestamp': entry_ts.isoformat(),
    }


def _base_settings(enabled=True):
    return {
        'position_analyst': {
            'enabled': enabled,
            'check_interval_minutes': 0,  # no throttle
            'min_position_age_hours': 1,
            'increase_confidence_threshold': 0.75,
            'exit_confidence_threshold': 0.8,
            'max_position_multiplier': 3.0,
        },
    }


class TestAutoAnalystRouting:
    """Verifies that auto positions use auto bot_state variants."""

    @patch('src.orchestration.position_analyst.analyze_position_investment')
    @patch('src.orchestration.position_analyst.get_recent_articles', new_callable=AsyncMock, return_value=[])
    @patch('src.orchestration.position_analyst.get_position_additions', new_callable=AsyncMock, return_value=[])
    @patch('src.orchestration.position_analyst.compute_news_velocity')
    @patch('src.orchestration.position_analyst.bot_state')
    def test_auto_uses_auto_analyst_last_run(self, mock_state, mock_velocity,
                                              mock_additions, mock_articles,
                                              mock_gemini):
        """Auto positions should use get/set_auto_analyst_last_run."""
        from src.orchestration.position_analyst import run_position_analyst

        mock_state.get_auto_analyst_last_run.return_value = None
        mock_velocity.return_value = {
            'articles_last_4h': 1, 'breaking_detected': False,
            'sentiment_trend': 'stable',
        }
        mock_gemini.return_value = {
            'recommendation': 'hold', 'confidence': 0.5,
            'reasoning': 'ok', 'risk_level': 'green',
        }

        position = _make_position()
        result = run_async(run_position_analyst(
            position, 51000.0,
            {'current_price': 51000, 'sma': 50000, 'rsi': 55},
            _base_settings(), {},
            trailing_stop_activation=0.02,
            trading_strategy='auto'))

        assert result == 'hold'
        mock_state.get_auto_analyst_last_run.assert_called_with('A_1')
        mock_state.set_auto_analyst_last_run.assert_called_once()
        # Manual variants should NOT be called
        mock_state.get_analyst_last_run.assert_not_called()
        mock_state.set_analyst_last_run.assert_not_called()

    @patch('src.orchestration.position_analyst.analyze_position_investment')
    @patch('src.orchestration.position_analyst.get_recent_articles', new_callable=AsyncMock, return_value=[])
    @patch('src.orchestration.position_analyst.get_position_additions', new_callable=AsyncMock, return_value=[])
    @patch('src.orchestration.position_analyst.compute_news_velocity')
    @patch('src.orchestration.position_analyst.bot_state')
    def test_manual_uses_manual_analyst_last_run(self, mock_state, mock_velocity,
                                                  mock_additions, mock_articles,
                                                  mock_gemini):
        """Manual positions should use get/set_analyst_last_run (not auto)."""
        from src.orchestration.position_analyst import run_position_analyst

        mock_state.get_analyst_last_run.return_value = None
        mock_velocity.return_value = {
            'articles_last_4h': 1, 'breaking_detected': False,
            'sentiment_trend': 'stable',
        }
        mock_gemini.return_value = {
            'recommendation': 'hold', 'confidence': 0.5,
            'reasoning': 'ok', 'risk_level': 'green',
        }

        position = _make_position()
        result = run_async(run_position_analyst(
            position, 51000.0,
            {'current_price': 51000, 'sma': 50000, 'rsi': 55},
            _base_settings(), {},
            trailing_stop_activation=0.02,
            trading_strategy='manual'))

        assert result == 'hold'
        mock_state.get_analyst_last_run.assert_called_with('A_1')
        mock_state.get_auto_analyst_last_run.assert_not_called()


class TestAutoSellRouting:
    """Verifies auto SELL uses correct state + skips confirmation."""

    @patch('src.notify.telegram_periodic_summary.send_trade_alert', new_callable=AsyncMock)
    @patch('src.orchestration.position_analyst.place_order')
    @patch('src.orchestration.position_analyst.bot_state')
    def test_auto_sell_skips_confirmation(self, mock_state,
                                          mock_order, mock_alert):
        """Auto SELL should place order with trading_strategy='auto'."""
        from src.orchestration.position_analyst import _handle_analyst_sell

        position = _make_position()
        run_async(_handle_analyst_sell(
            position, 'BTC', 48000.0, 'A_1',
            0.85, 'bearish momentum', 'crypto', trading_strategy='auto'))

        # Should place order with trading_strategy='auto'
        mock_order.assert_called_once()
        call_kwargs = mock_order.call_args
        assert call_kwargs[1]['trading_strategy'] == 'auto'
        assert call_kwargs[1]['exit_reason'] == 'analyst_exit'

    @patch('src.notify.telegram_periodic_summary.send_trade_alert', new_callable=AsyncMock)
    @patch('src.orchestration.position_analyst.place_order')
    @patch('src.orchestration.position_analyst.bot_state')
    def test_auto_sell_clears_auto_state(self, mock_state,
                                          mock_order, mock_alert):
        """Auto SELL should clear trailing stop and auto analyst state."""
        from src.orchestration.position_analyst import _handle_analyst_sell

        position = _make_position()
        run_async(_handle_analyst_sell(
            position, 'BTC', 48000.0, 'A_1',
            0.85, 'bearish', 'crypto', trading_strategy='auto'))

        # Strategy-keyed clear is called now
        mock_state.strategy_clear_trailing_stop.assert_called_once_with('A_1', 'auto')
        mock_state.remove_auto_analyst_last_run.assert_called_once_with('A_1')

    @patch('src.notify.telegram_periodic_summary.send_trade_alert', new_callable=AsyncMock)
    @patch('src.orchestration.position_analyst.place_order')
    @patch('src.orchestration.position_analyst.bot_state')
    def test_auto_sell_alert_called(self, mock_state,
                                     mock_order, mock_alert):
        """Auto SELL should send a trade alert with strategy + reasoning."""
        from src.orchestration.position_analyst import _handle_analyst_sell

        position = _make_position()
        run_async(_handle_analyst_sell(
            position, 'BTC', 48000.0, 'A_1',
            0.90, 'bearish reversal', 'crypto', trading_strategy='auto'))

        mock_alert.assert_called_once()
        kwargs = mock_alert.call_args[1]
        assert kwargs['action'] == 'SELL'
        assert kwargs['trading_strategy'] == 'auto'

class TestAutoIncreaseRouting:
    """Verifies auto INCREASE uses correct balance pool + skips confirmation."""

    @patch('src.orchestration.position_analyst.add_to_position')
    @patch('src.orchestration.position_analyst.get_account_balance')
    def test_auto_increase_skips_confirmation(self, mock_balance, mock_add):
        """Auto INCREASE should skip confirmation and add directly."""
        from src.orchestration.position_analyst import _handle_increase

        mock_balance.return_value = {'USDT': 5000}
        position = _make_position(entry_price=50000, quantity=0.01)
        result_dict = {
            'increase_sizing_hint': 'small', 'recommendation': 'increase',
            'confidence': 0.8,
        }
        analyst_cfg = {'max_position_multiplier': 3.0}

        run_async(_handle_increase(
            position, 'BTC', 51000.0, 50000.0,
            [], result_dict, 'bullish momentum', analyst_cfg, 'crypto',
            is_auto=True))

        # Should call add_to_position with trading_strategy='auto'
        mock_add.assert_called_once()
        assert mock_add.call_args[1]['trading_strategy'] == 'auto'

    @patch('src.orchestration.position_analyst.add_to_position')
    @patch('src.orchestration.position_analyst.get_account_balance')
    def test_auto_increase_uses_auto_balance_pool(self, mock_balance, mock_add):
        """Auto INCREASE should query balance with trading_strategy='auto'."""
        from src.orchestration.position_analyst import _handle_increase

        mock_balance.return_value = {'USDT': 5000}
        position = _make_position(entry_price=50000, quantity=0.01)
        result_dict = {'increase_sizing_hint': 'small'}
        analyst_cfg = {'max_position_multiplier': 3.0}

        run_async(_handle_increase(
            position, 'BTC', 51000.0, 50000.0,
            [], result_dict, 'bullish', analyst_cfg, 'crypto',
            is_auto=True))

        mock_balance.assert_called_once_with(asset_type='crypto', trading_strategy='auto')


class TestAutoTrailingStopInfo:
    """Verifies _build_trailing_stop_info routes to auto peak."""

    @patch('src.orchestration.position_analyst.bot_state')
    def test_auto_uses_get_auto_peak(self, mock_state):
        from src.orchestration.position_analyst import _build_trailing_stop_info

        mock_state.get_auto_peak.return_value = 55000.0
        result = _build_trailing_stop_info('A_1', 0.05, 0.02, is_auto=True)

        assert result is not None
        assert result['peak_price'] == 55000.0
        mock_state.get_auto_peak.assert_called_once_with('A_1')
        mock_state.get_peak.assert_not_called()

    @patch('src.orchestration.position_analyst.bot_state')
    def test_manual_uses_get_peak(self, mock_state):
        from src.orchestration.position_analyst import _build_trailing_stop_info

        mock_state.get_peak.return_value = 55000.0
        result = _build_trailing_stop_info('P_1', 0.05, 0.02, is_auto=False)

        assert result is not None
        assert result['peak_price'] == 55000.0
        mock_state.get_peak.assert_called_once_with('P_1')
        mock_state.get_auto_peak.assert_not_called()


class TestAutoSignalCooldown:
    """Verifies auto positions use auto signal cooldown."""

    @patch('src.orchestration.position_analyst.app_config')
    @patch('src.orchestration.position_analyst.send_telegram_alert', new_callable=AsyncMock)
    @patch('src.orchestration.position_analyst.add_to_position')
    @patch('src.orchestration.position_analyst.get_account_balance',
           return_value={'USDT': 10000})
    @patch('src.orchestration.position_analyst.analyze_position_investment')
    @patch('src.orchestration.position_analyst.get_recent_articles',
           new_callable=AsyncMock, return_value=[])
    @patch('src.orchestration.position_analyst.get_position_additions',
           new_callable=AsyncMock, return_value=[])
    @patch('src.orchestration.position_analyst.compute_news_velocity')
    @patch('src.orchestration.position_analyst.check_signal_cooldown',
           new_callable=AsyncMock, return_value=False)
    @patch('src.orchestration.position_analyst.bot_state')
    def test_auto_increase_sets_auto_signal_cooldown(
            self, mock_state, mock_cd_check, mock_velocity,
            mock_additions, mock_articles, mock_gemini, mock_balance,
            mock_add, mock_alert, mock_config):
        """Auto INCREASE should set auto signal cooldown, not manual."""
        from src.orchestration.position_analyst import run_position_analyst

        mock_state.get_auto_analyst_last_run.return_value = None
        mock_config.get.return_value = {'signal_cooldown_hours': 4}
        mock_velocity.return_value = {
            'articles_last_4h': 2, 'breaking_detected': False,
            'sentiment_trend': 'bullish',
        }
        mock_gemini.return_value = {
            'recommendation': 'increase', 'confidence': 0.85,
            'reasoning': 'strong', 'risk_level': 'green',
            'increase_sizing_hint': 'small',
        }

        position = _make_position()
        run_async(run_position_analyst(
            position, 52000.0,
            {'current_price': 52000, 'sma': 50000, 'rsi': 55},
            _base_settings(), {},
            trailing_stop_activation=0.02,
            trading_strategy='auto'))

        # check_signal_cooldown called with is_auto=True
        mock_cd_check.assert_called_once_with('BTC', 'INCREASE', 4, is_auto=True)
        # set_auto_signal_cooldown called (not manual)
        mock_state.set_auto_signal_cooldown.assert_called_once()
        mock_state.set_signal_cooldown.assert_not_called()
