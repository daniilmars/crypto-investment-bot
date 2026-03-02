# tests/test_signal_confirmation.py

import asyncio
import itertools
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.notify.telegram_bot import (
    _pending_signals,
    send_signal_for_confirmation,
    _handle_signal_callback,
    cleanup_expired_signals,
    register_execute_callback,
    is_confirmation_required,
)


def run_async(coro):
    """Helper to run async functions in sync tests."""
    return asyncio.get_event_loop().run_until_complete(coro)


@pytest.fixture(autouse=True)
def clear_pending_signals():
    """Clear pending signals before and after each test."""
    _pending_signals.clear()
    import src.notify.telegram_bot as tb
    tb._signal_counter = itertools.count(1)
    tb._execute_callback = None
    yield
    _pending_signals.clear()
    tb._signal_counter = itertools.count(1)
    tb._execute_callback = None


@pytest.fixture
def sample_buy_signal():
    return {
        'signal': 'BUY',
        'symbol': 'BTC',
        'current_price': 67432.50,
        'reason': 'Gemini bullish (confidence 0.85) + RSI oversold (28.3)',
        'asset_type': 'crypto',
        'quantity': 0.0044,
    }


@pytest.fixture
def sample_sell_signal():
    return {
        'signal': 'SELL',
        'symbol': 'ETH',
        'current_price': 3200.00,
        'reason': 'Gemini bearish (confidence 0.80)',
        'asset_type': 'crypto',
        'quantity': 1.5,
        'position': {'order_id': 'test-order-123', 'quantity': 1.5},
    }


class TestIsConfirmationRequired:
    @patch('src.notify.telegram_bot.CONFIRMATION_ENABLED', True)
    @patch('src.notify.telegram_bot.CONFIRMATION_SIGNALS', ['BUY', 'SELL'])
    def test_buy_requires_confirmation(self):
        assert is_confirmation_required("BUY") is True

    @patch('src.notify.telegram_bot.CONFIRMATION_ENABLED', True)
    @patch('src.notify.telegram_bot.CONFIRMATION_SIGNALS', ['BUY', 'SELL'])
    def test_sell_requires_confirmation(self):
        assert is_confirmation_required("SELL") is True

    @patch('src.notify.telegram_bot.CONFIRMATION_ENABLED', True)
    @patch('src.notify.telegram_bot.CONFIRMATION_SIGNALS', ['BUY', 'SELL'])
    def test_hold_does_not_require_confirmation(self):
        assert is_confirmation_required("HOLD") is False

    @patch('src.notify.telegram_bot.CONFIRMATION_ENABLED', False)
    def test_disabled_returns_false(self):
        assert is_confirmation_required("BUY") is False
        assert is_confirmation_required("SELL") is False


