# tests/test_binance_data.py

import pytest
from unittest.mock import patch, MagicMock
from src.collectors.binance_data import get_current_price
import requests
import psycopg2

# --- Test Fixtures ---

@pytest.fixture
def mock_db_connection():
    """Fixture to mock the database connection and cursor."""
    with patch('src.collectors.binance_data.get_db_connection') as mock_get_conn, \
         patch('src.collectors.binance_data.release_db_connection'):
        mock_conn = MagicMock()
        # Simulate a PostgreSQL connection for the isinstance check
        mock_conn.__class__ = psycopg2.extensions.connection
        mock_cursor = MagicMock()
        mock_conn.cursor.return_value = mock_cursor
        mock_get_conn.return_value = mock_conn
        yield mock_get_conn

# --- Test Cases for get_current_price ---

@patch('src.collectors.binance_data.requests.get')
def test_get_current_price_success(mock_requests_get, mock_db_connection):
    """
    Tests the successful fetching and saving of a price from the Binance API.
    """
    # Arrange: Configure the mock API response
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {'symbol': 'BTCUSDT', 'price': '50000.00'}
    mock_response.raise_for_status.return_value = None
    mock_requests_get.return_value = mock_response

    # Act: Call the function
    result = get_current_price('BTCUSDT')

    # Assert: Check the outcome
    # 1. The API was called correctly (with timeout)
    mock_requests_get.assert_called_once_with(
        "https://api.binance.com/api/v3/ticker/price",
        params={'symbol': 'BTCUSDT'},
        timeout=30
    )
    # 2. The function returned the correct data
    assert result == {'symbol': 'BTCUSDT', 'price': '50000.00'}
    # 3. The database connection was opened and the data was saved
    mock_db_connection.assert_called_once()
    mock_cursor = mock_db_connection.return_value.cursor.return_value
    mock_cursor.execute.assert_called_once_with(
        'INSERT INTO market_prices (symbol, price) VALUES (%s, %s)', ('BTCUSDT', '50000.00')
    )
    mock_db_connection.return_value.commit.assert_called_once()


@patch('src.collectors.binance_data.requests.get')
def test_get_current_price_invalid_symbol(mock_requests_get, mock_db_connection):
    """
    Tests how the function handles an HTTP 400 error for an invalid symbol.
    """
    # Arrange: Configure the mock API response for an error
    mock_response = MagicMock()
    mock_response.status_code = 400
    http_error = requests.exceptions.HTTPError("400 Client Error")
    http_error.response = mock_response
    mock_response.raise_for_status.side_effect = http_error
    mock_requests_get.return_value = mock_response

    # Act: Call the function
    result = get_current_price('INVALID')

    # Assert: Check the outcome
    # 1. The function should return None
    assert result is None
    # 2. The database should NOT be called
    mock_db_connection.assert_not_called()


@patch('src.collectors.binance_data.time.sleep')
@patch('src.collectors.binance_data.requests.get')
def test_get_current_price_network_error(mock_requests_get, mock_sleep, mock_db_connection):
    """
    Tests how the function handles a network-level error (e.g., timeout).
    Verifies retry behavior with exponential backoff.
    """
    # Arrange: Configure the mock to raise a network exception
    mock_requests_get.side_effect = requests.exceptions.RequestException("Connection error")

    # Act: Call the function
    result = get_current_price('BTCUSDT')

    # Assert: Check the outcome
    # 1. The function should return None after all retries
    assert result is None
    # 2. Should have been called MAX_RETRIES times
    assert mock_requests_get.call_count == 3
    # 3. Should have slept between retries (backoff)
    assert mock_sleep.call_count == 2
    # 4. The database should NOT be called
    mock_db_connection.assert_not_called()
