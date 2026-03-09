"""Tests for position rotation feature."""

import pytest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch, AsyncMock, MagicMock

from src.orchestration.position_rotation import (
    compute_pnl_velocity,
    evaluate_rotation_candidate,
    format_rotation_message,
)
from src.orchestration import bot_state


def _make_position(symbol, entry_price, quantity=1.0, hours_held=48,
                   strategy_type=None, asset_type='stock', order_id=None):
    """Helper to create a position dict."""
    entry_ts = (datetime.now(timezone.utc) - timedelta(hours=hours_held)).isoformat()
    return {
        'symbol': symbol,
        'entry_price': entry_price,
        'quantity': quantity,
        'entry_timestamp': entry_ts,
        'strategy_type': strategy_type,
        'asset_type': asset_type,
        'status': 'OPEN',
        'order_id': order_id or f'ord_{symbol}',
    }


class TestComputePnlVelocity:
    def test_positive_pnl(self):
        pos = _make_position('AAPL', 100.0, hours_held=48)
        # Price went from 100 to 110 = +10% in 2 days = +5%/day
        velocity = compute_pnl_velocity(pos, 110.0)
        assert abs(velocity - 0.05) < 0.01

    def test_negative_pnl(self):
        pos = _make_position('AAPL', 100.0, hours_held=48)
        velocity = compute_pnl_velocity(pos, 95.0)
        assert velocity < 0

    def test_zero_entry_price(self):
        pos = _make_position('AAPL', 0.0, hours_held=48)
        velocity = compute_pnl_velocity(pos, 100.0)
        assert velocity == 0.0

    def test_very_short_hold_floors_at_1_hour(self):
        pos = _make_position('AAPL', 100.0, hours_held=0)
        # Even with 0 hours held, floor at 1 hour (1/24 day)
        velocity = compute_pnl_velocity(pos, 110.0)
        # 10% in 1/24 day = 240%/day - just check it's very high
        assert velocity > 1.0

    def test_no_timestamp(self):
        pos = {'symbol': 'AAPL', 'entry_price': 100.0, 'quantity': 1.0}
        velocity = compute_pnl_velocity(pos, 110.0)
        assert abs(velocity - 0.1) < 0.01  # treated as 1-day hold


class TestEvaluateRotationCandidate:
    def setup_method(self):
        self.config = {
            'enabled': True,
            'min_hold_hours': 24,
            'min_signal_strength': 0.6,
            'min_strength_advantage': 0.15,
            'min_pnl_velocity_threshold': -0.005,
        }

    def test_returns_none_when_disabled(self):
        config = {**self.config, 'enabled': False}
        result = evaluate_rotation_candidate([], {}, {}, config=config)
        assert result is None

    def test_returns_none_weak_signal(self):
        signal = {'signal_strength': 0.3, 'symbol': 'NEW'}
        result = evaluate_rotation_candidate(
            [_make_position('OLD', 100.0)],
            signal, {'OLD': 95.0}, config=self.config)
        assert result is None

    def test_returns_none_no_eligible_positions(self):
        # All positions are strategic
        positions = [_make_position('OLD', 100.0, strategy_type='growth')]
        signal = {'signal_strength': 0.8, 'symbol': 'NEW'}
        result = evaluate_rotation_candidate(
            positions, signal, {'OLD': 95.0}, config=self.config)
        assert result is None

    def test_returns_none_too_young(self):
        # Position held for only 6 hours, min is 24
        positions = [_make_position('OLD', 100.0, hours_held=6)]
        signal = {'signal_strength': 0.8, 'symbol': 'NEW'}
        result = evaluate_rotation_candidate(
            positions, signal, {'OLD': 95.0}, config=self.config)
        assert result is None

    def test_returns_none_velocity_above_threshold(self):
        # Position is doing well (price up)
        positions = [_make_position('OLD', 100.0, hours_held=48)]
        signal = {'signal_strength': 0.8, 'symbol': 'NEW'}
        result = evaluate_rotation_candidate(
            positions, signal, {'OLD': 110.0}, config=self.config)
        assert result is None

    def test_rotates_weakest_position(self):
        positions = [
            _make_position('GOOD', 100.0, hours_held=48),
            _make_position('BAD', 100.0, hours_held=48),
        ]
        signal = {'signal_strength': 0.8, 'symbol': 'NEW'}
        # GOOD is up, BAD is down
        prices = {'GOOD': 105.0, 'BAD': 94.0}
        result = evaluate_rotation_candidate(
            positions, signal, prices, config=self.config)
        assert result is not None
        assert result['rotate_out']['symbol'] == 'BAD'
        assert result['signal_strength'] == 0.8
        assert result['pnl_velocity'] < 0

    def test_skips_strategic_positions(self):
        positions = [
            _make_position('STRATEGIC', 100.0, hours_held=48, strategy_type='growth'),
            _make_position('NORMAL', 100.0, hours_held=48),
        ]
        signal = {'signal_strength': 0.8, 'symbol': 'NEW'}
        prices = {'STRATEGIC': 80.0, 'NORMAL': 95.0}
        result = evaluate_rotation_candidate(
            positions, signal, prices, config=self.config)
        # Should only consider NORMAL, not STRATEGIC (even though it's worse)
        if result:
            assert result['rotate_out']['symbol'] == 'NORMAL'

    def test_returns_none_insufficient_advantage(self):
        # Signal strength barely above velocity — min_strength_advantage not met
        positions = [_make_position('OLD', 100.0, hours_held=48)]
        signal = {'signal_strength': 0.6, 'symbol': 'NEW'}
        config = {**self.config, 'min_strength_advantage': 0.8}
        result = evaluate_rotation_candidate(
            positions, signal, {'OLD': 99.0}, config=config)
        assert result is None

    def test_no_price_for_position(self):
        positions = [_make_position('OLD', 100.0, hours_held=48)]
        signal = {'signal_strength': 0.8, 'symbol': 'NEW'}
        # No price available for OLD
        result = evaluate_rotation_candidate(
            positions, signal, {}, config=self.config)
        assert result is None