class TestSendSignalForConfirmation:
    @patch('src.notify.telegram_bot.Bot')
    @patch('src.notify.telegram_bot.telegram_config', {'enabled': True})
    @patch('src.notify.telegram_bot.TOKEN', 'test-token')
    @patch('src.notify.telegram_bot.CHAT_ID', '12345')
    @patch('src.notify.telegram_bot._get_trading_mode', return_value='paper')
    def test_sends_message_with_inline_keyboard(self, mock_mode, mock_bot_class, sample_buy_signal):
        mock_bot = MagicMock()
        mock_sent_msg = MagicMock()
        mock_sent_msg.message_id = 999
        mock_bot.send_message = AsyncMock(return_value=mock_sent_msg)
        mock_bot_class.return_value = mock_bot

        signal_id = run_async(send_signal_for_confirmation(sample_buy_signal))

        assert signal_id == 1
        assert signal_id in _pending_signals
        assert _pending_signals[signal_id]['signal'] == sample_buy_signal
        assert _pending_signals[signal_id]['message_id'] == 999

        # Verify send_message was called with inline keyboard
        call_kwargs = mock_bot.send_message.call_args
        assert call_kwargs.kwargs['reply_markup'] is not None
        keyboard = call_kwargs.kwargs['reply_markup']
        buttons = keyboard.inline_keyboard[0]
        assert len(buttons) == 2
        assert 'a:1' in buttons[0].callback_data
        assert 'r:1' in buttons[1].callback_data

    @patch('src.notify.telegram_bot.Bot')
    @patch('src.notify.telegram_bot.telegram_config', {'enabled': True})
    @patch('src.notify.telegram_bot.TOKEN', 'test-token')
    @patch('src.notify.telegram_bot.CHAT_ID', '12345')
    @patch('src.notify.telegram_bot._get_trading_mode', return_value='paper')
    def test_increments_signal_counter(self, mock_mode, mock_bot_class, sample_buy_signal):
        mock_bot = MagicMock()
        mock_bot.send_message = AsyncMock(return_value=MagicMock(message_id=1))
        mock_bot_class.return_value = mock_bot

        id1 = run_async(send_signal_for_confirmation(sample_buy_signal))
        id2 = run_async(send_signal_for_confirmation(sample_buy_signal))

        assert id1 == 1
        assert id2 == 2
        assert len(_pending_signals) == 2

    @patch('src.notify.telegram_bot.telegram_config', {'enabled': False})
    def test_returns_negative_when_disabled(self, sample_buy_signal):
        signal_id = run_async(send_signal_for_confirmation(sample_buy_signal))
        assert signal_id == -1
        assert len(_pending_signals) == 0


class TestHandleSignalCallback:
    def _make_callback_query(self, data, user_id=7910661624):
        query = MagicMock()
        query.data = data
        query.from_user = MagicMock()
        query.from_user.id = user_id
        query.answer = AsyncMock()
        query.edit_message_text = AsyncMock()
        return query

    def _make_update(self, query):
        update = MagicMock()
        update.callback_query = query
        return update

    @patch('src.notify.telegram_bot.AUTHORIZED_USER_IDS', [7910661624])
    def test_approve_calls_execute_callback(self, sample_buy_signal):
        mock_execute = AsyncMock(return_value={'price': 67435.0, 'status': 'FILLED'})
        register_execute_callback(mock_execute)

        _pending_signals[1] = {
            'signal': sample_buy_signal,
            'message_id': 999,
            'chat_id': '12345',
            'created_at': datetime.now(timezone.utc),
        }

        query = self._make_callback_query("a:1")
        update = self._make_update(query)

        run_async(_handle_signal_callback(update, MagicMock()))

        mock_execute.assert_called_once_with(sample_buy_signal)
        query.edit_message_text.assert_called_once()
        edited_text = query.edit_message_text.call_args.args[0]
        assert "EXECUTED" in edited_text
        assert "BTC" in edited_text
        assert 1 not in _pending_signals

    @patch('src.notify.telegram_bot.AUTHORIZED_USER_IDS', [7910661624])
    def test_reject_does_not_execute(self, sample_buy_signal):
        mock_execute = AsyncMock()
        register_execute_callback(mock_execute)

        _pending_signals[1] = {
            'signal': sample_buy_signal,
            'message_id': 999,
            'chat_id': '12345',
            'created_at': datetime.now(timezone.utc),
        }

        query = self._make_callback_query("r:1")
        update = self._make_update(query)

        run_async(_handle_signal_callback(update, MagicMock()))

        mock_execute.assert_not_called()
        query.edit_message_text.assert_called_once()
        edited_text = query.edit_message_text.call_args.args[0]
        assert "SKIPPED" in edited_text
        assert 1 not in _pending_signals

    @patch('src.notify.telegram_bot.AUTHORIZED_USER_IDS', [7910661624])
    def test_unknown_signal_id(self):
        query = self._make_callback_query("a:999")
        update = self._make_update(query)

        run_async(_handle_signal_callback(update, MagicMock()))

        query.answer.assert_called_once_with("Signal expired or already handled.", show_alert=True)

    @patch('src.notify.telegram_bot.AUTHORIZED_USER_IDS', [7910661624])
    def test_unauthorized_user_rejected(self, sample_buy_signal):
        _pending_signals[1] = {
            'signal': sample_buy_signal,
            'message_id': 999,
            'chat_id': '12345',
            'created_at': datetime.now(timezone.utc),
        }

        query = self._make_callback_query("a:1", user_id=99999)
        update = self._make_update(query)

        run_async(_handle_signal_callback(update, MagicMock()))

        query.answer.assert_called_once_with("You are not authorized.", show_alert=True)
        # Signal should still be pending
        assert 1 in _pending_signals

    @patch('src.notify.telegram_bot.AUTHORIZED_USER_IDS', [7910661624])
    def test_approve_with_execution_error(self, sample_buy_signal):
        mock_execute = AsyncMock(side_effect=Exception("Connection failed"))
        register_execute_callback(mock_execute)

        _pending_signals[1] = {
            'signal': sample_buy_signal,
            'message_id': 999,
            'chat_id': '12345',
            'created_at': datetime.now(timezone.utc),
        }

        query = self._make_callback_query("a:1")
        update = self._make_update(query)

        run_async(_handle_signal_callback(update, MagicMock()))

        query.edit_message_text.assert_called_once()
        edited_text = query.edit_message_text.call_args.args[0]
        assert "EXECUTION FAILED" in edited_text
        assert "Connection failed" in edited_text

    @patch('src.notify.telegram_bot.AUTHORIZED_USER_IDS', [7910661624])
    def test_approve_sell_with_pnl(self, sample_sell_signal):
        order_result = {'price': 3205.0, 'status': 'CLOSED', 'pnl': 7.50}
        mock_execute = AsyncMock(return_value=order_result)
        register_execute_callback(mock_execute)

        _pending_signals[1] = {
            'signal': sample_sell_signal,
            'message_id': 999,
            'chat_id': '12345',
            'created_at': datetime.now(timezone.utc),
        }

        query = self._make_callback_query("a:1")
        update = self._make_update(query)

        run_async(_handle_signal_callback(update, MagicMock()))

        edited_text = query.edit_message_text.call_args.args[0]
        assert "EXECUTED" in edited_text
        assert "SELL" in edited_text
        assert "$7.50" in edited_text


