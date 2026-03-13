# tests/test_database.py

import pytest
from unittest.mock import patch, MagicMock
import sqlite3
import os

# --- Mocks ---

@patch('src.database.release_db_connection')
@patch('src.database.get_db_connection')
def test_initialize_database_creates_tables(mock_get_db_connection, mock_release):
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

    # Assert: Check if CREATE TABLE + ALTER TABLE statements were executed
    # 20 CREATE TABLEs (incl. session_peaks, watchlist_items, bot_state_kv, signal_decisions)
    # + 8 CREATE INDEXes (original + watchlist + signal_decisions)
    # + 6 ALTER TABLE (trade columns) + 1 ALTER TABLE (trailing_stop_peak)
    # + 1 ALTER TABLE (scraped_articles category) + 1 ALTER TABLE (scraped_articles gemini_score)
    # + 1 ALTER TABLE (trades trading_strategy) + 1 ALTER TABLE (trades exit_reason)
    # + 1 ALTER TABLE (trades strategy_type) + 1 ALTER TABLE (trades trade_reason)
    # + 1 ALTER TABLE (cb_events asset_type)
    # + 1 UPDATE (resolve stale cb_events) + 6 performance indexes = 49
    assert mock_cursor.execute.call_count == 49

    # Check the SQL statements (case-insensitive and ignoring whitespace)
    executed_queries = [' '.join(call[0][0].split()) for call in mock_cursor.execute.call_args_list]
    assert any("CREATE TABLE IF NOT EXISTS market_prices" in query for query in executed_queries)
    assert any("CREATE TABLE IF NOT EXISTS signals" in query for query in executed_queries)
    assert any("CREATE TABLE IF NOT EXISTS trades" in query for query in executed_queries)
    assert any("CREATE TABLE IF NOT EXISTS optimization_results" in query for query in executed_queries)
    assert any("CREATE TABLE IF NOT EXISTS news_sentiment" in query for query in executed_queries)
    assert any("CREATE TABLE IF NOT EXISTS scraped_articles" in query for query in executed_queries)
    assert any("idx_scraped_articles_title_hash" in query for query in executed_queries)
    assert any("CREATE TABLE IF NOT EXISTS position_additions" in query for query in executed_queries)
    assert any("idx_pos_additions_order" in query for query in executed_queries)
    assert any("CREATE TABLE IF NOT EXISTS macro_regime_history" in query for query in executed_queries)
    assert any("idx_macro_regime_recorded_at" in query for query in executed_queries)
    assert any("CREATE TABLE IF NOT EXISTS source_registry" in query for query in executed_queries)
    assert any("CREATE TABLE IF NOT EXISTS signal_attribution" in query for query in executed_queries)
    assert any("CREATE TABLE IF NOT EXISTS experiment_log" in query for query in executed_queries)
    assert any("CREATE TABLE IF NOT EXISTS tuning_history" in query for query in executed_queries)
    assert any("CREATE TABLE IF NOT EXISTS signal_decisions" in query for query in executed_queries)
    assert any("idx_signal_attribution_symbol" in query for query in executed_queries)
    assert any("idx_signal_attribution_order" in query for query in executed_queries)
    assert any("idx_signal_decisions_symbol" in query for query in executed_queries)

    mock_conn.commit.assert_called_once()
    mock_release.assert_called_once_with(mock_conn)


@patch('src.database.release_db_connection')
@patch('src.database.get_db_connection')
def test_get_historical_prices(mock_get_db_connection, mock_release):
    """
    Tests the get_historical_prices function to ensure it retrieves and processes data correctly.
    """
    # Arrange
    mock_conn = MagicMock()
    mock_cursor_context = MagicMock()
    mock_get_db_connection.return_value = mock_conn
    mock_conn.cursor.return_value = mock_cursor_context
    mock_cursor_context.__enter__.return_value = mock_cursor_context # For 'with' statement
    mock_cursor_context.fetchall.return_value = [(50500,), (50400,), (50300,), (50200,), (50100,)]

    # Act
    from src.database import get_historical_prices
    prices = get_historical_prices.sync('BTCUSDT', limit=5)

    # Assert
    # 1. Check if the correct query was executed
    mock_cursor_context.execute.assert_called_once()
    # 2. Check if the returned data is correct (should be reversed to oldest-to-newest)
    assert prices == [50100, 50200, 50300, 50400, 50500]
    mock_release.assert_called_once_with(mock_conn)