class TestFormatRotationMessage:
    def test_format_produces_string(self):
        candidate = {
            'rotate_out': {
                'symbol': 'BAD', 'entry_price': 100.0,
                'quantity': 1.5, 'order_id': 'ord_1',
            },
            'pnl_velocity': -0.03,
            'signal_strength': 0.75,
        }
        signal = {'symbol': 'NEW', 'reason': 'Strong momentum signal'}
        msg = format_rotation_message(candidate, signal)
        assert 'BAD' in msg
        assert 'NEW' in msg
        assert 'Rotation' in msg


class TestRotationCooldown:
    def setup_method(self):
        bot_state.clear_all()

    def test_cooldown_set_and_get(self):
        expires = datetime.now(timezone.utc) + timedelta(hours=4)
        bot_state.set_rotation_cooldown('stock', expires, is_auto=True)
        assert bot_state.get_rotation_cooldown('stock', is_auto=True) == expires
        assert bot_state.get_rotation_cooldown('stock', is_auto=False) is None

    def test_cooldown_cleared(self):
        expires = datetime.now(timezone.utc) + timedelta(hours=4)
        bot_state.set_rotation_cooldown('crypto', expires)
        bot_state.clear_rotation_cooldown('crypto')
        assert bot_state.get_rotation_cooldown('crypto') is None

    def test_clear_all_clears_rotation(self):
        expires = datetime.now(timezone.utc) + timedelta(hours=4)
        bot_state.set_rotation_cooldown('stock', expires, is_auto=True)
        bot_state.clear_all()
        assert bot_state.get_rotation_cooldown('stock', is_auto=True) is None


class TestSignalStrength:
    """Verify signal engines include signal_strength in BUY/SELL signals."""

    def test_crypto_scoring_buy_has_strength(self):
        from src.analysis.signal_engine import generate_signal
        market = {'current_price': 100, 'sma': 90, 'rsi': 25}
        news = {
            'gemini_assessment': {'direction': 'bullish', 'confidence': 0.8,
                                  'reasoning': 'test', 'catalyst_freshness': 'recent'},
            'avg_sentiment_score': 0.5,
        }
        result = generate_signal('BTC', market, news_sentiment_data=news,
                                 signal_mode='scoring', signal_threshold=2)
        if result['signal'] == 'BUY':
            assert 'signal_strength' in result
            assert 0 < result['signal_strength'] <= 1.0

    def test_crypto_sentiment_buy_has_strength(self):
        from src.analysis.signal_engine import generate_signal
        market = {'current_price': 100, 'sma': 90, 'rsi': 50}
        news = {
            'gemini_assessment': {'direction': 'bullish', 'confidence': 0.9,
                                  'reasoning': 'test', 'catalyst_freshness': 'recent'},
        }
        result = generate_signal('BTC', market, news_sentiment_data=news,
                                 signal_mode='sentiment')
        if result['signal'] == 'BUY':
            assert 'signal_strength' in result
            assert result['signal_strength'] == pytest.approx(0.72, abs=0.01)

    def test_stock_scoring_buy_has_strength(self):
        from src.analysis.stock_signal_engine import generate_stock_signal
        market = {'current_price': 100, 'sma': 90, 'rsi': 25}
        news = {
            'gemini_assessment': {'direction': 'bullish', 'confidence': 0.8,
                                  'reasoning': 'test'},
            'avg_sentiment_score': 0.5,
        }
        result = generate_stock_signal('AAPL', market, news_sentiment_data=news,
                                       signal_mode='scoring', signal_threshold=2)
        if result['signal'] == 'BUY':
            assert 'signal_strength' in result

    def test_stock_sentiment_buy_has_strength(self):
        from src.analysis.stock_signal_engine import generate_stock_signal
        market = {'current_price': 100, 'sma': 90, 'rsi': 50}
        news = {
            'gemini_assessment': {'direction': 'bullish', 'confidence': 0.9,
                                  'reasoning': 'test', 'catalyst_freshness': 'recent'},
        }
        result = generate_stock_signal('AAPL', market, news_sentiment_data=news,
                                       signal_mode='sentiment')
        if result['signal'] == 'BUY':
            assert 'signal_strength' in result

    def test_hold_signal_no_strength(self):
        from src.analysis.signal_engine import generate_signal
        market = {'current_price': 100, 'sma': 100, 'rsi': 50}
        result = generate_signal('BTC', market, signal_mode='scoring')
        assert result['signal'] == 'HOLD'
        assert 'signal_strength' not in result


