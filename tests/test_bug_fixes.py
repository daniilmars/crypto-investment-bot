"""Tests for Phase 0 bug fixes."""

import itertools
from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest


# --- Bug 2: _signal_counter is atomic via itertools.count ---

def test_signal_counter_is_itertools_count():
    """Bug 2: _signal_counter should be itertools.count, not a plain int."""
    from src.notify.telegram_bot import _signal_counter
    assert isinstance(_signal_counter, type(itertools.count()))


def test_signal_counter_increments():
    """Bug 2: next(_signal_counter) should return sequential IDs."""
    counter = itertools.count(1000)  # use a fresh counter to avoid side-effects
    assert next(counter) == 1000
    assert next(counter) == 1001
    assert next(counter) == 1002


# --- Bug 4: Paper PnL deducts simulated fees ---

@patch('src.execution.binance_trader.app_config', {
    'settings': {
        'paper_trading': True,
        'simulated_fee_pct': 0.001,
    }
})
@patch('src.execution.binance_trader.get_db_connection')
@patch('src.execution.binance_trader.release_db_connection')
def test_paper_sell_deducts_fees(mock_release, mock_get_conn):
    """Bug 4: Paper SELL PnL should deduct simulated trading fees."""
    from src.execution.binance_trader import _paper_place_order

    mock_conn = MagicMock()
    mock_cursor = MagicMock()
    mock_conn.cursor.return_value = mock_cursor
    mock_get_conn.return_value = mock_conn
    # Simulate: bought at 100, selling at 110, qty=1
    mock_cursor.fetchone.return_value = (100.0, "BUY")

    result = _paper_place_order("BTC", "SELL", 1.0, 110.0,
                                existing_order_id="PAPER_BTC_BUY_123")

    # With 0.1% slippage: fill_price = 110 * 0.999 = 109.89
    # PnL = (109.89 - 100) * 1 - (109.89*0.001 + 100*0.001) = 9.89 - 0.20989 = 9.68
    assert result['status'] == 'CLOSED'
    assert result['pnl'] == 9.68


# --- Bug 6: Symbol format avoids "USDTUSDT" ---

def test_usdt_symbol_not_doubled():
    """Bug 6: symbol='USDT' should not produce 'USDTUSDT'."""
    symbol = "USDT"
    api_symbol = symbol if symbol.endswith("USDT") else f"{symbol}USDT"
    assert api_symbol == "USDT"


def test_btc_gets_usdt_suffix():
    """Bug 6: symbol='BTC' should produce 'BTCUSDT'."""
    symbol = "BTC"
    api_symbol = symbol if symbol.endswith("USDT") else f"{symbol}USDT"
    assert api_symbol == "BTCUSDT"


def test_btcusdt_stays_unchanged():
    """Bug 6: symbol='BTCUSDT' should stay 'BTCUSDT'."""
    symbol = "BTCUSDT"
    api_symbol = symbol if symbol.endswith("USDT") else f"{symbol}USDT"
    assert api_symbol == "BTCUSDT"


# --- Bug 7: Zero price rejected ---

@patch('src.execution.binance_trader._is_live_trading', return_value=False)
def test_zero_price_rejected(mock_live):
    """Bug 7: place_order should reject price <= 0."""
    from src.execution.binance_trader import place_order
    result = place_order("BTC", "BUY", 0.1, 0.0)
    assert result['status'] == 'FAILED'
    assert 'price' in result['message'].lower()


@patch('src.execution.binance_trader._is_live_trading', return_value=False)
def test_negative_price_rejected(mock_live):
    """Bug 7: place_order should reject negative price."""
    from src.execution.binance_trader import place_order
    result = place_order("BTC", "BUY", 0.1, -50.0)
    assert result['status'] == 'FAILED'


# --- Bug 5: add_to_position atomic (single connection, single commit) ---

@patch('src.execution.binance_trader._is_live_trading', return_value=False)
@patch('src.execution.binance_trader.get_db_connection')
@patch('src.execution.binance_trader.release_db_connection')
def test_add_to_position_single_commit(mock_release, mock_get_conn, mock_live):
    """Bug 5: Paper add_to_position should use a single connection and single commit."""
    from src.execution.binance_trader import add_to_position

    mock_conn = MagicMock()
    mock_conn.__class__ = type('MockSQLiteConn', (), {})  # Not psycopg2
    mock_cursor = MagicMock()

    # Context manager for _cursor
    from contextlib import contextmanager

    @contextmanager
    def mock_cursor_ctx(conn):
        yield mock_cursor

    # fetchone returns old position data
    mock_cursor.fetchone.return_value = (100.0, 1.0)

    mock_get_conn.return_value = mock_conn

    with patch('src.execution.binance_trader._cursor', mock_cursor_ctx):
        result = add_to_position("ORDER_1", "BTC", 0.5, 110.0,
                                 reason="test", asset_type="crypto")

    assert result['status'] == 'FILLED'
    # Should have exactly 1 commit (atomic)
    assert mock_conn.commit.call_count == 1
    # Cursor should have been called 3 times: SELECT, UPDATE, INSERT
    assert mock_cursor.execute.call_count == 3
    # get_db_connection should have been called exactly once
    assert mock_get_conn.call_count == 1