# --- Article Archive Tests ---

class TestArticleArchive:
    """Tests for the scraped_articles archive functions."""

    @patch('src.database.release_db_connection')
    @patch('src.database.get_db_connection')
    def test_save_articles_batch_inserts(self, mock_get_db_connection, mock_release):
        """Verify INSERT calls with correct params."""
        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_get_db_connection.return_value = mock_conn
        mock_conn.cursor.return_value = mock_cursor
        mock_cursor.__enter__ = MagicMock(return_value=mock_cursor)
        mock_cursor.__exit__ = MagicMock(return_value=False)

        from src.database import save_articles_batch

        articles = [
            {
                'title': 'Bitcoin hits new high',
                'title_hash': 'abc123',
                'source': 'CoinDesk',
                'source_url': 'https://coindesk.com/1',
                'description': 'BTC surged today.',
                'symbol': 'BTC',
                'category': 'crypto',
            },
            {
                'title': 'Ethereum upgrade',
                'title_hash': 'def456',
                'source': 'CoinTelegraph',
                'source_url': 'https://cointelegraph.com/2',
                'description': 'ETH protocol change.',
                'symbol': 'ETH',
                'category': 'crypto',
            },
        ]

        save_articles_batch(articles)

        assert mock_cursor.execute.call_count == 2
        mock_conn.commit.assert_called_once()
        mock_release.assert_called_once_with(mock_conn)

    @patch('src.database.release_db_connection')
    @patch('src.database.get_db_connection')
    def test_save_articles_batch_empty(self, mock_get_db_connection, mock_release):
        """Empty list = no DB interaction."""
        from src.database import save_articles_batch

        save_articles_batch([])

        mock_get_db_connection.assert_not_called()
        mock_release.assert_not_called()

    @patch('src.database.release_db_connection')
    @patch('src.database.get_db_connection')
    def test_get_recent_articles(self, mock_get_db_connection, mock_release):
        """Mock cursor, verify query and return."""
        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_get_db_connection.return_value = mock_conn
        mock_conn.cursor.return_value = mock_cursor
        mock_cursor.__enter__ = MagicMock(return_value=mock_cursor)
        mock_cursor.__exit__ = MagicMock(return_value=False)

        mock_row1 = MagicMock()
        mock_row1.keys.return_value = ['title', 'source', 'vader_score', 'collected_at']
        mock_row1.__iter__ = MagicMock(return_value=iter(['BTC breaks record', 'CoinDesk', 0.8, '2026-02-14']))
        mock_row1.__getitem__ = lambda self, key: {'title': 'BTC breaks record', 'source': 'CoinDesk',
                                                    'vader_score': 0.8, 'collected_at': '2026-02-14'}[key]

        mock_cursor.fetchall.return_value = [
            {'title': 'BTC breaks record', 'source': 'CoinDesk', 'vader_score': 0.8, 'collected_at': '2026-02-14'},
        ]

        from src.database import get_recent_articles

        result = get_recent_articles.sync('BTC', hours=24)

        mock_cursor.execute.assert_called_once()
        assert len(result) == 1
        assert result[0]['title'] == 'BTC breaks record'
        mock_release.assert_called_once_with(mock_conn)

    def test_title_hash_consistency(self):
        """Same title -> same hash regardless of case/whitespace."""
        from src.database import compute_title_hash

        hash1 = compute_title_hash("Bitcoin Hits New High")
        hash2 = compute_title_hash("  bitcoin hits new high  ")
        hash3 = compute_title_hash("BITCOIN HITS NEW HIGH")

        assert hash1 == hash2
        assert hash2 == hash3

        # Different title = different hash
        hash4 = compute_title_hash("Ethereum upgrade")
        assert hash1 != hash4


