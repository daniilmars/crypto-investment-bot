"""Tests for position reconciliation at startup (Fix 3).

Verifies that:
- Paper mode skips reconciliation
- Live mode marks stale DB positions as CLOSED with exit_reason='reconciled_stale'
- Exchange positions not in DB are logged but not touched
- Errors don't crash startup
"""
import sqlite3
from unittest.mock import patch, MagicMock


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_db_position(symbol, order_id, status='OPEN', asset_type='crypto'):
    return {
        'id': 1,
        'symbol': symbol,
        'order_id': order_id,
        'side': 'BUY',
        'entry_price': 100.0,
        'quantity': 1.0,
        'status': status,
        'pnl': None,
        'exit_price': None,
        'asset_type': asset_type,
        'trading_strategy': 'manual',
    }


# ---------------------------------------------------------------------------
# Crypto reconciliation
# ---------------------------------------------------------------------------

class TestReconcileCryptoPositions:

    @patch('src.execution.binance_trader.app_config', {
        'settings': {'paper_trading': True}
    })
    def test_paper_mode_skips(self):
        from src.execution.binance_trader import reconcile_crypto_positions
        assert reconcile_crypto_positions() == 0

    @patch('src.execution.binance_trader.app_config', {
        'settings': {
            'paper_trading': False,
            'live_trading': {'enabled': False, 'mode': 'testnet'},
        }
    })
    def test_live_not_enabled_skips(self):
        from src.execution.binance_trader import reconcile_crypto_positions
        assert reconcile_crypto_positions() == 0

    @patch('src.execution.binance_trader.release_db_connection')
    @patch('src.execution.binance_trader.get_db_connection')
    @patch('src.execution.binance_trader.get_open_positions')
    @patch('src.execution.binance_trader._get_binance_client')
    @patch('src.execution.binance_trader._is_live_trading', return_value=True)
    @patch('src.execution.binance_trader.app_config', {
        'settings': {'paper_trading': False, 'live_trading': {'enabled': True, 'mode': 'testnet'}}
    })
    def test_stale_position_closed(self, mock_is_live, mock_client, mock_positions,
                                    mock_db_conn, mock_release):
        """DB position not found on exchange → marked CLOSED."""
        # Exchange has BTC only
        mock_binance = MagicMock()
        mock_binance.get_account.return_value = {
            'balances': [
                {'asset': 'BTC', 'free': '0.5', 'locked': '0'},
                {'asset': 'USDT', 'free': '100', 'locked': '0'},
            ]
        }
        mock_client.return_value = mock_binance

        # DB has ETH position (not on exchange)
        mock_positions.return_value = [
            _make_db_position('ETHUSDT', 'ORDER_ETH_1'),
        ]

        # Mock DB connection with sqlite
        conn = sqlite3.connect(':memory:')
        conn.execute('CREATE TABLE trades (order_id TEXT, status TEXT, '
                     'exit_reason TEXT, exit_timestamp TEXT)')
        conn.execute("INSERT INTO trades VALUES ('ORDER_ETH_1', 'OPEN', NULL, NULL)")
        mock_db_conn.return_value = conn

        from src.execution.binance_trader import reconcile_crypto_positions
        result = reconcile_crypto_positions()

        assert result == 1
        row = conn.execute("SELECT status, exit_reason FROM trades WHERE order_id='ORDER_ETH_1'").fetchone()
        assert row[0] == 'CLOSED'
        assert row[1] == 'reconciled_stale'
        conn.close()

    @patch('src.execution.binance_trader.get_open_positions')
    @patch('src.execution.binance_trader._get_binance_client')
    @patch('src.execution.binance_trader._is_live_trading', return_value=True)
    @patch('src.execution.binance_trader.app_config', {
        'settings': {'paper_trading': False, 'live_trading': {'enabled': True, 'mode': 'testnet'}}
    })
    def test_exchange_only_position_logged(self, mock_is_live, mock_client, mock_positions):
        """Exchange position not in DB → only a warning, no crash."""
        mock_binance = MagicMock()
        mock_binance.get_account.return_value = {
            'balances': [
                {'asset': 'SOL', 'free': '10', 'locked': '0'},
                {'asset': 'USDT', 'free': '100', 'locked': '0'},
            ]
        }
        mock_client.return_value = mock_binance
        mock_positions.return_value = []  # DB empty

        from src.execution.binance_trader import reconcile_crypto_positions
        result = reconcile_crypto_positions()
        assert result == 0  # nothing to close

    @patch('src.execution.binance_trader.get_open_positions')
    @patch('src.execution.binance_trader._get_binance_client')
    @patch('src.execution.binance_trader._is_live_trading', return_value=True)
    @patch('src.execution.binance_trader.app_config', {
        'settings': {'paper_trading': False, 'live_trading': {'enabled': True, 'mode': 'testnet'}}
    })
    def test_matching_positions_untouched(self, mock_is_live, mock_client, mock_positions):
        """DB and exchange agree → no changes."""
        mock_binance = MagicMock()
        mock_binance.get_account.return_value = {
            'balances': [
                {'asset': 'BTC', 'free': '0.5', 'locked': '0'},
                {'asset': 'USDT', 'free': '100', 'locked': '0'},
            ]
        }
        mock_client.return_value = mock_binance
        mock_positions.return_value = [
            _make_db_position('BTCUSDT', 'ORDER_BTC_1'),
        ]

        from src.execution.binance_trader import reconcile_crypto_positions
        result = reconcile_crypto_positions()
        assert result == 0

    @patch('src.execution.binance_trader._get_binance_client', return_value=None)
    @patch('src.execution.binance_trader._is_live_trading', return_value=True)
    @patch('src.execution.binance_trader.app_config', {
        'settings': {'paper_trading': False, 'live_trading': {'enabled': True, 'mode': 'testnet'}}
    })
    def test_client_unavailable_skips(self, mock_is_live, mock_client):
        """No Binance client → skip gracefully."""
        from src.execution.binance_trader import reconcile_crypto_positions
        assert reconcile_crypto_positions() == 0

    @patch('src.execution.binance_trader._get_binance_client')
    @patch('src.execution.binance_trader._is_live_trading', return_value=True)
    @patch('src.execution.binance_trader.app_config', {
        'settings': {'paper_trading': False, 'live_trading': {'enabled': True, 'mode': 'testnet'}}
    })
    def test_api_error_doesnt_crash(self, mock_is_live, mock_client):
        """Exchange API error → returns 0, no exception."""
        mock_binance = MagicMock()
        mock_binance.get_account.side_effect = Exception("API timeout")
        mock_client.return_value = mock_binance

        from src.execution.binance_trader import reconcile_crypto_positions
        assert reconcile_crypto_positions() == 0


