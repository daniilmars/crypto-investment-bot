"""Tests for Flash analyst tier — exit-only gemini-2.5-flash checks every 4h."""

import asyncio
import json
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

from src.orchestration import bot_state


def run_async(coro):
    return asyncio.run(coro)


# -- Helpers --

def _make_position(**overrides):
    base = {
        'symbol': 'AAPL',
        'entry_price': 200.0,
        'quantity': 10,
        'order_id': 'test-order-1',
        'entry_timestamp': (datetime.now(timezone.utc) - timedelta(hours=6)).isoformat(),
    }
    base.update(overrides)
    return base


def _make_settings(**analyst_overrides):
    analyst = {
        'enabled': True,
        'min_position_age_hours': 2,
        'quick_check_interval_minutes': 240,
        'check_interval_minutes': 1440,
        'exit_confidence_threshold': 0.8,
        'increase_confidence_threshold': 0.75,
        'max_position_multiplier': 3.0,
    }
    analyst.update(analyst_overrides)
    return {'position_analyst': analyst}


# -- analyze_position_quick tests --

class TestAnalyzePositionQuick:

    @patch.dict('os.environ', {}, clear=True)
    def test_quick_returns_none_without_gcp(self):
        """GCP_PROJECT_ID not set → returns None."""
        from src.analysis.gemini_news_analyzer import analyze_position_quick
        result = analyze_position_quick(
            _make_position(), 210.0, [], {'rsi': 50}, {'articles_last_4h': 0},
        )
        assert result is None

    @patch.dict('os.environ', {'GCP_PROJECT_ID': 'test-project'})
    @patch('src.analysis.gemini_news_analyzer.vertexai')
    @patch('src.analysis.gemini_news_analyzer.GenerativeModel')
    def test_quick_returns_hold(self, mock_model_cls, mock_vertexai):
        """Flash model returns hold → parsed correctly."""
        from src.analysis.gemini_news_analyzer import analyze_position_quick
        mock_model = MagicMock()
        mock_model_cls.return_value = mock_model
        mock_response = MagicMock()
        mock_response.text = json.dumps({
            'action': 'hold', 'confidence': 0.3, 'reason': 'No adverse catalysts',
        })
        mock_model.generate_content.return_value = mock_response

        result = analyze_position_quick(
            _make_position(), 205.0, [], {'rsi': 55},
            {'articles_last_4h': 2, 'sentiment_trend': 'stable',
             'velocity_status': 'normal', 'breaking_detected': False},
        )
        assert result is not None
        assert result['action'] == 'hold'
        assert result['confidence'] == 0.3

    @patch.dict('os.environ', {'GCP_PROJECT_ID': 'test-project'})
    @patch('src.analysis.gemini_news_analyzer.vertexai')
    @patch('src.analysis.gemini_news_analyzer.GenerativeModel')
    def test_quick_returns_exit(self, mock_model_cls, mock_vertexai):
        """Flash model returns exit → parsed correctly."""
        from src.analysis.gemini_news_analyzer import analyze_position_quick
        mock_model = MagicMock()
        mock_model_cls.return_value = mock_model
        mock_response = MagicMock()
        mock_response.text = json.dumps({
            'action': 'exit', 'confidence': 0.9, 'reason': 'Regulatory ban announced',
        })
        mock_model.generate_content.return_value = mock_response

        result = analyze_position_quick(
            _make_position(), 180.0, [], {'rsi': 75},
            {'articles_last_4h': 5, 'sentiment_trend': 'declining',
             'velocity_status': 'high', 'breaking_detected': True},
        )
        assert result is not None
        assert result['action'] == 'exit'
        assert result['confidence'] == 0.9

    @patch.dict('os.environ', {'GCP_PROJECT_ID': 'test-project'})
    @patch('src.analysis.gemini_news_analyzer.vertexai')
    @patch('src.analysis.gemini_news_analyzer.GenerativeModel')
    def test_quick_uses_flash_model(self, mock_model_cls, mock_vertexai):
        """Verify Flash analyst uses gemini-2.5-flash, not Pro."""
        from src.analysis.gemini_news_analyzer import analyze_position_quick
        mock_model = MagicMock()
        mock_model_cls.return_value = mock_model
        mock_response = MagicMock()
        mock_response.text = json.dumps({
            'action': 'hold', 'confidence': 0.2, 'reason': 'OK',
        })
        mock_model.generate_content.return_value = mock_response

        analyze_position_quick(
            _make_position(), 200.0, [], {'rsi': 50},
            {'articles_last_4h': 1, 'sentiment_trend': 'stable',
             'velocity_status': 'normal', 'breaking_detected': False},
        )
        mock_model_cls.assert_called_with('gemini-2.5-flash')


# -- _run_flash_analyst integration tests --