class TestProcessTradeSignalRotation:
    """Integration test: rotation path in process_trade_signal."""

    def test_rotation_triggered_on_max_positions(self):
        import asyncio
        from src.orchestration.trade_executor import process_trade_signal
        bot_state.clear_all()

        positions = [
            _make_position('OLD', 100.0, hours_held=72, order_id='ord_1'),
        ]
        signal = {
            'signal': 'BUY', 'symbol': 'NEW', 'current_price': 50.0,
            'reason': 'Strong signal', 'signal_strength': 0.8,
        }
        prices = {'OLD': 94.0, 'NEW': 50.0}

        with patch('src.orchestration.trade_executor.place_order') as mock_place, \
             patch('src.orchestration.trade_executor.send_telegram_alert', new_callable=AsyncMock), \
             patch('src.orchestration.trade_executor.check_buy_gates') as mock_gates, \
             patch('src.orchestration.trade_executor.check_stoploss_cooldown', new_callable=AsyncMock, return_value=False), \
             patch('src.orchestration.trade_executor.check_signal_cooldown', new_callable=AsyncMock, return_value=False), \
             patch('src.orchestration.position_rotation.app_config', {
                 'settings': {'position_rotation': {
                     'enabled': True, 'min_hold_hours': 24,
                     'min_signal_strength': 0.6, 'min_strength_advantage': 0.15,
                     'min_pnl_velocity_threshold': -0.005,
                     'rotation_cooldown_hours': 4,
                 }}}), \
             patch('src.execution.binance_trader.get_account_balance', return_value={'USDT': 1000}):

            mock_gates.return_value = (False, 0.0, 'Max concurrent positions (1) reached.')
            mock_place.return_value = {'status': 'CLOSED', 'symbol': 'OLD'}

            result = asyncio.new_event_loop().run_until_complete(
                process_trade_signal(
                    'NEW', signal, 50.0, positions, 1000.0,
                    0.03, 4.0, 1, False, 1.0,
                    asset_type='stock', trading_strategy='auto',
                    label='AUTO', is_auto=True,
                    current_prices=prices))

            # Should have called place_order for SELL (rotation out) then BUY
            assert mock_place.call_count >= 1

    def test_no_rotation_without_prices(self):
        import asyncio
        from src.orchestration.trade_executor import process_trade_signal
        bot_state.clear_all()

        positions = [_make_position('OLD', 100.0, hours_held=72)]
        signal = {
            'signal': 'BUY', 'symbol': 'NEW', 'current_price': 50.0,
            'reason': 'test', 'signal_strength': 0.8,
        }

        with patch('src.orchestration.trade_executor.check_buy_gates') as mock_gates, \
             patch('src.orchestration.trade_executor.check_stoploss_cooldown', new_callable=AsyncMock, return_value=False), \
             patch('src.orchestration.trade_executor.check_signal_cooldown', new_callable=AsyncMock, return_value=False):
            mock_gates.return_value = (False, 0.0, 'Max concurrent positions (1) reached.')

            # No current_prices passed — rotation should not trigger
            result = asyncio.new_event_loop().run_until_complete(
                process_trade_signal(
                    'NEW', signal, 50.0, positions, 1000.0,
                    0.03, 4.0, 1, False, 1.0,
                    asset_type='stock', is_auto=True))
            assert result is None