# --- Trailing Stop Persistence Tests ---

class TestTrailingStopPersistence:
    """Tests for trailing stop peak DB persistence functions."""

    @patch('src.database.release_db_connection')
    @patch('src.database.get_db_connection')
    def test_save_and_load_trailing_stop_peak(self, mock_get_db_connection, mock_release):
        """Save a peak, load it back, verify the value."""
        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_get_db_connection.return_value = mock_conn
        mock_conn.cursor.return_value = mock_cursor
        mock_cursor.__enter__ = MagicMock(return_value=mock_cursor)
        mock_cursor.__exit__ = MagicMock(return_value=False)

        from src.database import save_trailing_stop_peak

        save_trailing_stop_peak.sync('order_123', 50500.0)

        mock_cursor.execute.assert_called_once()
        call_args = mock_cursor.execute.call_args
        assert 'trailing_stop_peak' in call_args[0][0]
        assert call_args[0][1] == (50500.0, 'order_123')
        mock_conn.commit.assert_called_once()

    @patch('src.database.release_db_connection')
    @patch('src.database.get_db_connection')
    def test_load_peaks_only_open_positions(self, mock_get_db_connection, mock_release):
        """Only OPEN positions with non-null peaks are returned."""
        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_get_db_connection.return_value = mock_conn
        mock_conn.cursor.return_value = mock_cursor
        mock_cursor.__enter__ = MagicMock(return_value=mock_cursor)
        mock_cursor.__exit__ = MagicMock(return_value=False)

        mock_cursor.fetchall.return_value = [
            ('order_1', 50500.0),
            ('order_2', 3200.0),
        ]

        from src.database import load_trailing_stop_peaks

        result = load_trailing_stop_peaks.sync()

        assert result == {'order_1': 50500.0, 'order_2': 3200.0}
        # Verify the query filters for OPEN and NOT NULL
        query = mock_cursor.execute.call_args[0][0]
        assert "status = 'OPEN'" in query
        assert 'trailing_stop_peak IS NOT NULL' in query

    @patch('src.database.release_db_connection')
    @patch('src.database.get_db_connection')
    def test_load_peaks_empty(self, mock_get_db_connection, mock_release):
        """No open positions with peaks returns empty dict."""
        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_get_db_connection.return_value = mock_conn
        mock_conn.cursor.return_value = mock_cursor
        mock_cursor.__enter__ = MagicMock(return_value=mock_cursor)
        mock_cursor.__exit__ = MagicMock(return_value=False)

        mock_cursor.fetchall.return_value = []

        from src.database import load_trailing_stop_peaks

        result = load_trailing_stop_peaks.sync()
        assert result == {}

    @patch('src.database.release_db_connection')
    @patch('src.database.get_db_connection')
    def test_save_peak_updates_existing(self, mock_get_db_connection, mock_release):
        """Second save overwrites first (UPDATE semantics)."""
        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_get_db_connection.return_value = mock_conn
        mock_conn.cursor.return_value = mock_cursor
        mock_cursor.__enter__ = MagicMock(return_value=mock_cursor)
        mock_cursor.__exit__ = MagicMock(return_value=False)

        from src.database import save_trailing_stop_peak

        save_trailing_stop_peak.sync('order_1', 50000.0)
        save_trailing_stop_peak.sync('order_1', 51000.0)

        assert mock_cursor.execute.call_count == 2
        last_call = mock_cursor.execute.call_args
        assert last_call[0][1] == (51000.0, 'order_1')


# --- Gemini Score DB Tests ---

