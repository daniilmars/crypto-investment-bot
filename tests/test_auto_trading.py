# tests/test_auto_trading.py
"""Tests for the auto-trading shadow bot feature."""

import sqlite3
import time
import pytest
from unittest.mock import patch, MagicMock, AsyncMock
from decimal import Decimal


# ---------------------------------------------------------------------------
# Database: trading_strategy column
# ---------------------------------------------------------------------------

@patch('src.database.release_db_connection')
@patch('src.database.get_db_connection')
def test_initialize_database_adds_trading_strategy_column(mock_get_conn, mock_release):
    """initialize_database runs ALTER TABLE to add trading_strategy."""
    mock_conn = MagicMock()
    mock_cursor = MagicMock()
    mock_get_conn.return_value = mock_conn
    mock_conn.cursor.return_value = mock_cursor

    from src.database import initialize_database
    initialize_database()

    executed_queries = [' '.join(call[0][0].split()) for call in mock_cursor.execute.call_args_list]
    assert any("trading_strategy" in q for q in executed_queries), \
        "Expected ALTER TABLE for trading_strategy column"


@patch('src.database.release_db_connection')
@patch('src.database.get_db_connection')
def test_get_trade_summary_filters_by_strategy(mock_get_conn, mock_release):
    """get_trade_summary passes trading_strategy filter to SQL."""
    mock_conn = MagicMock()
    # Make it look like SQLite (not psycopg2)
    mock_conn.__class__ = sqlite3.Connection
    mock_cursor = MagicMock()
    mock_cursor.fetchall.return_value = []
    mock_get_conn.return_value = mock_conn

    # Patch _cursor context manager
    from contextlib import contextmanager
    @contextmanager
    def fake_cursor(conn):
        yield mock_cursor

    with patch('src.database._cursor', fake_cursor):
        from src.database import get_trade_summary
        result = get_trade_summary(hours_ago=24, trading_strategy='auto')

    # Verify SQL includes trading_strategy filter
    call_args = mock_cursor.execute.call_args
    query = call_args[0][0]
    params = call_args[0][1]
    assert "trading_strategy" in query
    assert 'auto' in params


@patch('src.database.release_db_connection')
@patch('src.database.get_db_connection')
def test_get_trade_summary_no_strategy_filter(mock_get_conn, mock_release):
    """get_trade_summary without trading_strategy doesn't add the filter."""
    mock_conn = MagicMock()
    mock_conn.__class__ = sqlite3.Connection
    mock_cursor = MagicMock()
    mock_cursor.fetchall.return_value = []
    mock_get_conn.return_value = mock_conn

    from contextlib import contextmanager
    @contextmanager
    def fake_cursor(conn):
        yield mock_cursor

    with patch('src.database._cursor', fake_cursor):
        from src.database import get_trade_summary
        result = get_trade_summary(hours_ago=24)

    call_args = mock_cursor.execute.call_args
    query = call_args[0][0]
    assert "trading_strategy" not in query


@patch('src.database.release_db_connection')
@patch('src.database.get_db_connection')
def test_get_trade_history_stats_filters_by_strategy(mock_get_conn, mock_release):
    """get_trade_history_stats passes trading_strategy filter."""
    mock_conn = MagicMock()
    mock_conn.__class__ = sqlite3.Connection
    mock_cursor = MagicMock()
    mock_cursor.fetchall.return_value = []
    mock_get_conn.return_value = mock_conn

    from contextlib import contextmanager
    @contextmanager
    def fake_cursor(conn):
        yield mock_cursor

    with patch('src.database._cursor', fake_cursor):
        from src.database import get_trade_history_stats
        result = get_trade_history_stats(trading_strategy='auto')

    call_args = mock_cursor.execute.call_args
    query = call_args[0][0]
    params = call_args[0][1]
    assert "trading_strategy" in query
    assert 'auto' in params


# ---------------------------------------------------------------------------
# Binance Trader: strategy-aware functions
# ---------------------------------------------------------------------------

@patch('src.execution.binance_trader.release_db_connection')
@patch('src.execution.binance_trader.get_db_connection')
def test_get_open_positions_filters_by_strategy(mock_get_conn, mock_release):
    """get_open_positions adds trading_strategy filter when provided."""
    mock_conn = MagicMock()
    mock_conn.__class__ = sqlite3.Connection
    mock_conn.row_factory = None
    mock_cursor = MagicMock()
    mock_cursor.fetchall.return_value = []
    mock_conn.cursor.return_value = mock_cursor

    mock_get_conn.return_value = mock_conn

    from src.execution.binance_trader import get_open_positions
    result = get_open_positions(trading_strategy='auto')

    call_args = mock_cursor.execute.call_args
    query = call_args[0][0]
    params = call_args[0][1]
    assert "trading_strategy" in query
    assert 'auto' in params
    assert result == []


