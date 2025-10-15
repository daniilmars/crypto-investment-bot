# tests/test_database.py

import pytest
from unittest.mock import patch, MagicMock
import sqlite3
import os

# --- Mocks ---

# It's better to patch the functions from where they are *used*, not where they are defined.
# However, since these tests are specifically for the database module, we can patch them here.
@patch('src.database.get_db_connection')
def test_initialize_database_creates_tables(mock_get_db_connection):
    """
    Tests that the initialize_database function correctly creates all expected tables.
    """
    # Arrange: Set up a mock for the database connection and cursor
    mock_conn = MagicMock()
    mock_cursor = MagicMock()
    mock_get_db_connection.return_value = mock_conn
    mock_conn.cursor.return_value = mock_cursor

    # Act: Call the function to be tested
    from src.database import initialize_database
    initialize_database()

    # Assert: Check if the CREATE TABLE statements were executed
    assert mock_cursor.execute.call_count == 2
    
    # Check the SQL statements (case-insensitive and ignoring whitespace)
    first_call_args = ' '.join(mock_cursor.execute.call_args_list[0][0][0].split())
    assert "CREATE TABLE IF NOT EXISTS market_prices" in first_call_args
    
    second_call_args = ' '.join(mock_cursor.execute.call_args_list[1][0][0].split())
    assert "CREATE TABLE IF NOT EXISTS whale_transactions" in second_call_args
    
    mock_conn.commit.assert_called_once()
    mock_cursor.close.assert_called_once()
    mock_conn.close.assert_called_once()


@patch('src.database.get_db_connection')
def test_get_historical_prices(mock_get_db_connection):
    """
    Tests the get_historical_prices function to ensure it retrieves and processes data correctly.
    """
    # Arrange
    mock_conn = MagicMock()
    mock_cursor = MagicMock()
    mock_get_db_connection.return_value = mock_conn
    mock_conn.cursor.return_value = mock_cursor
    
    # Simulate the data that would be returned from the database
    mock_cursor.fetchall.return_value = [(50500,), (50400,), (50300,), (50200,), (50100,)]

    # Act
    from src.database import get_historical_prices
    prices = get_historical_prices('BTCUSDT', limit=5)

    # Assert
    # 1. Check if the correct query was executed
    mock_cursor.execute.assert_called_once()
    # 2. Check if the returned data is correct (should be reversed to oldest-to-newest)
    assert prices == [50100, 50200, 50300, 50400, 50500]


@patch('src.database.get_db_connection')
def test_get_transaction_timestamps_since(mock_get_db_connection):
    """
    Tests the get_transaction_timestamps_since function.
    """
    # Arrange
    mock_conn = MagicMock()
    mock_cursor = MagicMock()
    mock_get_db_connection.return_value = mock_conn
    mock_conn.cursor.return_value = mock_cursor
    
    mock_cursor.fetchall.return_value = [(1672531200,), (1672534800,)] # Example timestamps

    # Act
    from src.database import get_transaction_timestamps_since
    timestamps = get_transaction_timestamps_since('btc', hours_ago=4)

    # Assert
    mock_cursor.execute.assert_called_once()
    assert timestamps == [1672531200, 1672534800]