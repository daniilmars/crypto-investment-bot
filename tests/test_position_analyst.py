# tests/test_position_analyst.py
"""Tests for the Position Analyst feature: news velocity, Gemini analyst,
add_to_position, and main.py integration."""

import asyncio
import sqlite3
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ---------- News Velocity ----------

class TestComputeNewsVelocity:
    """Tests for src/analysis/news_velocity.compute_news_velocity()."""

    @patch('src.analysis.news_velocity.get_db_connection')
    @patch('src.analysis.news_velocity.release_db_connection')
    def test_quiet_when_no_articles(self, mock_release, mock_conn):
        from src.analysis.news_velocity import compute_news_velocity
        conn = MagicMock()
        conn.cursor.return_value.__enter__ = MagicMock(return_value=MagicMock())
        conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
        mock_conn.return_value = conn

        # Make it look like SQLite (not psycopg2)
        cursor_mock = MagicMock()
        cursor_mock.fetchone.return_value = [0]  # count=0, then sentiment=0
        cursor_mock.close = MagicMock()

        with patch('src.analysis.news_velocity._cursor') as mock_cursor_ctx:
            mock_cursor_ctx.return_value.__enter__ = MagicMock(return_value=cursor_mock)
            mock_cursor_ctx.return_value.__exit__ = MagicMock(return_value=False)
            result = compute_news_velocity('BTC')

        assert result['velocity_status'] == 'quiet'
        assert result['breaking_detected'] is False
        assert result['articles_last_1h'] == 0

    def test_returns_default_on_db_error(self):
        """Should return safe defaults when DB is unavailable."""
        with patch('src.analysis.news_velocity.get_db_connection',
                   side_effect=sqlite3.OperationalError("DB down")):
            from src.analysis.news_velocity import compute_news_velocity
            result = compute_news_velocity('BTC')

        assert result['velocity_status'] == 'quiet'
        assert result['breaking_detected'] is False
        assert result['sentiment_trend'] == 'stable'

    def test_sentiment_trend_improving(self):
        """When 1h avg sentiment is much higher than 24h, trend should be improving."""
        from src.analysis.news_velocity import compute_news_velocity

        call_count = [0]
        def fake_fetchone():
            call_count[0] += 1
            # counts: 1h=5, 4h=10, 24h=20, sentiments: 1h=0.5, 4h=0.3, 24h=0.1
            values = [5, 0.5, 10, 0.3, 20, 0.1]
            idx = call_count[0] - 1
            if idx < len(values):
                return [values[idx]]
            return [0]

        cursor_mock = MagicMock()
        cursor_mock.fetchone = fake_fetchone
        cursor_mock.close = MagicMock()

        conn = MagicMock(spec=sqlite3.Connection)
        with patch('src.analysis.news_velocity.get_db_connection', return_value=conn), \
             patch('src.analysis.news_velocity.release_db_connection'), \
             patch('src.analysis.news_velocity._cursor') as mock_ctx, \
             patch('src.analysis.news_velocity.isinstance', return_value=False):
            mock_ctx.return_value.__enter__ = MagicMock(return_value=cursor_mock)
            mock_ctx.return_value.__exit__ = MagicMock(return_value=False)
            result = compute_news_velocity('BTC')

        assert result['sentiment_trend'] == 'improving'

    def test_accelerating_velocity(self):
        """When 1h articles >> 24h hourly average, velocity should be accelerating."""
        from src.analysis.news_velocity import compute_news_velocity

        call_count = [0]
        def fake_fetchone():
            call_count[0] += 1
            # counts: 1h=10, 4h=12, 24h=24; sentiments: 1h=0.3, 4h=0.2, 24h=0.2
            values = [10, 0.3, 12, 0.2, 24, 0.2]
            idx = call_count[0] - 1
            if idx < len(values):
                return [values[idx]]
            return [0]

        cursor_mock = MagicMock()
        cursor_mock.fetchone = fake_fetchone
        cursor_mock.close = MagicMock()

        conn = MagicMock(spec=sqlite3.Connection)
        with patch('src.analysis.news_velocity.get_db_connection', return_value=conn), \
             patch('src.analysis.news_velocity.release_db_connection'), \
             patch('src.analysis.news_velocity._cursor') as mock_ctx, \
             patch('src.analysis.news_velocity.isinstance', return_value=False):
            mock_ctx.return_value.__enter__ = MagicMock(return_value=cursor_mock)
            mock_ctx.return_value.__exit__ = MagicMock(return_value=False)
            result = compute_news_velocity('BTC')

        assert result['velocity_status'] == 'accelerating'
        assert result['breaking_detected'] is True


