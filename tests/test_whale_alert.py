# tests/test_whale_alert.py

import pytest
from unittest.mock import patch, MagicMock
from src.collectors.whale_alert import get_whale_transactions
import requests

# --- Test Fixtures ---

@pytest.fixture
def mock_db_connection():
    """Fixture to mock the database connection and cursor."""
    with patch('src.collectors.whale_alert.get_db_connection') as mock_get_conn:
        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_conn.cursor.return_value = mock_cursor
        mock_get_conn.return_value = mock_conn
        yield mock_get_conn

@pytest.fixture
def mock_config():
    """Fixture to mock the configuration loading."""
    with patch('src.collectors.whale_alert.load_config') as mock_load_config:
        mock_load_config.return_value = {
            'api_keys': {'whale_alert': 'test_api_key'}
        }
        yield mock_load_config

# --- Test Cases for get_whale_transactions ---

@patch('src.collectors.whale_alert.requests.get')
def test_get_whale_transactions_success(mock_requests_get, mock_config, mock_db_connection):
    """
    Tests the successful fetching and saving of whale transactions.
    """
    # Arrange
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

    # Act
    result = get_whale_transactions()

    # Assert
    assert result == mock_api_data['transactions']
    mock_requests_get.assert_called_once()
    mock_db_connection.assert_called_once()
    assert mock_db_connection.return_value.cursor.return_value.execute.call_count == 2

@patch('src.collectors.whale_alert.requests.get')
def test_get_whale_transactions_api_error(mock_requests_get, mock_config, mock_db_connection):
    """
    Tests how the function handles an API error message (result != 'success').
    """
    # Arrange
    mock_api_data = {'result': 'error', 'message': 'Invalid API key'}
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = mock_api_data
    mock_requests_get.return_value = mock_response

    # Act
    result = get_whale_transactions()

    # Assert
    assert result is None
    mock_db_connection.assert_not_called()

@patch('src.collectors.whale_alert.requests.get')
def test_get_whale_transactions_network_error(mock_requests_get, mock_config, mock_db_connection):
    """
    Tests how the function handles a network-level error.
    """
    # Arrange
    mock_requests_get.side_effect = requests.exceptions.RequestException("Connection error")

    # Act
    result = get_whale_transactions()

    # Assert
    assert result is None
    mock_db_connection.assert_not_called()

def test_get_whale_transactions_no_api_key(mock_config, mock_db_connection):
    """
    Tests that the function returns None if the API key is not configured.
    """
    # Arrange
    mock_config.return_value = {'api_keys': {'whale_alert': None}}

    # Act
    result = get_whale_transactions()

    # Assert
    assert result is None
    mock_db_connection.assert_not_called()