class TestCleanupExpiredSignals:
    @patch('src.notify.telegram_bot.Bot')
    @patch('src.notify.telegram_bot.TOKEN', 'test-token')
    @patch('src.notify.telegram_bot.CONFIRMATION_TIMEOUT_MINUTES', 30)
    def test_expires_old_signals(self, mock_bot_class, sample_buy_signal):
        mock_bot = MagicMock()
        mock_bot.edit_message_text = AsyncMock()
        mock_bot_class.return_value = mock_bot

        # Add an expired signal (created 31 minutes ago)
        _pending_signals[1] = {
            'signal': sample_buy_signal,
            'message_id': 999,
            'chat_id': '12345',
            'created_at': datetime.now(timezone.utc) - timedelta(minutes=31),
        }

        run_async(cleanup_expired_signals())

        assert 1 not in _pending_signals
        mock_bot.edit_message_text.assert_called_once()
        edited_text = mock_bot.edit_message_text.call_args.kwargs['text']
        assert "EXPIRED" in edited_text

    @patch('src.notify.telegram_bot.CONFIRMATION_TIMEOUT_MINUTES', 30)
    def test_does_not_expire_fresh_signals(self, sample_buy_signal):
        # Add a fresh signal (created 5 minutes ago)
        _pending_signals[1] = {
            'signal': sample_buy_signal,
            'message_id': 999,
            'chat_id': '12345',
            'created_at': datetime.now(timezone.utc) - timedelta(minutes=5),
        }

        run_async(cleanup_expired_signals())

        assert 1 in _pending_signals

    def test_no_op_when_empty(self):
        # Should not raise
        run_async(cleanup_expired_signals())


class TestRegisterExecuteCallback:
    def test_registers_callback(self):
        import src.notify.telegram_bot as tb
        mock_fn = AsyncMock()
        register_execute_callback(mock_fn)
        assert tb._execute_callback is mock_fn