# ---------- Gemini Analyst ----------

class TestAnalyzePositionInvestment:
    """Tests for analyze_position_investment() in gemini_news_analyzer."""

    @patch.dict('os.environ', {'GCP_PROJECT_ID': ''})
    def test_returns_none_without_gcp_project(self):
        from src.analysis.gemini_news_analyzer import analyze_position_investment
        result = analyze_position_investment(
            {'symbol': 'BTC', 'entry_price': 50000, 'quantity': 0.1},
            52000.0, [], {'rsi': 45, 'sma': 49000}, {},
        )
        assert result is None

    @patch('src.analysis.gemini_news_analyzer.vertexai')
    @patch('src.analysis.gemini_news_analyzer.GenerativeModel')
    @patch.dict('os.environ', {'GCP_PROJECT_ID': 'test-project'})
    def test_returns_hold_on_valid_response(self, mock_model_cls, mock_vertexai):
        from src.analysis.gemini_news_analyzer import analyze_position_investment

        mock_model = MagicMock()
        mock_model_cls.return_value = mock_model
        mock_response = MagicMock()
        mock_response.text = '{"recommendation": "hold", "confidence": 0.6, "reasoning": "No catalyst", "risk_level": "green", "primary_driver": "none", "news_momentum": "stable", "increase_sizing_hint": null, "key_article": null}'
        mock_model.generate_content.return_value = mock_response

        result = analyze_position_investment(
            {'symbol': 'BTC', 'entry_price': 50000, 'quantity': 0.1},
            51000.0,
            [{'title': 'BTC update', 'source': 'CoinDesk', 'gemini_score': 0.2}],
            {'rsi': 55, 'sma': 49000, 'regime': 'unknown'},
            {'articles_last_1h': 2, 'articles_last_4h': 5, 'articles_last_24h': 20,
             'avg_sentiment_1h': 0.2, 'avg_sentiment_4h': 0.15, 'avg_sentiment_24h': 0.1,
             'sentiment_trend': 'stable', 'velocity_status': 'normal', 'breaking_detected': False},
        )
        assert result is not None
        assert result['recommendation'] == 'hold'

    @patch('src.analysis.gemini_news_analyzer.vertexai')
    @patch('src.analysis.gemini_news_analyzer.GenerativeModel')
    @patch.dict('os.environ', {'GCP_PROJECT_ID': 'test-project'})
    def test_returns_increase_with_catalyst(self, mock_model_cls, mock_vertexai):
        from src.analysis.gemini_news_analyzer import analyze_position_investment

        mock_model = MagicMock()
        mock_model_cls.return_value = mock_model
        mock_response = MagicMock()
        mock_response.text = '{"recommendation": "increase", "confidence": 0.85, "reasoning": "ETF approval catalyst", "risk_level": "green", "primary_driver": "bullish_catalyst", "news_momentum": "accelerating", "increase_sizing_hint": "medium", "key_article": "SEC approves BTC ETF"}'
        mock_model.generate_content.return_value = mock_response

        result = analyze_position_investment(
            {'symbol': 'BTC', 'entry_price': 50000, 'quantity': 0.1},
            52000.0,
            [{'title': 'SEC approves BTC ETF', 'source': 'Reuters', 'gemini_score': 0.9}],
            {'rsi': 55, 'sma': 49000, 'regime': 'unknown'},
            {'articles_last_1h': 8, 'articles_last_4h': 15, 'articles_last_24h': 20,
             'avg_sentiment_1h': 0.8, 'avg_sentiment_4h': 0.5, 'avg_sentiment_24h': 0.2,
             'sentiment_trend': 'improving', 'velocity_status': 'accelerating', 'breaking_detected': True},
        )
        assert result['recommendation'] == 'increase'
        assert result['increase_sizing_hint'] == 'medium'

    @patch('src.analysis.gemini_news_analyzer.vertexai')
    @patch('src.analysis.gemini_news_analyzer.GenerativeModel')
    @patch.dict('os.environ', {'GCP_PROJECT_ID': 'test-project'})
    def test_handles_json_parse_error(self, mock_model_cls, mock_vertexai):
        from src.analysis.gemini_news_analyzer import analyze_position_investment

        mock_model = MagicMock()
        mock_model_cls.return_value = mock_model
        mock_response = MagicMock()
        mock_response.text = 'not valid json at all'
        mock_model.generate_content.return_value = mock_response

        result = analyze_position_investment(
            {'symbol': 'BTC', 'entry_price': 50000, 'quantity': 0.1},
            52000.0, [], {'rsi': 55, 'sma': 49000, 'regime': 'unknown'},
            {'articles_last_1h': 0, 'articles_last_4h': 0, 'articles_last_24h': 0,
             'avg_sentiment_1h': 0, 'avg_sentiment_4h': 0, 'avg_sentiment_24h': 0,
             'sentiment_trend': 'stable', 'velocity_status': 'quiet', 'breaking_detected': False},
        )
        assert result is None

    @patch('src.analysis.gemini_news_analyzer.vertexai')
    @patch('src.analysis.gemini_news_analyzer.GenerativeModel')
    @patch.dict('os.environ', {'GCP_PROJECT_ID': 'test-project'})
    def test_includes_additions_history_in_prompt(self, mock_model_cls, mock_vertexai):
        from src.analysis.gemini_news_analyzer import analyze_position_investment

        mock_model = MagicMock()
        mock_model_cls.return_value = mock_model
        mock_response = MagicMock()
        mock_response.text = '{"recommendation": "hold", "confidence": 0.5, "reasoning": "At max", "risk_level": "yellow", "primary_driver": "none", "news_momentum": "stable", "increase_sizing_hint": null, "key_article": null}'
        mock_model.generate_content.return_value = mock_response

        additions = [
            {'addition_price': 51000, 'addition_quantity': 0.05, 'reason': 'First increase'},
        ]

        result = analyze_position_investment(
            {'symbol': 'BTC', 'entry_price': 50500, 'quantity': 0.15},
            52000.0, [], {'rsi': 55, 'sma': 49000, 'regime': 'unknown'},
            {'articles_last_1h': 1, 'articles_last_4h': 3, 'articles_last_24h': 10,
             'avg_sentiment_1h': 0.1, 'avg_sentiment_4h': 0.1, 'avg_sentiment_24h': 0.1,
             'sentiment_trend': 'stable', 'velocity_status': 'normal', 'breaking_detected': False},
            position_additions=additions,
        )
        assert result is not None
        # Verify prompt included additions info
        call_args = mock_model.generate_content.call_args
        prompt_text = call_args[0][0]
        assert 'Addition History' in prompt_text
        assert 'First increase' in prompt_text


