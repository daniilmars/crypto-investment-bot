# tests/test_stock_trader.py
"""Tests for stock trader module: Alpaca order placement, PDT tracking, market hours."""

import pytest
from unittest.mock import patch, MagicMock
from datetime import datetime, timezone, timedelta


class TestMarketHours:
    """Tests for market hours checking."""

    @patch('src.execution.stock_trader._get_alpaca_client')
    def test_market_open_via_alpaca(self, mock_client_fn):
        from src.execution.stock_trader import _is_market_open
        mock_client = MagicMock()
        mock_clock = MagicMock()
        mock_clock.is_open = True
        mock_client.get_clock.return_value = mock_clock
        mock_client_fn.return_value = mock_client

        assert _is_market_open() is True

    @patch('src.execution.stock_trader._get_alpaca_client')
    def test_market_closed_via_alpaca(self, mock_client_fn):
        from src.execution.stock_trader import _is_market_open
        mock_client = MagicMock()
        mock_clock = MagicMock()
        mock_clock.is_open = False
        mock_client.get_clock.return_value = mock_clock
        mock_client_fn.return_value = mock_client

        assert _is_market_open() is False

    @patch('src.execution.stock_trader._get_alpaca_client')
    def test_get_market_hours(self, mock_client_fn):
        from src.execution.stock_trader import get_market_hours
        mock_client = MagicMock()
        mock_clock = MagicMock()
        mock_clock.is_open = True
        mock_clock.next_open = '2025-01-02T09:30:00-05:00'
        mock_clock.next_close = '2025-01-02T16:00:00-05:00'
        mock_client.get_clock.return_value = mock_clock
        mock_client_fn.return_value = mock_client

        hours = get_market_hours()
        assert hours['is_open'] is True
        assert 'next_open' in hours
        assert 'next_close' in hours


class TestPDTRule:
    """Tests for Pattern Day Trader rule tracking."""

    def test_pdt_initial_state(self):
        from src.execution.stock_trader import _check_pdt_rule, _day_trades
        _day_trades.clear()
        pdt = _check_pdt_rule()
        assert pdt['day_trades_used'] == 0
        assert pdt['day_trades_remaining'] == 3
        assert pdt['is_restricted'] is False

    def test_pdt_after_trades(self):
        from src.execution.stock_trader import _check_pdt_rule, _record_day_trade, _day_trades
        _day_trades.clear()
        _record_day_trade()
        _record_day_trade()

        pdt = _check_pdt_rule()
        assert pdt['day_trades_used'] == 2
        assert pdt['day_trades_remaining'] == 1
        assert pdt['is_restricted'] is False

    def test_pdt_restricted(self):
        from src.execution.stock_trader import _check_pdt_rule, _record_day_trade, _day_trades
        _day_trades.clear()
        _record_day_trade()
        _record_day_trade()
        _record_day_trade()

        pdt = _check_pdt_rule()
        assert pdt['day_trades_used'] == 3
        assert pdt['day_trades_remaining'] == 0
        assert pdt['is_restricted'] is True

    def test_pdt_old_trades_expire(self):
        from src.execution.stock_trader import _check_pdt_rule, _day_trades
        _day_trades.clear()
        # Add trades from 8 days ago — should be expired
        old_time = datetime.now(timezone.utc) - timedelta(days=8)
        _day_trades.append(old_time)
        _day_trades.append(old_time)
        _day_trades.append(old_time)

        pdt = _check_pdt_rule()
        assert pdt['day_trades_used'] == 0
        assert pdt['is_restricted'] is False


class TestPlaceStockOrder:
    """Tests for Alpaca order placement."""

    @patch('src.execution.stock_trader._record_stock_trade')
    @patch('src.execution.stock_trader._place_bracket_order', return_value=None)
    @patch('src.execution.stock_trader._is_market_open', return_value=True)
    @patch('src.execution.stock_trader._get_alpaca_client')
    def test_buy_order_success(self, mock_client_fn, mock_market, mock_bracket, mock_record):
        import sys
        # Mock the alpaca modules before importing
        mock_alpaca_trading = MagicMock()
        mock_alpaca_requests = MagicMock()
        mock_alpaca_enums = MagicMock()
        sys.modules['alpaca'] = MagicMock()
        sys.modules['alpaca.trading'] = MagicMock()
        sys.modules['alpaca.trading.requests'] = mock_alpaca_requests
        sys.modules['alpaca.trading.enums'] = mock_alpaca_enums

        from src.execution.stock_trader import place_stock_order

        mock_client = MagicMock()
        mock_order = MagicMock()
        mock_order.id = 'test-order-123'
        mock_order.filled_avg_price = 150.0
        mock_order.filled_qty = 10.0
        mock_client.submit_order.return_value = mock_order
        mock_client_fn.return_value = mock_client

        result = place_stock_order('AAPL', 'BUY', 10.0, 150.0)
        assert result['status'] == 'FILLED'
        assert result['symbol'] == 'AAPL'
        assert result['side'] == 'BUY'
        assert result['price'] == 150.0
        mock_record.assert_called_once()

        # Clean up mocked modules
        for mod in ['alpaca', 'alpaca.trading', 'alpaca.trading.requests', 'alpaca.trading.enums']:
            sys.modules.pop(mod, None)

    @patch('src.execution.stock_trader._is_market_open', return_value=False)
    @patch('src.execution.stock_trader._get_alpaca_client')
    def test_buy_market_closed(self, mock_client_fn, mock_market):
        from src.execution.stock_trader import place_stock_order
        mock_client_fn.return_value = MagicMock()

        result = place_stock_order('AAPL', 'BUY', 10.0, 150.0)
        assert result['status'] == 'FAILED'
        assert 'closed' in result['message'].lower()

    @patch('src.execution.stock_trader._get_alpaca_client', return_value=None)
    def test_buy_no_client(self, mock_client_fn):
        from src.execution.stock_trader import place_stock_order
        result = place_stock_order('AAPL', 'BUY', 10.0, 150.0)
        assert result['status'] == 'FAILED'
        assert 'not available' in result['message'].lower()


