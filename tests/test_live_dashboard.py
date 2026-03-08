"""Tests for src/notify/telegram_live_dashboard.py"""

import asyncio
import pytest
from unittest.mock import patch, AsyncMock, MagicMock

from src.notify.telegram_live_dashboard import (
    build_live_dashboard,
    build_daily_recap,
    update_live_dashboard,
    reset_dashboard_state,
)


def run_async(coro):
    return asyncio.run(coro)


@pytest.fixture(autouse=True)
def clean_state():
    reset_dashboard_state()
    yield
    reset_dashboard_state()


def _make_cycle_data(**overrides):
    base = {
        'crypto_positions': [],
        'stock_positions': [],
        'auto_positions': [],
        'crypto_balance': {'total_usd': 5000, 'USDT': 2000},
        'stock_balance': {'total_usd': 5000, 'USDT': 0},
        'daily_pnl': 76.0,
        'regime': {
            'regime': 'RISK_ON',
            'position_size_multiplier': 1.0,
            'suppress_buys': False,
        },
        'cb_status': {'in_cooldown': False},
        'events': [],
        'prices': {},
        'last_signals': [],
        'auto_summary': {},
    }
    base.update(overrides)
    return base


class TestBuildDashboardWithPositions:
    def test_basic_format(self):
        data = _make_cycle_data(
            crypto_positions=[
                {'symbol': 'BTC', 'entry_price': 50000, 'quantity': 0.1,
                 'status': 'OPEN', 'order_id': 'o1',
                 'entry_timestamp': '2026-03-06T10:00:00+00:00'},
            ],
            prices={'BTC': 51700},
        )
        text = build_live_dashboard(data)
        assert 'DASHBOARD' in text
        assert '$10,000' in text
        assert 'BTC' in text
        assert 'RISK_ON' in text
        assert '+$76' in text

    def test_shows_cb_halt(self):
        data = _make_cycle_data(cb_status={'in_cooldown': True})
        text = build_live_dashboard(data)
        assert 'CB: HALT' in text


class TestBuildDashboardEmpty:
    def test_no_positions(self):
        data = _make_cycle_data()
        text = build_live_dashboard(data)
        assert 'No open positions' in text
        assert 'DASHBOARD' in text


class TestBuildDashboardStrategicPositions:
    def test_shows_strategy_label(self):
        data = _make_cycle_data(
            stock_positions=[
                {'symbol': 'IONQ', 'entry_price': 30, 'quantity': 10,
                 'status': 'OPEN', 'order_id': 'o2',
                 'entry_timestamp': '2026-03-05T10:00:00+00:00',
                 'strategy_type': 'growth'},
            ],
            prices={'IONQ': 30.84},
        )
        text = build_live_dashboard(data)
        assert 'IONQ' in text
        assert 'growth' in text


class TestUpdateSendsAndPinsFirstTime:
    @patch('src.notify.telegram_live_dashboard.save_bot_state')
    @patch('src.notify.telegram_live_dashboard._get_chat_id', return_value='123')
    def test_sends_and_pins(self, mock_chat, mock_save):
        app = MagicMock()
        msg = MagicMock()
        msg.message_id = 42
        app.bot.send_message = AsyncMock(return_value=msg)
        app.bot.pin_chat_message = AsyncMock()

        data = _make_cycle_data()
        run_async(update_live_dashboard(app, data))

        app.bot.send_message.assert_called_once()
        app.bot.pin_chat_message.assert_called_once()
        mock_save.assert_called_with('dashboard_message_id', '42')


class TestUpdateEditsExisting:
    @patch('src.notify.telegram_live_dashboard._get_chat_id', return_value='123')
    def test_edits_message(self, mock_chat):
        import src.notify.telegram_live_dashboard as mod
        mod._dashboard_message_id = 42
        mod._last_dashboard_text = 'old text'

        app = MagicMock()
        app.bot.edit_message_text = AsyncMock()

        data = _make_cycle_data()
        run_async(update_live_dashboard(app, data))

        app.bot.edit_message_text.assert_called_once()
        call_kwargs = app.bot.edit_message_text.call_args[1]
        assert call_kwargs['message_id'] == 42


class TestUpdateSkipsUnchanged:
    @patch('src.notify.telegram_live_dashboard._get_chat_id', return_value='123')
    def test_skips_if_same(self, mock_chat):
        import src.notify.telegram_live_dashboard as mod
        mod._dashboard_message_id = 42

        app = MagicMock()
        app.bot.edit_message_text = AsyncMock()

        data = _make_cycle_data()
        text = build_live_dashboard(data)
        mod._last_dashboard_text = text

        run_async(update_live_dashboard(app, data))
        app.bot.edit_message_text.assert_not_called()


class TestDeletedMessageRecovery:
    @patch('src.notify.telegram_live_dashboard.save_bot_state')
    @patch('src.notify.telegram_live_dashboard._get_chat_id', return_value='123')
    def test_creates_new_on_bad_request(self, mock_chat, mock_save):
        from telegram.error import BadRequest
        import src.notify.telegram_live_dashboard as mod
        mod._dashboard_message_id = 42
        mod._last_dashboard_text = 'old text'

        app = MagicMock()
        app.bot.edit_message_text = AsyncMock(
            side_effect=BadRequest('Message to edit not found'))
        msg = MagicMock()
        msg.message_id = 99
        app.bot.send_message = AsyncMock(return_value=msg)
        app.bot.pin_chat_message = AsyncMock()

        data = _make_cycle_data()
        run_async(update_live_dashboard(app, data))

        app.bot.send_message.assert_called_once()
        app.bot.pin_chat_message.assert_called_once()
        mock_save.assert_called_with('dashboard_message_id', '99')


class TestBuildDailyRecap:
    @patch('src.notify.telegram_live_dashboard.get_trades_closed_today')
    def test_format_with_trades(self, mock_trades):
        mock_trades.side_effect = [
            [{'symbol': 'BTC', 'entry_price': 50000, 'exit_price': 52000,
              'pnl': 200, 'exit_reason': 'take_profit',
              'entry_timestamp': '2026-03-08T06:00:00+00:00',
              'exit_timestamp': '2026-03-08T14:00:00+00:00',
              'strategy_type': None}],
            [],  # auto trades
        ]
        text = build_daily_recap()
        assert 'DAILY RECAP' in text
        assert 'BTC' in text
        assert '+$200' in text
        assert 'take_profit' in text


class TestDailyRecapSkipsNoTrades:
    @patch('src.notify.telegram_live_dashboard.get_trades_closed_today',
           return_value=[])
    def test_empty(self, mock_trades):
        text = build_daily_recap()
        assert text == ''


class TestSaveLoadBotState:
    @patch('src.database.release_db_connection')
    @patch('src.database.get_db_connection')
    def test_roundtrip(self, mock_conn_fn, mock_release):
        """save_bot_state + load_bot_state round-trip with real SQLite."""
        import sqlite3
        conn = sqlite3.connect(':memory:')
        conn.execute("""
            CREATE TABLE bot_state_kv (
                key TEXT PRIMARY KEY,
                value TEXT,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        mock_conn_fn.return_value = conn

        from src.database import save_bot_state, load_bot_state

        save_bot_state('test_key', 'test_value')
        result = load_bot_state('test_key')
        assert result == 'test_value'

        # Update existing key
        save_bot_state('test_key', 'updated_value')
        result = load_bot_state('test_key')
        assert result == 'updated_value'

        # Non-existent key
        result = load_bot_state('nonexistent')
        assert result is None

        conn.close()