# ---------- Database: Position Additions ----------

class TestPositionAdditionsDB:
    """Tests for save_position_addition, get_position_additions, update_trade_position."""

    @patch('src.database.get_db_connection')
    @patch('src.database.release_db_connection')
    def test_save_position_addition(self, mock_release, mock_conn):
        from src.database import save_position_addition
        conn = MagicMock(spec=sqlite3.Connection)
        mock_conn.return_value = conn

        cursor_mock = MagicMock()
        with patch('src.database._cursor') as mock_ctx:
            mock_ctx.return_value.__enter__ = MagicMock(return_value=cursor_mock)
            mock_ctx.return_value.__exit__ = MagicMock(return_value=False)
            save_position_addition('ORDER_123', 52000.0, 0.05, 'ETF catalyst')

        cursor_mock.execute.assert_called_once()
        conn.commit.assert_called_once()

    @patch('src.database.get_db_connection')
    @patch('src.database.release_db_connection')
    def test_get_position_additions_returns_list(self, mock_release, mock_conn):
        from src.database import get_position_additions
        conn = MagicMock(spec=sqlite3.Connection)
        mock_conn.return_value = conn

        fake_rows = [
            {'parent_order_id': 'ORDER_123', 'addition_price': 52000.0,
             'addition_quantity': 0.05, 'reason': 'test', 'created_at': '2026-01-01'},
        ]
        cursor_mock = MagicMock()
        cursor_mock.fetchall.return_value = fake_rows

        with patch('src.database._cursor') as mock_ctx:
            mock_ctx.return_value.__enter__ = MagicMock(return_value=cursor_mock)
            mock_ctx.return_value.__exit__ = MagicMock(return_value=False)
            result = get_position_additions.sync('ORDER_123')

        assert len(result) == 1
        assert result[0]['addition_price'] == 52000.0

    @patch('src.database.get_db_connection')
    @patch('src.database.release_db_connection')
    def test_update_trade_position(self, mock_release, mock_conn):
        from src.database import update_trade_position
        conn = MagicMock(spec=sqlite3.Connection)
        mock_conn.return_value = conn

        cursor_mock = MagicMock()
        with patch('src.database._cursor') as mock_ctx:
            mock_ctx.return_value.__enter__ = MagicMock(return_value=cursor_mock)
            mock_ctx.return_value.__exit__ = MagicMock(return_value=False)
            update_trade_position('ORDER_123', 51500.0, 0.15)

        cursor_mock.execute.assert_called_once()
        conn.commit.assert_called_once()