class TestStockPositions:
    """Tests for fetching Alpaca positions and balance."""

    @patch('src.execution.stock_trader._get_alpaca_client')
    def test_get_stock_positions(self, mock_client_fn):
        from src.execution.stock_trader import get_stock_positions

        mock_pos = MagicMock()
        mock_pos.symbol = 'AAPL'
        mock_pos.qty = '10'
        mock_pos.avg_entry_price = '150.0'
        mock_pos.current_price = '155.0'
        mock_pos.market_value = '1550.0'
        mock_pos.unrealized_pl = '50.0'
        mock_pos.unrealized_plpc = '0.0333'

        mock_client = MagicMock()
        mock_client.get_all_positions.return_value = [mock_pos]
        mock_client_fn.return_value = mock_client

        positions = get_stock_positions()
        assert len(positions) == 1
        assert positions[0]['symbol'] == 'AAPL'
        assert positions[0]['quantity'] == 10.0
        assert positions[0]['unrealized_pl'] == 50.0

    @patch('src.execution.stock_trader._get_alpaca_client', return_value=None)
    def test_get_stock_positions_no_client(self, mock_client_fn):
        from src.execution.stock_trader import get_stock_positions
        assert get_stock_positions() == []

    @patch('src.execution.stock_trader._get_alpaca_client')
    def test_get_stock_balance(self, mock_client_fn):
        from src.execution.stock_trader import get_stock_balance

        mock_account = MagicMock()
        mock_account.cash = '25000.0'
        mock_account.portfolio_value = '30000.0'
        mock_account.buying_power = '50000.0'
        mock_account.equity = '30000.0'

        mock_client = MagicMock()
        mock_client.get_account.return_value = mock_account
        mock_client_fn.return_value = mock_client

        balance = get_stock_balance()
        assert balance['cash'] == 25000.0
        assert balance['portfolio_value'] == 30000.0
        assert balance['buying_power'] == 50000.0

    @patch('src.execution.stock_trader._get_alpaca_client', return_value=None)
    def test_get_stock_balance_no_client(self, mock_client_fn):
        from src.execution.stock_trader import get_stock_balance
        balance = get_stock_balance()
        assert balance['cash'] == 0.0


class TestAssetTypeFiltering:
    """Tests for asset_type parameter in binance_trader functions."""

    @patch('src.execution.binance_trader.release_db_connection')
    @patch('src.execution.binance_trader.get_db_connection')
    def test_get_open_positions_filters_by_asset_type(self, mock_get_conn, mock_release):
        from src.execution.binance_trader import get_open_positions

        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_get_conn.return_value = mock_conn
        mock_conn.row_factory = None
        mock_cursor.fetchall.return_value = []
        mock_conn.cursor.return_value = mock_cursor

        get_open_positions(asset_type='stock')
        # Should have been called with asset_type parameter
        call_args = mock_cursor.execute.call_args
        assert 'asset_type' in call_args[0][0]
        assert call_args[0][1] == ("OPEN", "stock")

    @patch('src.execution.binance_trader.release_db_connection')
    @patch('src.execution.binance_trader.get_db_connection')
    def test_get_open_positions_no_filter(self, mock_get_conn, mock_release):
        from src.execution.binance_trader import get_open_positions

        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_get_conn.return_value = mock_conn
        mock_conn.row_factory = None
        mock_cursor.fetchall.return_value = []
        mock_conn.cursor.return_value = mock_cursor

        get_open_positions()
        call_args = mock_cursor.execute.call_args
        assert 'asset_type' not in call_args[0][0]
        assert call_args[0][1] == ("OPEN",)