class TestRunFlashAnalyst:

    def setup_method(self):
        bot_state.clear_all()

    def test_flash_uses_separate_interval(self):
        """Flash analyst uses quick_check_interval_minutes, not check_interval_minutes."""
        from src.orchestration.position_analyst import _run_flash_analyst

        position = _make_position()
        settings = _make_settings()
        order_id = position['order_id']

        # Set Flash last run to 3h ago (< 4h interval) → should skip
        bot_state.set_flash_analyst_last_run(
            order_id, datetime.now(timezone.utc) - timedelta(hours=3))

        result = run_async(_run_flash_analyst(
            position, 'AAPL', 205.0, 200.0, 0.025,
            order_id, {'rsi': 50}, settings['position_analyst'], settings,
            0.05, 'stock', False,
        ))
        assert result is None  # Skipped due to interval

    def test_flash_skips_when_no_news(self):
        """Flash analyst returns hold (no API call) when news velocity is zero."""
        from src.orchestration.position_analyst import _run_flash_analyst

        position = _make_position()
        settings = _make_settings()

        with patch('src.orchestration.position_analyst.compute_news_velocity') as mock_vel:
            mock_vel.return_value = {
                'articles_last_1h': 0, 'articles_last_4h': 0,
                'articles_last_24h': 0, 'breaking_detected': False,
                'sentiment_trend': 'stable', 'velocity_status': 'normal',
            }
            result = run_async(_run_flash_analyst(
                position, 'AAPL', 205.0, 200.0, 0.025,
                position['order_id'], {'rsi': 50},
                settings['position_analyst'], settings,
                0.05, 'stock', False,
            ))
        assert result == 'hold'

    def test_flash_exit_triggers_sell(self):
        """Flash exit with high confidence calls _handle_analyst_sell."""
        from src.orchestration.position_analyst import _run_flash_analyst

        position = _make_position()
        settings = _make_settings()

        with patch('src.orchestration.position_analyst.compute_news_velocity') as mock_vel, \
             patch('src.orchestration.position_analyst.get_recent_articles', new_callable=AsyncMock) as mock_art, \
             patch('src.orchestration.position_analyst.analyze_position_quick') as mock_quick, \
             patch('src.orchestration.position_analyst._handle_analyst_sell', new_callable=AsyncMock) as mock_sell:
            mock_vel.return_value = {
                'articles_last_4h': 3, 'breaking_detected': True,
                'sentiment_trend': 'declining', 'velocity_status': 'high',
                'articles_last_1h': 2, 'articles_last_24h': 10,
            }
            mock_art.return_value = []
            mock_quick.return_value = {
                'action': 'exit', 'confidence': 0.9, 'reason': 'Regulatory crackdown',
            }
            result = run_async(_run_flash_analyst(
                position, 'AAPL', 180.0, 200.0, -0.10,
                position['order_id'], {'rsi': 75},
                settings['position_analyst'], settings,
                0.05, 'stock', False,
            ))
            assert result == 'exit'
            mock_sell.assert_called_once()

    def test_flash_hold_allows_pro_to_run(self):
        """Flash hold doesn't block Pro analyst from running."""
        from src.orchestration.position_analyst import run_position_analyst

        position = _make_position()
        settings = _make_settings()

        with patch('src.orchestration.position_analyst._run_flash_analyst', new_callable=AsyncMock) as mock_flash, \
             patch('src.orchestration.position_analyst._run_investment_analyst', new_callable=AsyncMock) as mock_pro:
            mock_flash.return_value = 'hold'
            mock_pro.return_value = 'hold'

            run_async(run_position_analyst(
                position, 205.0, {'rsi': 50}, settings, {},
                trailing_stop_activation=0.05,
                asset_type='stock',
            ))
            mock_flash.assert_called_once()
            mock_pro.assert_called_once()

    def test_flash_state_isolated_from_pro(self):
        """Flash and Pro use separate bot_state dicts."""
        order_id = 'test-order-1'
        now = datetime.now(timezone.utc)

        bot_state.set_flash_analyst_last_run(order_id, now)
        bot_state.set_analyst_last_run(order_id, now - timedelta(hours=12))

        flash_ts = bot_state.get_flash_analyst_last_run(order_id)
        pro_ts = bot_state.get_analyst_last_run(order_id)

        assert flash_ts == now
        assert pro_ts == now - timedelta(hours=12)
        assert flash_ts != pro_ts

    def test_cleanup_clears_flash_state(self):
        """Position close clears Flash analyst state."""
        from src.orchestration.position_monitor import _cleanup_position_state

        order_id = 'test-order-1'
        now = datetime.now(timezone.utc)

        # Set state for both manual and auto
        bot_state.set_flash_analyst_last_run(order_id, now)
        bot_state.set_auto_flash_analyst_last_run(order_id, now)

        # Cleanup manual
        _cleanup_position_state(order_id, is_auto=False)
        assert bot_state.get_flash_analyst_last_run(order_id) is None
        # Auto state should still exist
        assert bot_state.get_auto_flash_analyst_last_run(order_id) == now

        # Cleanup auto
        _cleanup_position_state(order_id, is_auto=True)
        assert bot_state.get_auto_flash_analyst_last_run(order_id) is None

    def test_clear_all_clears_flash_state(self):
        """bot_state.clear_all() clears Flash analyst dicts."""
        order_id = 'test-order-1'
        now = datetime.now(timezone.utc)

        bot_state.set_flash_analyst_last_run(order_id, now)
        bot_state.set_auto_flash_analyst_last_run(order_id, now)

        bot_state.clear_all()

        assert bot_state.get_flash_analyst_last_run(order_id) is None
        assert bot_state.get_auto_flash_analyst_last_run(order_id) is None