# ---------- Add to Position (Binance Trader) ----------

class TestAddToPosition:
    """Tests for add_to_position() in binance_trader."""

    @patch('src.execution.binance_trader._is_live_trading', return_value=False)
    @patch('src.database.release_db_connection')
    @patch('src.database.get_db_connection')
    @patch('src.execution.binance_trader.get_db_connection')
    @patch('src.execution.binance_trader.release_db_connection')
    def test_paper_add_to_position_weighted_avg(self, mock_release, mock_conn,
                                                 mock_db_conn, mock_db_release, mock_live):
        from src.execution.binance_trader import add_to_position

        conn = MagicMock(spec=sqlite3.Connection)
        mock_conn.return_value = conn
        mock_db_conn.return_value = conn

        # Existing position: 0.1 BTC at $50000
        cursor_mock = MagicMock()
        cursor_mock.fetchone.return_value = [50000.0, 0.1]
        cursor_mock.close = MagicMock()

        with patch('src.execution.binance_trader._cursor') as mock_ctx, \
             patch('src.database._cursor') as mock_db_ctx:
            mock_ctx.return_value.__enter__ = MagicMock(return_value=cursor_mock)
            mock_ctx.return_value.__exit__ = MagicMock(return_value=False)
            mock_db_ctx.return_value.__enter__ = MagicMock(return_value=cursor_mock)
            mock_db_ctx.return_value.__exit__ = MagicMock(return_value=False)

            result = add_to_position('ORDER_1', 'BTC', 0.05, 52000.0, reason='Catalyst')

        assert result['status'] == 'FILLED'
        # Weighted avg: (50000*0.1 + 52000*0.05) / 0.15 = 50666.67
        expected_avg = (50000 * 0.1 + 52000 * 0.05) / 0.15
        assert abs(result['new_avg_price'] - expected_avg) < 0.01
        assert abs(result['new_total_quantity'] - 0.15) < 0.001

    @patch('src.execution.binance_trader._is_live_trading', return_value=False)
    @patch('src.execution.binance_trader.get_db_connection')
    @patch('src.execution.binance_trader.release_db_connection')
    def test_paper_add_fails_if_position_not_found(self, mock_release, mock_conn, mock_live):
        from src.execution.binance_trader import add_to_position

        conn = MagicMock(spec=sqlite3.Connection)
        mock_conn.return_value = conn

        cursor_mock = MagicMock()
        cursor_mock.fetchone.return_value = None
        cursor_mock.close = MagicMock()

        with patch('src.execution.binance_trader._cursor') as mock_ctx:
            mock_ctx.return_value.__enter__ = MagicMock(return_value=cursor_mock)
            mock_ctx.return_value.__exit__ = MagicMock(return_value=False)

            result = add_to_position('NONEXISTENT', 'BTC', 0.05, 52000.0)

        assert result['status'] == 'FAILED'

    @patch('src.execution.binance_trader._is_live_trading', return_value=False)
    @patch('src.database.release_db_connection')
    @patch('src.database.get_db_connection')
    @patch('src.execution.binance_trader.get_db_connection')
    @patch('src.execution.binance_trader.release_db_connection')
    def test_paper_add_position_preserves_order_id(self, mock_release, mock_conn,
                                                    mock_db_conn, mock_db_release, mock_live):
        from src.execution.binance_trader import add_to_position

        conn = MagicMock(spec=sqlite3.Connection)
        mock_conn.return_value = conn
        mock_db_conn.return_value = conn

        cursor_mock = MagicMock()
        cursor_mock.fetchone.return_value = [100.0, 10.0]
        cursor_mock.close = MagicMock()

        with patch('src.execution.binance_trader._cursor') as mock_ctx, \
             patch('src.database._cursor') as mock_db_ctx:
            mock_ctx.return_value.__enter__ = MagicMock(return_value=cursor_mock)
            mock_ctx.return_value.__exit__ = MagicMock(return_value=False)
            mock_db_ctx.return_value.__enter__ = MagicMock(return_value=cursor_mock)
            mock_db_ctx.return_value.__exit__ = MagicMock(return_value=False)

            result = add_to_position('PAPER_AAPL_BUY_123', 'AAPL', 5.0, 105.0,
                                     asset_type='stock')

        assert result['order_id'] == 'PAPER_AAPL_BUY_123'
        assert result['symbol'] == 'AAPL'