# ---------------------------------------------------------------------------
# Stock reconciliation
# ---------------------------------------------------------------------------

class TestReconcileStockPositions:

    @patch('src.execution.stock_trader.app_config', {
        'settings': {'stock_trading': {'broker': 'paper_only'}}
    })
    def test_paper_only_skips(self):
        from src.execution.stock_trader import reconcile_stock_positions
        assert reconcile_stock_positions() == 0

    @patch('src.execution.stock_trader._get_alpaca_client', return_value=None)
    @patch('src.execution.stock_trader.app_config', {
        'settings': {'stock_trading': {'broker': 'alpaca'}}
    })
    def test_client_unavailable_skips(self, mock_client):
        from src.execution.stock_trader import reconcile_stock_positions
        assert reconcile_stock_positions() == 0

    @patch('src.execution.stock_trader.release_db_connection')
    @patch('src.execution.stock_trader.get_db_connection')
    @patch('src.execution.binance_trader.get_open_positions')
    @patch('src.execution.stock_trader._get_alpaca_client')
    @patch('src.execution.stock_trader.app_config', {
        'settings': {'stock_trading': {'broker': 'alpaca'}}
    })
    def test_stale_stock_position_closed(self, mock_client, mock_positions,
                                          mock_db_conn, mock_release):
        """DB stock position not on exchange → marked CLOSED."""
        # Exchange has AAPL only
        mock_alpaca = MagicMock()
        mock_pos = MagicMock()
        mock_pos.symbol = 'AAPL'
        mock_alpaca.get_all_positions.return_value = [mock_pos]
        mock_client.return_value = mock_alpaca

        # DB has MSFT (not on exchange)
        mock_positions.return_value = [
            _make_db_position('MSFT', 'ORDER_MSFT_1', asset_type='stock'),
        ]

        conn = sqlite3.connect(':memory:')
        conn.execute('CREATE TABLE trades (order_id TEXT, status TEXT, '
                     'exit_reason TEXT, exit_timestamp TEXT)')
        conn.execute("INSERT INTO trades VALUES ('ORDER_MSFT_1', 'OPEN', NULL, NULL)")
        mock_db_conn.return_value = conn

        from src.execution.stock_trader import reconcile_stock_positions
        result = reconcile_stock_positions()

        assert result == 1
        row = conn.execute("SELECT status, exit_reason FROM trades WHERE order_id='ORDER_MSFT_1'").fetchone()
        assert row[0] == 'CLOSED'
        assert row[1] == 'reconciled_stale'
        conn.close()

    @patch('src.execution.stock_trader._get_alpaca_client')
    @patch('src.execution.stock_trader.app_config', {
        'settings': {'stock_trading': {'broker': 'alpaca'}}
    })
    def test_api_error_doesnt_crash(self, mock_client):
        """Alpaca API error → returns 0, no exception."""
        mock_alpaca = MagicMock()
        mock_alpaca.get_all_positions.side_effect = Exception("API timeout")
        mock_client.return_value = mock_alpaca

        from src.execution.stock_trader import reconcile_stock_positions
        assert reconcile_stock_positions() == 0
