"""Tests for batch price fetching functions."""
import pytest
from unittest.mock import patch, MagicMock


class TestGetAllPrices:
    """Tests for Binance get_all_prices() batch function."""

    @patch('src.collectors.binance_data.requests.get')
    def test_get_all_prices_success(self, mock_get):
        """Returns dict of {symbol: float} from batch API call."""
        from src.collectors.binance_data import get_all_prices

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = [
            {'symbol': 'BTCUSDT', 'price': '65000.50'},
            {'symbol': 'ETHUSDT', 'price': '3500.25'},
            {'symbol': 'SOLUSDT', 'price': '150.00'},
        ]
        mock_response.raise_for_status = MagicMock()
        mock_get.return_value = mock_response

        result = get_all_prices()

        assert isinstance(result, dict)
        assert len(result) == 3
        assert result['BTCUSDT'] == 65000.50
        assert result['ETHUSDT'] == 3500.25
        assert result['SOLUSDT'] == 150.00
        # Verify no params were passed (fetches ALL pairs)
        mock_get.assert_called_once()
        call_kwargs = mock_get.call_args
        assert 'params' not in call_kwargs.kwargs or call_kwargs.kwargs.get('params') is None

    @patch('src.collectors.binance_data.requests.get')
    def test_get_all_prices_failure_returns_empty(self, mock_get):
        """Returns empty dict on complete failure after retries."""
        from src.collectors.binance_data import get_all_prices
        import requests as req

        mock_get.side_effect = req.exceptions.ConnectionError("Connection refused")

        result = get_all_prices()

        assert result == {}


class TestBatchStockPrices:
    """Tests for yfinance batch stock price functions."""

    @patch('src.collectors.alpha_vantage_data.save_price_data')
    @patch('src.collectors.alpha_vantage_data.yf')
    def test_get_batch_stock_prices_success(self, mock_yf, mock_save):
        """Returns price data for multiple symbols from batch download."""
        from src.collectors.alpha_vantage_data import get_batch_stock_prices
        import pandas as pd
        import numpy as np

        # Build a multi-level DataFrame like yf.download returns for multiple symbols
        dates = pd.date_range('2026-02-26', periods=2, freq='D')
        arrays = [
            ['AAPL', 'AAPL', 'SAP.DE', 'SAP.DE'],
            ['Close', 'Volume', 'Close', 'Volume'],
        ]
        tuples = list(zip(*arrays))
        index = pd.MultiIndex.from_tuples(tuples)
        data = pd.DataFrame(
            [[150.0, 1000000.0, 200.0, 500000.0],
             [155.0, 1100000.0, 205.0, 550000.0]],
            index=dates,
            columns=index,
        )
        mock_yf.download.return_value = data

        result = get_batch_stock_prices(['AAPL', 'SAP.DE'])

        assert 'AAPL' in result
        assert result['AAPL']['price'] == 155.0
        assert result['AAPL']['volume'] == 1100000.0
        assert 'SAP.DE' in result
        assert result['SAP.DE']['price'] == 205.0

    @patch('src.collectors.alpha_vantage_data.yf')
    def test_get_batch_stock_prices_empty_returns_empty(self, mock_yf):
        """Returns empty dict when yfinance returns no data."""
        from src.collectors.alpha_vantage_data import get_batch_stock_prices
        import pandas as pd

        mock_yf.download.return_value = pd.DataFrame()

        result = get_batch_stock_prices(['FAKE.XX'])

        assert result == {}

    @patch('src.collectors.alpha_vantage_data.yf')
    def test_get_batch_daily_prices_success(self, mock_yf):
        """Returns daily price series for multiple symbols."""
        from src.collectors.alpha_vantage_data import get_batch_daily_prices
        import pandas as pd

        # Build multi-level DataFrame for 30 days
        dates = pd.date_range('2025-09-01', periods=30, freq='D')
        arrays = [
            ['AAPL'] * 2 + ['7203.T'] * 2,
            ['Close', 'Volume', 'Close', 'Volume'],
        ]
        tuples = list(zip(*arrays))
        index = pd.MultiIndex.from_tuples(tuples)
        data = pd.DataFrame(
            [[150.0 + i, 1000000.0 + i * 100, 2500.0 + i, 50000.0 + i * 10] for i in range(30)],
            index=dates,
            columns=index,
        )
        mock_yf.download.return_value = data

        result = get_batch_daily_prices(['AAPL', '7203.T'])

        assert 'AAPL' in result
        assert len(result['AAPL']['prices']) == 30
        assert '7203.T' in result
        assert len(result['7203.T']['prices']) == 30


class TestStockSymbolValidation:
    """Tests for updated symbol regex supporting international tickers."""

    def test_international_ticker_validation(self):
        """International tickers with dots/hyphens up to 15 chars should be valid."""
        from src.collectors.alpha_vantage_data import _validate_stock_symbol

        assert _validate_stock_symbol('AAPL') is True
        assert _validate_stock_symbol('BRK-B') is True
        assert _validate_stock_symbol('SAP.DE') is True
        assert _validate_stock_symbol('ICICIBANK.NS') is True
        assert _validate_stock_symbol('MAERSK-B.CO') is True
        assert _validate_stock_symbol('005930.KS') is True
        assert _validate_stock_symbol('0700.HK') is True
        assert _validate_stock_symbol('') is False