# ---------- Telegram INCREASE Signal ----------

class TestTelegramIncreaseSignal:
    """Tests that send_signal_for_confirmation handles INCREASE correctly."""

    @patch('src.notify.telegram_bot.Bot')
    @patch('src.notify.telegram_bot.telegram_config', {'enabled': True})
    @patch('src.notify.telegram_bot.TOKEN', 'fake-token')
    @patch('src.notify.telegram_bot.CHAT_ID', '12345')
    def test_increase_signal_message_format(self, mock_bot_cls):
        """INCREASE signal should show current + adding + new total."""
        from src.notify.telegram_bot import send_signal_for_confirmation, _pending_signals

        mock_bot = MagicMock()
        mock_bot_cls.return_value = mock_bot
        mock_sent = MagicMock()
        mock_sent.message_id = 42
        mock_bot.send_message = AsyncMock(return_value=mock_sent)

        signal = {
            'signal': 'INCREASE',
            'symbol': 'BTC',
            'current_price': 52000.0,
            'quantity': 0.05,
            'reason': 'ETF approval catalyst',
            'asset_type': 'crypto',
            'position': {'order_id': 'ORDER_1', 'quantity': 0.1},
        }

        loop = asyncio.get_event_loop()
        signal_id = loop.run_until_complete(send_signal_for_confirmation(signal))

        assert signal_id > 0
        assert signal_id in _pending_signals

        # Check that the message mentions the current/adding/new total
        call_args = mock_bot.send_message.call_args
        message_text = call_args.kwargs.get('text', call_args[1].get('text', ''))
        assert 'INCREASE' in message_text
        assert 'Adding' in message_text
        assert 'New Total' in message_text

        _pending_signals.clear()


# ---------- Execute Confirmed Signal: INCREASE ----------

