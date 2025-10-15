# tests/test_database.py

import pytest
import sqlite3
import os
import time
from src.database import get_db_connection, initialize_database

# --- Test Fixtures ---

@pytest.fixture
def test_db():
    """
    Fixture to set up and yield an open, initialized in-memory SQLite database connection.
    The connection is automatically closed after the test.
    """
    conn = get_db_connection(":memory:")
    # Pass the connection object directly to the initializer
    initialize_database(connection=conn)
    yield conn
    conn.close()

@pytest.fixture
def test_db_file(tmp_path):
    """
    Fixture to set up a temporary database file for testing.
    This is useful for tests that need to check file existence or path handling.
    """
    db_path = tmp_path / "test_crypto.db"
    initialize_database(str(db_path))
    yield str(db_path)
    # Cleanup: the tmp_path fixture handles file deletion

# --- Test Cases ---

def test_initialize_database_creates_tables(test_db):
    """
    Tests that the initialize_database function correctly creates all expected tables.
    """
    # The 'test_db' fixture now provides an open, initialized connection
    cursor = test_db.cursor()
    
    # Get the list of tables from the database schema
    cursor.execute("SELECT name FROM sqlite_master WHERE type='table';")
    tables = [row[0] for row in cursor.fetchall()]
    
    # Define the expected tables
    expected_tables = [
        'market_prices', 
        'whale_transactions'
    ]
    
    # Check if all expected tables were created
    for table in expected_tables:
        assert table in tables
        
    # No need to close the connection here, the fixture handles it.

def test_get_db_connection(test_db_file):
    """
    Tests that a database connection can be successfully established.
    """
    conn = None
    try:
        conn = get_db_connection(test_db_file)
        assert isinstance(conn, sqlite3.Connection)
        # Check if the connection is usable
        cursor = conn.cursor()
        cursor.execute("SELECT 1")
        result = cursor.fetchone()
        assert result[0] == 1
    finally:
        if conn:
            conn.close()

def test_database_file_creation(tmp_path):
    """
    Tests that initialize_database creates a new database file if it doesn't exist.
    """
    db_path = tmp_path / "new_db.db"
    assert not os.path.exists(db_path)
    
    initialize_database(str(db_path))
    
    assert os.path.exists(db_path)

def test_get_historical_prices(test_db):
    """
    Tests the get_historical_prices function to ensure it retrieves the correct data.
    """
    # Arrange: Insert some mock data into the in-memory database
    cursor = test_db.cursor()
    mock_prices = [
        ('BTCUSDT', 50000, 1), ('BTCUSDT', 50100, 2), ('BTCUSDT', 50200, 3),
        ('BTCUSDT', 50300, 4), ('BTCUSDT', 50400, 5), ('BTCUSDT', 50500, 6),
        ('ETHUSDT', 4000, 7)
    ]
    cursor.executemany("INSERT INTO market_prices (symbol, price, timestamp) VALUES (?, ?, ?)", mock_prices)
    test_db.commit()

    # Act: Call the function to get the last 5 prices for BTCUSDT
    from src.database import get_historical_prices
    prices = get_historical_prices('BTCUSDT', limit=5, connection=test_db)

    # Assert: Check if the returned data is correct
    # 1. It should return 5 prices.
    assert len(prices) == 5
    # 2. It should return the most recent prices, in oldest-to-newest order.
    assert prices == [50100, 50200, 50300, 50400, 50500]

def test_get_transaction_timestamps_since(test_db):
    """
    Tests the get_transaction_timestamps_since function to ensure it retrieves the correct timestamps.
    """
    # Arrange: Insert mock transaction data
    cursor = test_db.cursor()
    now = int(time.time())
    mock_transactions = [
        ('1', 'btc', now - 3600, 1000000),      # 1 hour ago
        ('2', 'btc', now - 7200, 1000000),      # 2 hours ago
        ('3', 'eth', now - 3700, 1000000),      # ETH transaction, should be ignored
        ('4', 'btc', now - (3600 * 5), 1000000) # 5 hours ago, should be ignored by 4hr lookback
    ]
    cursor.executemany("INSERT INTO whale_transactions (id, symbol, timestamp, amount_usd) VALUES (?, ?, ?, ?)", mock_transactions)
    test_db.commit()
    
    # Act: Call the function to get timestamps for 'btc' in the last 4 hours
    from src.database import get_transaction_timestamps_since
    timestamps = get_transaction_timestamps_since('btc', hours_ago=4, connection=test_db)

    # Assert: Check if the returned timestamps are correct
    assert len(timestamps) == 2
    assert (now - 3600) in timestamps
    assert (now - 7200) in timestamps
    assert (now - (3600 * 5)) not in timestamps