class TestGeminiScoreDB:
    """Tests for Gemini per-article score DB functions."""

    @patch('src.database.release_db_connection')
    @patch('src.database.get_db_connection')
    def test_gemini_score_column_in_migration(self, mock_get_db_connection, mock_release):
        """Migration includes ALTER TABLE for gemini_score column."""
        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_get_db_connection.return_value = mock_conn
        mock_conn.cursor.return_value = mock_cursor

        from src.database import initialize_database
        initialize_database()

        executed_queries = [' '.join(call[0][0].split()) for call in mock_cursor.execute.call_args_list]
        assert any("gemini_score" in query for query in executed_queries)

    @patch('src.database.release_db_connection')
    @patch('src.database.get_db_connection')
    def test_get_gemini_scores_for_hashes(self, mock_get_db_connection, mock_release):
        """Returns {title_hash: score} for cached scores."""
        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_get_db_connection.return_value = mock_conn
        mock_conn.cursor.return_value = mock_cursor
        mock_cursor.__enter__ = MagicMock(return_value=mock_cursor)
        mock_cursor.__exit__ = MagicMock(return_value=False)
        mock_cursor.fetchall.return_value = [
            ('hash_a', 0.7),
            ('hash_b', -0.3),
        ]

        from src.database import get_gemini_scores_for_hashes

        result = get_gemini_scores_for_hashes(['hash_a', 'hash_b', 'hash_c'])

        assert result == {'hash_a': 0.7, 'hash_b': -0.3}
        mock_cursor.execute.assert_called_once()
        query = mock_cursor.execute.call_args[0][0]
        assert 'gemini_score IS NOT NULL' in query

    @patch('src.database.release_db_connection')
    @patch('src.database.get_db_connection')
    def test_get_gemini_scores_empty_input(self, mock_get_db_connection, mock_release):
        """Empty hash list returns empty dict without DB call."""
        from src.database import get_gemini_scores_for_hashes

        result = get_gemini_scores_for_hashes([])
        assert result == {}
        mock_get_db_connection.assert_not_called()

    @patch('src.database.release_db_connection')
    @patch('src.database.get_db_connection')
    def test_update_gemini_scores_batch(self, mock_get_db_connection, mock_release):
        """Updates gemini_score for each title_hash."""
        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_get_db_connection.return_value = mock_conn
        mock_conn.cursor.return_value = mock_cursor
        mock_cursor.__enter__ = MagicMock(return_value=mock_cursor)
        mock_cursor.__exit__ = MagicMock(return_value=False)

        from src.database import update_gemini_scores_batch

        update_gemini_scores_batch({'hash_a': 0.5, 'hash_b': -0.2})

        assert mock_cursor.execute.call_count == 2
        mock_conn.commit.assert_called_once()

        # Verify UPDATE query
        query = mock_cursor.execute.call_args_list[0][0][0]
        assert 'UPDATE scraped_articles SET gemini_score' in query

    @patch('src.database.release_db_connection')
    @patch('src.database.get_db_connection')
    def test_update_gemini_scores_empty(self, mock_get_db_connection, mock_release):
        """Empty scores dict skips DB call."""
        from src.database import update_gemini_scores_batch

        update_gemini_scores_batch({})
        mock_get_db_connection.assert_not_called()

    @patch('src.database.release_db_connection')
    @patch('src.database.get_db_connection')
    def test_save_articles_batch_includes_gemini_score(self, mock_get_db_connection, mock_release):
        """save_articles_batch includes gemini_score in INSERT."""
        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_get_db_connection.return_value = mock_conn
        mock_conn.cursor.return_value = mock_cursor
        mock_cursor.__enter__ = MagicMock(return_value=mock_cursor)
        mock_cursor.__exit__ = MagicMock(return_value=False)

        from src.database import save_articles_batch

        articles = [{
            'title': 'Test',
            'title_hash': 'abc',
            'source': 'Test',
            'source_url': '',
            'description': '',
            'symbol': 'BTC',
            'category': 'crypto',
            'gemini_score': 0.7,
        }]
        save_articles_batch(articles)

        query = mock_cursor.execute.call_args[0][0]
        assert 'gemini_score' in query
        params = mock_cursor.execute.call_args[0][1]
        assert 0.7 in params