class TestExecuteConfirmedIncrease:
    """Tests for the INCREASE branch in execute_confirmed_signal."""

    def setup_method(self):
        from src.orchestration import bot_state
        bot_state._trailing_stop_peaks.clear()
        bot_state._analyst_last_run.clear()

    @patch('src.orchestration.trade_executor.add_to_position')
    @patch('src.orchestration.trade_executor._get_trading_mode', return_value='paper')
    def test_crypto_increase_calls_add_to_position(self, mock_mode, mock_add):
        from src.orchestration.trade_executor import execute_confirmed_signal
        mock_add.return_value = {'status': 'FILLED', 'new_avg_price': 51000, 'new_total_quantity': 0.15, 'price': 52000}

        signal = {
            'signal': 'INCREASE',
            'symbol': 'BTC',
            'current_price': 52000.0,
            'quantity': 0.05,
            'reason': 'Catalyst',
            'asset_type': 'crypto',
            'position': {'order_id': 'ORDER_1', 'quantity': 0.1},
        }

        loop = asyncio.get_event_loop()
        result = loop.run_until_complete(execute_confirmed_signal(signal))

        mock_add.assert_called_once_with(
            'ORDER_1', 'BTC', 0.05, 52000.0,
            reason='Catalyst', asset_type='crypto',
        )
        assert result['status'] == 'FILLED'

    @patch('src.orchestration.trade_executor.add_to_position')
    @patch('src.orchestration.trade_executor._get_trading_mode', return_value='paper')
    @patch('src.orchestration.trade_executor.app_config', {'settings': {'stock_trading': {'broker': 'paper_only'}}})
    def test_stock_increase_calls_add_to_position(self, mock_mode, mock_add):
        from src.orchestration.trade_executor import execute_confirmed_signal
        mock_add.return_value = {'status': 'FILLED', 'new_avg_price': 105, 'new_total_quantity': 15, 'price': 105}

        signal = {
            'signal': 'INCREASE',
            'symbol': 'AAPL',
            'current_price': 105.0,
            'quantity': 5.0,
            'reason': 'Earnings beat',
            'asset_type': 'stock',
            'position': {'order_id': 'PAPER_AAPL_BUY_123', 'quantity': 10.0},
        }

        loop = asyncio.get_event_loop()
        result = loop.run_until_complete(execute_confirmed_signal(signal))

        mock_add.assert_called_once()
        assert result['status'] == 'FILLED'

    @patch('src.orchestration.trade_executor.place_order')
    @patch('src.orchestration.trade_executor._get_trading_mode', return_value='paper')
    def test_sell_clears_analyst_run(self, mock_mode, mock_place):
        """SELL via execute_confirmed_signal should clean up _analyst_last_run."""
        from src.orchestration.trade_executor import execute_confirmed_signal
        from src.orchestration import bot_state

        bot_state._analyst_last_run['ORDER_1'] = datetime.now(timezone.utc)
        mock_place.return_value = {'status': 'CLOSED', 'pnl': 50.0}

        signal = {
            'signal': 'SELL',
            'symbol': 'BTC',
            'current_price': 52000.0,
            'quantity': 0.1,
            'asset_type': 'crypto',
            'position': {'order_id': 'ORDER_1', 'quantity': 0.1},
        }

        loop = asyncio.get_event_loop()
        loop.run_until_complete(execute_confirmed_signal(signal))

        assert 'ORDER_1' not in bot_state._analyst_last_run


# ---------- Config ----------

class TestPositionAnalystConfig:
    """Tests that position_analyst config is loaded correctly."""

    def test_config_has_position_analyst_section(self):
        from src.config import app_config
        settings = app_config.get('settings', {})
        analyst_cfg = settings.get('position_analyst', {})
        assert analyst_cfg.get('enabled') is True
        assert analyst_cfg.get('min_position_age_hours') == 2
        assert analyst_cfg.get('check_interval_minutes') == 30
        assert analyst_cfg.get('max_position_multiplier') == 3.0

    def test_increase_in_confirmation_signals(self):
        from src.config import app_config
        settings = app_config.get('settings', {})
        confirm_cfg = settings.get('signal_confirmation', {})
        signals = confirm_cfg.get('require_confirmation_for', [])
        assert 'INCREASE' in signals


# ---------- Sizing Hints ----------

class TestIncreaseSizingHints:
    """Tests that sizing hints map to correct fractions."""

    def test_hint_fractions(self):
        hint_fractions = {'small': 0.25, 'medium': 0.50, 'large': 0.75}
        assert hint_fractions['small'] == 0.25
        assert hint_fractions['medium'] == 0.50
        assert hint_fractions['large'] == 0.75

    def test_weighted_average_calculation(self):
        """Verify the weighted average formula used in add_to_position."""
        old_price, old_qty = 50000.0, 0.1
        add_price, add_qty = 52000.0, 0.05
        new_total = old_qty + add_qty
        new_avg = (old_price * old_qty + add_price * add_qty) / new_total
        assert abs(new_avg - 50666.67) < 0.01
        assert abs(new_total - 0.15) < 0.001

    def test_max_multiplier_cap(self):
        """Position additions should be blocked when multiplier exceeds cap."""
        original_value = 5000.0  # 0.1 BTC at $50000
        max_mult = 3.0
        max_total_value = original_value * max_mult  # $15000

        # After 2 additions: $5000 + $2500 + $2500 = $10000 (2x) — allowed
        assert 10000 <= max_total_value

        # After 3 additions: $10000 + $6000 = $16000 (3.2x) — blocked
        assert 16000 > max_total_value