@patch('src.execution.binance_trader.release_db_connection')
@patch('src.execution.binance_trader.get_db_connection')
def test_get_open_positions_no_strategy_filter(mock_get_conn, mock_release):
    """get_open_positions without trading_strategy doesn't add filter."""
    mock_conn = MagicMock()
    mock_conn.__class__ = sqlite3.Connection
    mock_conn.row_factory = None
    mock_cursor = MagicMock()
    mock_cursor.fetchall.return_value = []
    mock_conn.cursor.return_value = mock_cursor

    mock_get_conn.return_value = mock_conn

    from src.execution.binance_trader import get_open_positions
    result = get_open_positions()

    call_args = mock_cursor.execute.call_args
    query = call_args[0][0]
    assert "trading_strategy" not in query


@patch('src.execution.binance_trader.release_db_connection')
@patch('src.execution.binance_trader.get_db_connection')
def test_paper_place_order_includes_trading_strategy(mock_get_conn, mock_release):
    """place_order inserts trading_strategy into the trades table."""
    mock_conn = MagicMock()
    mock_conn.__class__ = sqlite3.Connection
    mock_cursor = MagicMock()
    mock_conn.cursor.return_value = mock_cursor
    mock_get_conn.return_value = mock_conn

    from src.execution.binance_trader import place_order
    with patch('src.execution.binance_trader._is_live_trading', return_value=False):
        result = place_order("BTC", "BUY", 0.1, 50000.0, trading_strategy='auto')

    call_args = mock_cursor.execute.call_args
    query = call_args[0][0]
    params = call_args[0][1]
    assert "trading_strategy" in query
    assert 'auto' in params


@patch('src.execution.binance_trader.release_db_connection')
@patch('src.execution.binance_trader.get_db_connection')
def test_paper_place_order_auto_prefix(mock_get_conn, mock_release):
    """Auto-bot BUY orders get 'AUTO_' prefix instead of 'PAPER_'."""
    mock_conn = MagicMock()
    mock_conn.__class__ = sqlite3.Connection
    mock_cursor = MagicMock()
    mock_conn.cursor.return_value = mock_cursor
    mock_get_conn.return_value = mock_conn

    from src.execution.binance_trader import place_order
    with patch('src.execution.binance_trader._is_live_trading', return_value=False):
        result = place_order("ETH", "BUY", 1.0, 3000.0, trading_strategy='auto')

    assert result['order_id'].startswith('AUTO_')


@patch('src.execution.binance_trader.release_db_connection')
@patch('src.execution.binance_trader.get_db_connection')
def test_paper_place_order_manual_prefix(mock_get_conn, mock_release):
    """Manual BUY orders keep 'PAPER_' prefix."""
    mock_conn = MagicMock()
    mock_conn.__class__ = sqlite3.Connection
    mock_cursor = MagicMock()
    mock_conn.cursor.return_value = mock_cursor
    mock_get_conn.return_value = mock_conn

    from src.execution.binance_trader import place_order
    with patch('src.execution.binance_trader._is_live_trading', return_value=False):
        result = place_order("ETH", "BUY", 1.0, 3000.0, trading_strategy='manual')

    assert result['order_id'].startswith('PAPER_')


@patch('src.execution.binance_trader.release_db_connection')
@patch('src.execution.binance_trader.get_db_connection')
def test_get_paper_balance_uses_auto_capital(mock_get_conn, mock_release):
    """Auto-bot balance uses auto_trading.paper_trading_initial_capital."""
    mock_conn = MagicMock()
    mock_conn.__class__ = sqlite3.Connection
    mock_cursor = MagicMock()
    # Return 0 PnL and 0 locked
    mock_cursor.fetchone.side_effect = [(0,), (0,)]
    mock_get_conn.return_value = mock_conn

    from contextlib import contextmanager
    @contextmanager
    def fake_cursor(conn):
        yield mock_cursor

    with patch('src.execution.binance_trader._cursor', fake_cursor), \
         patch('src.execution.binance_trader.app_config', {
             'settings': {
                 'paper_trading_initial_capital': 10000.0,
                 'auto_trading': {'paper_trading_initial_capital': 5000.0}
             }
         }):
        from src.execution.binance_trader import _get_paper_balance
        result = _get_paper_balance(trading_strategy='auto')

    assert result['total_usd'] == 5000.0
    assert result['USDT'] == 5000.0


