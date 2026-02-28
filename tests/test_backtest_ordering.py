"""Tests for backtest data ordering assertions (W3)."""

from unittest.mock import patch, MagicMock

import pandas as pd
import pytest

from src.analysis.backtest import DataLoader


class TestBacktestOrdering:

    @patch('src.analysis.backtest.get_db_connection')
    def test_unsorted_data_raises_assertion(self, mock_conn):
        """DataLoader raises AssertionError if prices are not sorted by timestamp ASC."""
        # Create unsorted DataFrame (simulates a DB that returns data out of order)
        unsorted_df = pd.DataFrame({
            'timestamp': ['2025-01-03', '2025-01-01', '2025-01-02'],
            'symbol': ['BTC', 'BTC', 'BTC'],
            'price': [100, 98, 99],
        })

        # Patch pd.read_sql_query to return our unsorted data
        with patch('src.analysis.backtest.pd.read_sql_query', return_value=unsorted_df):
            with pytest.raises(AssertionError, match="sorted by timestamp ASC"):
                DataLoader.load_historical_data()
