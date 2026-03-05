"""Tests for src/analysis/feedback_loop.py"""

import pytest
from unittest.mock import patch, MagicMock, call
import sqlite3


@pytest.fixture
def sqlite_db():
    """Create an in-memory SQLite DB with required tables."""
    conn = sqlite3.connect(':memory:')
    conn.execute("""
        CREATE TABLE signal_attribution (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            signal_id INTEGER,
            symbol TEXT NOT NULL,
            signal_type TEXT NOT NULL,
            signal_timestamp TIMESTAMP NOT NULL,
            signal_confidence REAL,
            article_hashes TEXT,
            source_names TEXT,
            gemini_direction TEXT,
            gemini_confidence REAL,
            catalyst_type TEXT,
            trade_order_id TEXT,
            trade_pnl REAL,
            trade_pnl_pct REAL,
            trade_duration_hours REAL,
            exit_reason TEXT,
            attribution_score REAL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            resolved_at TIMESTAMP
        )
    """)
    conn.execute("""
        CREATE TABLE experiment_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            experiment_type TEXT NOT NULL,
            description TEXT NOT NULL,
            old_value TEXT,
            new_value TEXT,
            reason TEXT,
            impact_metric TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.execute("""
        CREATE TABLE source_registry (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_type TEXT NOT NULL,
            source_name TEXT NOT NULL UNIQUE,
            source_url TEXT NOT NULL,
            category TEXT,
            tier INTEGER DEFAULT 2,
            is_active INTEGER DEFAULT 1,
            reliability_score REAL DEFAULT 0.5,
            articles_total INTEGER DEFAULT 0,
            articles_with_signals INTEGER DEFAULT 0,
            profitable_signal_ratio REAL,
            avg_signal_pnl REAL,
            last_fetched_at TIMESTAMP,
            last_article_at TIMESTAMP,
            error_count INTEGER DEFAULT 0,
            consecutive_errors INTEGER DEFAULT 0,
            added_by TEXT DEFAULT 'manual',
            added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            deactivated_at TIMESTAMP,
            deactivation_reason TEXT,
            metadata_json TEXT
        )
    """)
    conn.commit()
    return conn


def _insert_attribution(conn, order_id, source_names):
    """Insert a test attribution record."""
    conn.execute(
        "INSERT INTO signal_attribution "
        "(symbol, signal_type, signal_timestamp, trade_order_id, source_names) "
        "VALUES (?, ?, datetime('now'), ?, ?)",
        ('BTC', 'BUY', order_id, source_names))
    conn.commit()


def _insert_source(conn, name, url='https://test.com'):
    """Insert a test source."""
    conn.execute(
        "INSERT INTO source_registry (source_type, source_name, source_url) "
        "VALUES ('rss', ?, ?)", (name, url))
    conn.commit()
    cur = conn.cursor()
    cur.execute("SELECT id FROM source_registry WHERE source_name = ?", (name,))
    return cur.fetchone()[0]


@patch('src.analysis.feedback_loop.release_db_connection')
@patch('src.analysis.feedback_loop.get_db_connection')
@patch('src.analysis.signal_attribution.release_db_connection')
@patch('src.analysis.signal_attribution.get_db_connection')
@patch('src.collectors.source_registry.release_db_connection')
@patch('src.collectors.source_registry.get_db_connection')
def test_process_closed_trade(mock_reg_conn, mock_reg_rel,
                               mock_attr_conn, mock_attr_rel,
                               mock_fb_conn, mock_fb_rel,
                               sqlite_db):
    """Test that process_closed_trade resolves attribution and updates sources."""
    mock_reg_conn.return_value = sqlite_db
    mock_attr_conn.return_value = sqlite_db
    mock_fb_conn.return_value = sqlite_db

    from src.analysis.feedback_loop import process_closed_trade

    # Setup: create a source and an attribution
    src_id = _insert_source(sqlite_db, 'TestSource')
    _insert_attribution(sqlite_db, 'ORD-001', 'TestSource')

    process_closed_trade('ORD-001', pnl=50.0, pnl_pct=0.05,
                         exit_reason='take_profit')

    # Verify attribution was resolved
    cur = sqlite_db.cursor()
    cur.execute("SELECT trade_pnl, exit_reason, resolved_at FROM signal_attribution WHERE trade_order_id = 'ORD-001'")
    row = cur.fetchone()
    assert row[0] == 50.0
    assert row[1] == 'take_profit'
    assert row[2] is not None

    # Verify source stats updated
    cur.execute("SELECT articles_with_signals, profitable_signal_ratio, avg_signal_pnl FROM source_registry WHERE id = ?", (src_id,))
    row = cur.fetchone()
    assert row[0] == 1
    assert row[1] == 1.0  # 100% profitable
    assert row[2] == 50.0


@patch('src.analysis.feedback_loop.release_db_connection')
@patch('src.analysis.feedback_loop.get_db_connection')
def test_process_closed_trade_no_attribution(mock_fb_conn, mock_fb_rel, sqlite_db):
    """process_closed_trade with no matching attribution should not error."""
    mock_fb_conn.return_value = sqlite_db
    from src.analysis.feedback_loop import process_closed_trade
    # Should not raise
    process_closed_trade('NONEXISTENT', pnl=100.0)


def test_calculate_reliability_score():
    from src.analysis.feedback_loop import _calculate_reliability_score

    # New source with defaults
    score = _calculate_reliability_score({
        'articles_total': 0, 'error_count': 0, 'consecutive_errors': 0,
        'articles_with_signals': 0, 'profitable_signal_ratio': 0,
    })
    assert 0.0 <= score <= 1.0

    # High-quality source
    score = _calculate_reliability_score({
        'articles_total': 100, 'error_count': 5, 'consecutive_errors': 0,
        'articles_with_signals': 20, 'profitable_signal_ratio': 0.7,
    })
    assert score > 0.5

    # Bad source with many errors
    score = _calculate_reliability_score({
        'articles_total': 10, 'error_count': 90, 'consecutive_errors': 20,
        'articles_with_signals': 1, 'profitable_signal_ratio': 0.1,
    })
    assert score < 0.4


@patch('src.analysis.feedback_loop.release_db_connection')
@patch('src.analysis.feedback_loop.get_db_connection')
def test_log_experiment(mock_conn, mock_release, sqlite_db):
    mock_conn.return_value = sqlite_db
    from src.analysis.feedback_loop import _log_experiment, get_recent_experiments

    _log_experiment('test_type', 'test description', old_value='old', new_value='new')

    mock_conn.return_value = sqlite_db
    entries = get_recent_experiments(limit=5)
    assert len(entries) == 1
    assert entries[0]['experiment_type'] == 'test_type'
    assert entries[0]['description'] == 'test description'


@patch('src.analysis.feedback_loop.app_config', {
    'settings': {'autonomous_bot': {
        'feedback_loop': {'enabled': False},
    }}
})
def test_daily_review_disabled():
    from src.analysis.feedback_loop import run_daily_source_review
    result = run_daily_source_review()
    assert result.get('skipped') is True
    assert 'disabled' in result.get('reason', '')