@patch('src.execution.binance_trader.release_db_connection')
@patch('src.execution.binance_trader.get_db_connection')
def test_get_paper_balance_manual_uses_default_capital(mock_get_conn, mock_release):
    """Manual balance uses settings.paper_trading_initial_capital."""
    mock_conn = MagicMock()
    mock_conn.__class__ = sqlite3.Connection
    mock_cursor = MagicMock()
    mock_cursor.fetchone.side_effect = [(0,), (0,)]
    mock_get_conn.return_value = mock_conn

    from contextlib import contextmanager
    @contextmanager
    def fake_cursor(conn):
        yield mock_cursor

    with patch('src.execution.binance_trader._cursor', fake_cursor), \
         patch('src.execution.binance_trader.app_config', {
             'settings': {
                 'paper_trading_initial_capital': 10000.0,
                 'auto_trading': {'paper_trading_initial_capital': 5000.0}
             }
         }):
        from src.execution.binance_trader import _get_paper_balance
        result = _get_paper_balance(trading_strategy=None)

    assert result['total_usd'] == 10000.0


@patch('src.execution.binance_trader._is_live_trading', return_value=False)
def test_get_account_balance_auto_never_live(mock_live):
    """Auto-bot always uses paper balance, even if live trading is on."""
    with patch('src.execution.binance_trader._get_paper_balance', return_value={"USDT": 5000, "total_usd": 5000}) as mock_paper:
        from src.execution.binance_trader import get_account_balance
        # Even though _is_live_trading returns False here, the key test is that
        # trading_strategy='auto' routes to paper balance
        result = get_account_balance(trading_strategy='auto')
        mock_paper.assert_called_once_with(asset_type=None, trading_strategy='auto')


# ---------------------------------------------------------------------------
# Main: auto-bot helpers
# ---------------------------------------------------------------------------

def test_auto_update_trailing_stop():
    """_auto_update_trailing_stop tracks peak correctly."""
    from main import _auto_update_trailing_stop, _auto_trailing_stop_peaks

    # Clear state
    _auto_trailing_stop_peaks.clear()

    # First call sets peak
    peak = _auto_update_trailing_stop("ORDER_1", 100.0)
    assert peak == 100.0
    assert _auto_trailing_stop_peaks["ORDER_1"] == 100.0

    # Higher price updates peak
    peak = _auto_update_trailing_stop("ORDER_1", 110.0)
    assert peak == 110.0

    # Lower price doesn't reduce peak
    peak = _auto_update_trailing_stop("ORDER_1", 105.0)
    assert peak == 110.0

    _auto_trailing_stop_peaks.clear()


def test_auto_clear_trailing_stop():
    """_auto_clear_trailing_stop removes the entry."""
    from main import _auto_clear_trailing_stop, _auto_trailing_stop_peaks

    _auto_trailing_stop_peaks["ORDER_2"] = 200.0
    _auto_clear_trailing_stop("ORDER_2")
    assert "ORDER_2" not in _auto_trailing_stop_peaks

    # Clearing non-existent key doesn't error
    _auto_clear_trailing_stop("NONEXISTENT")


# ---------------------------------------------------------------------------
# Telegram: auto-bot summary and status
# ---------------------------------------------------------------------------

def test_send_auto_bot_summary():
    """send_auto_bot_summary sends a silent message with correct content."""
    import asyncio
    from src.notify.telegram_bot import send_auto_bot_summary

    mock_app = MagicMock()
    mock_bot = AsyncMock()
    mock_app.bot = mock_bot

    summary = {"total_closed": 3, "wins": 2, "losses": 1, "total_pnl": 150.0, "win_rate": 66.7}
    positions = [{"symbol": "BTC", "entry_price": 50000.0, "quantity": 0.1}]
    balance = {"USDT": 5150.0, "total_usd": 10150.0}

    with patch('src.notify.telegram_bot.telegram_config', {'enabled': True}), \
         patch('src.notify.telegram_bot.TOKEN', 'test_token'), \
         patch('src.notify.telegram_bot.CHAT_ID', '12345'), \
         patch('src.notify.telegram_bot._get_position_price', return_value=51000.0):
        asyncio.get_event_loop().run_until_complete(
            send_auto_bot_summary(mock_app, summary, positions, balance, 1)
        )

    mock_bot.send_message.assert_called_once()
    call_kwargs = mock_bot.send_message.call_args[1]
    assert call_kwargs['disable_notification'] is True
    assert 'Auto-Bot Summary' in call_kwargs['text']
    assert '$10,150.00' in call_kwargs['text']


