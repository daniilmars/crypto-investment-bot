# tests/test_whale_alert.py

import pytest
from unittest.mock import patch, MagicMock
from src.collectors.whale_alert import get_whale_transactions
import requests

# --- Test Cases for get_whale_transactions ---

@patch('src.collectors.whale_alert.requests.get')
@patch('src.collectors.whale_alert.get_db_connection')
@patch('src.collectors.whale_alert.app_config')
def test_get_whale_transactions_success(mock_app_config, mock_get_db_connection, mock_requests_get):
    """
    Tests the successful fetching and saving of whale transactions.
    """
    # Arrange
    # 1. Mock the config
    mock_app_config.get.return_value.get.return_value = 'test_api_key'
    
    # 2. Mock the API response
    mock_api_data = {
        'result': 'success',
        'transactions': [
            {'id': 1, 'symbol': 'BTC', 'timestamp': 1634169600, 'amount_usd': 5000000, 'from': {'owner': 'unknown', 'owner_type': 'wallet'}, 'to': {'owner': 'binance', 'owner_type': 'exchange'}},
            {'id': 2, 'symbol': 'ETH', 'timestamp': 1634083200, 'amount_usd': 2000000, 'from': {'owner': 'kraken', 'owner_type': 'exchange'}, 'to': {'owner': 'unknown', 'owner_type': 'wallet'}}
        ]
    }
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = mock_api_data
    mock_response.raise_for_status.return_value = None
    mock_requests_get.return_value = mock_response

    # 3. Mock the database connection
    mock_conn = MagicMock()
    mock_cursor = MagicMock()
    mock_get_db_connection.return_value = mock_conn
    mock_conn.cursor.return_value = mock_cursor

    # Act
    result = get_whale_transactions()

    # Assert
    assert result == mock_api_data['transactions']
    mock_requests_get.assert_called_once()
    mock_get_db_connection.assert_called_once()
    assert mock_cursor.execute.call_count == 2

@patch('src.collectors.whale_alert.requests.get')
@patch('src.collectors.whale_alert.app_config')
def test_get_whale_transactions_api_error(mock_app_config, mock_requests_get):
    """
    Tests how the function handles an API error message (result != 'success').
    """
    # Arrange
    mock_app_config.get.return_value.get.return_value = 'test_api_key'
    mock_api_data = {'result': 'error', 'message': 'Invalid API key'}
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = mock_api_data
    mock_requests_get.return_value = mock_response

    # Act
    result = get_whale_transactions()

    # Assert
    assert result is None

@patch('src.collectors.whale_alert.requests.get')
@patch('src.collectors.whale_alert.app_config')
def test_get_whale_transactions_network_error(mock_app_config, mock_requests_get):
    """
    Tests how the function handles a network-level error.
    """
    # Arrange
    mock_app_config.get.return_value.get.return_value = 'test_api_key'
    mock_requests_get.side_effect = requests.exceptions.RequestException("Connection error")

    # Act
    result = get_whale_transactions()

    # Assert
    assert result is None

@patch('src.collectors.whale_alert.app_config')
def test_get_whale_transactions_no_api_key(mock_app_config):
    """
    Tests that the function returns None if the API key is not configured.
    """
    # Arrange
    mock_app_config.get.return_value.get.return_value = None

    # Act
    result = get_whale_transactions()

    # Assert
    assert result is None