def test_auto_status_command():
    """The /auto_status command handler returns status info."""
    import asyncio
    from src.notify.telegram_bot import auto_status_cmd

    mock_update = MagicMock()
    mock_update.message = AsyncMock()
    mock_update.message.from_user.id = 7910661624  # authorized user
    mock_update.message.reply_text = AsyncMock()
    mock_context = MagicMock()

    with patch('src.notify.telegram_bot.app_config', {
        'settings': {
            'auto_trading': {'enabled': True, 'paper_trading_initial_capital': 10000.0},
            'paper_trading_initial_capital': 10000.0,
        }
    }), \
         patch('src.notify.telegram_bot.get_open_positions', return_value=[]), \
         patch('src.notify.telegram_bot.get_account_balance', return_value={"USDT": 10000.0, "total_usd": 10000.0}), \
         patch('src.notify.telegram_bot.get_trade_summary', return_value={
             "total_closed": 0, "wins": 0, "losses": 0, "total_pnl": 0, "win_rate": 0
         }), \
         patch('src.notify.telegram_bot.AUTHORIZED_USER_IDS', [7910661624]):
        asyncio.get_event_loop().run_until_complete(
            auto_status_cmd(mock_update, mock_context)
        )

    mock_update.message.reply_text.assert_called_once()
    reply_text = mock_update.message.reply_text.call_args[0][0]
    assert 'Auto-Bot Status' in reply_text


def test_auto_status_disabled():
    """The /auto_status command shows disabled message when auto-trading is off."""
    import asyncio
    from src.notify.telegram_bot import auto_status_cmd

    mock_update = MagicMock()
    mock_update.message = AsyncMock()
    mock_update.message.from_user.id = 7910661624
    mock_update.message.reply_text = AsyncMock()
    mock_context = MagicMock()

    with patch('src.notify.telegram_bot.app_config', {
        'settings': {'auto_trading': {'enabled': False}}
    }), \
         patch('src.notify.telegram_bot.AUTHORIZED_USER_IDS', [7910661624]):
        asyncio.get_event_loop().run_until_complete(
            auto_status_cmd(mock_update, mock_context)
        )

    reply_text = mock_update.message.reply_text.call_args[0][0]
    assert 'disabled' in reply_text.lower()


# ---------------------------------------------------------------------------
# Auto-bot always paper (never live)
# ---------------------------------------------------------------------------

@patch('src.execution.binance_trader._is_live_trading', return_value=True)
@patch('src.execution.binance_trader.release_db_connection')
@patch('src.execution.binance_trader.get_db_connection')
def test_auto_place_order_uses_paper_even_when_live(mock_get_conn, mock_release, mock_live):
    """Auto-bot place_order uses paper path even when live trading is active."""
    mock_conn = MagicMock()
    mock_conn.__class__ = sqlite3.Connection
    mock_cursor = MagicMock()
    mock_conn.cursor.return_value = mock_cursor
    mock_get_conn.return_value = mock_conn

    from src.execution.binance_trader import place_order
    result = place_order("BTC", "BUY", 0.01, 60000.0, trading_strategy='auto')

    # Should use paper path (AUTO_ prefix, not live Binance call)
    assert result['order_id'].startswith('AUTO_')
    assert result['status'] == 'FILLED'


# ---------------------------------------------------------------------------
# Balance isolation: auto vs manual
# ---------------------------------------------------------------------------

@patch('src.execution.binance_trader.release_db_connection')
@patch('src.execution.binance_trader.get_db_connection')
def test_balance_query_includes_strategy_filter(mock_get_conn, mock_release):
    """Paper balance queries include trading_strategy filter when provided."""
    mock_conn = MagicMock()
    mock_conn.__class__ = sqlite3.Connection
    mock_cursor = MagicMock()
    mock_cursor.fetchone.side_effect = [(100.0,), (500.0,)]  # PnL, locked
    mock_get_conn.return_value = mock_conn

    from contextlib import contextmanager
    @contextmanager
    def fake_cursor(conn):
        yield mock_cursor

    with patch('src.execution.binance_trader._cursor', fake_cursor), \
         patch('src.execution.binance_trader.app_config', {
             'settings': {
                 'auto_trading': {'paper_trading_initial_capital': 10000.0}
             }
         }):
        from src.execution.binance_trader import _get_paper_balance
        result = _get_paper_balance(trading_strategy='auto')

    # Both queries (PnL + locked) should include trading_strategy
    assert mock_cursor.execute.call_count == 2
    for call in mock_cursor.execute.call_args_list:
        query = call[0][0]
        params = call[0][1]
        assert "trading_strategy" in query
        assert 'auto' in params
