"""Tests for src/collectors/source_registry.py"""

import pytest
from unittest.mock import patch, MagicMock
import sqlite3
import os


@pytest.fixture
def sqlite_db():
    """Create an in-memory SQLite DB with the source_registry table."""
    conn = sqlite3.connect(':memory:')
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


@patch('src.collectors.source_registry.release_db_connection')
@patch('src.collectors.source_registry.get_db_connection')
def test_add_source(mock_get_conn, mock_release, sqlite_db):
    mock_get_conn.return_value = sqlite_db
    from src.collectors.source_registry import add_source, get_source_by_name

    result = add_source('rss', 'Test Feed', 'https://test.com/feed',
                        category='crypto', tier=2)
    assert result is not None

    # Verify it was added
    mock_get_conn.return_value = sqlite_db
    source = get_source_by_name('Test Feed')
    assert source is not None
    assert source['source_type'] == 'rss'
    assert source['source_url'] == 'https://test.com/feed'
    assert source['category'] == 'crypto'
    assert source['tier'] == 2


@patch('src.collectors.source_registry.release_db_connection')
@patch('src.collectors.source_registry.get_db_connection')
def test_add_duplicate_source(mock_get_conn, mock_release, sqlite_db):
    mock_get_conn.return_value = sqlite_db
    from src.collectors.source_registry import add_source

    add_source('rss', 'Dup Feed', 'https://test.com/feed')
    result = add_source('rss', 'Dup Feed', 'https://other.com/feed')
    assert result is None  # Duplicate should return None


@patch('src.collectors.source_registry.release_db_connection')
@patch('src.collectors.source_registry.get_db_connection')
def test_load_active_sources(mock_get_conn, mock_release, sqlite_db):
    mock_get_conn.return_value = sqlite_db
    from src.collectors.source_registry import add_source, load_active_sources

    add_source('rss', 'Feed1', 'https://f1.com/feed', category='crypto', tier=1)
    mock_get_conn.return_value = sqlite_db
    add_source('web_scraper', 'Scraper1', 'scraper://s1', category='mixed', tier=2)
    mock_get_conn.return_value = sqlite_db
    add_source('rss', 'Feed2', 'https://f2.com/feed', category='crypto', tier=3)

    # All active
    mock_get_conn.return_value = sqlite_db
    sources = load_active_sources()
    assert len(sources) == 3

    # Filter by type
    mock_get_conn.return_value = sqlite_db
    rss_sources = load_active_sources(source_type='rss')
    assert len(rss_sources) == 2

    # Filter by tier
    mock_get_conn.return_value = sqlite_db
    tier2_max = load_active_sources(tier_max=2)
    assert len(tier2_max) == 2


@patch('src.collectors.source_registry.release_db_connection')
@patch('src.collectors.source_registry.get_db_connection')
def test_update_source_stats(mock_get_conn, mock_release, sqlite_db):
    mock_get_conn.return_value = sqlite_db
    from src.collectors.source_registry import add_source, update_source_stats, get_source_by_id

    sid = add_source('rss', 'Stats Feed', 'https://test.com/feed')

    mock_get_conn.return_value = sqlite_db
    update_source_stats(sid, articles_fetched=10)

    mock_get_conn.return_value = sqlite_db
    src = get_source_by_id(sid)
    assert src['articles_total'] == 10
    assert src['consecutive_errors'] == 0


@patch('src.collectors.source_registry.release_db_connection')
@patch('src.collectors.source_registry.get_db_connection')
def test_update_source_stats_errors(mock_get_conn, mock_release, sqlite_db):
    mock_get_conn.return_value = sqlite_db
    from src.collectors.source_registry import add_source, update_source_stats, get_source_by_id

    sid = add_source('rss', 'Err Feed', 'https://test.com/feed')

    mock_get_conn.return_value = sqlite_db
    update_source_stats(sid, errors=3)

    mock_get_conn.return_value = sqlite_db
    src = get_source_by_id(sid)
    assert src['error_count'] == 3
    assert src['consecutive_errors'] == 3


@patch('src.collectors.source_registry.release_db_connection')
@patch('src.collectors.source_registry.get_db_connection')
def test_deactivate_source(mock_get_conn, mock_release, sqlite_db):
    mock_get_conn.return_value = sqlite_db
    from src.collectors.source_registry import add_source, deactivate_source, load_active_sources

    sid = add_source('rss', 'Deact Feed', 'https://test.com/feed')

    mock_get_conn.return_value = sqlite_db
    deactivate_source(sid, 'test_reason')

    mock_get_conn.return_value = sqlite_db
    sources = load_active_sources()
    assert len(sources) == 0


@patch('src.collectors.source_registry.release_db_connection')
@patch('src.collectors.source_registry.get_db_connection')
def test_activate_source(mock_get_conn, mock_release, sqlite_db):
    mock_get_conn.return_value = sqlite_db
    from src.collectors.source_registry import (
        add_source, deactivate_source, activate_source, load_active_sources,
    )

    sid = add_source('rss', 'Toggle Feed', 'https://test.com/feed')
    mock_get_conn.return_value = sqlite_db
    deactivate_source(sid, 'test')
    mock_get_conn.return_value = sqlite_db
    activate_source(sid)
    mock_get_conn.return_value = sqlite_db
    sources = load_active_sources()
    assert len(sources) == 1


@patch('src.collectors.source_registry.release_db_connection')
@patch('src.collectors.source_registry.get_db_connection')
def test_promote_source(mock_get_conn, mock_release, sqlite_db):
    mock_get_conn.return_value = sqlite_db
    from src.collectors.source_registry import add_source, promote_source, get_source_by_id

    sid = add_source('rss', 'Promo Feed', 'https://test.com/feed', tier=3)
    mock_get_conn.return_value = sqlite_db
    promote_source(sid, 2)
    mock_get_conn.return_value = sqlite_db
    src = get_source_by_id(sid)
    assert src['tier'] == 2


@patch('src.collectors.source_registry.release_db_connection')
@patch('src.collectors.source_registry.get_db_connection')
def test_update_signal_stats(mock_get_conn, mock_release, sqlite_db):
    mock_get_conn.return_value = sqlite_db
    from src.collectors.source_registry import add_source, update_signal_stats, get_source_by_id

    sid = add_source('rss', 'Sig Feed', 'https://test.com/feed')

    # First profitable trade
    mock_get_conn.return_value = sqlite_db
    update_signal_stats(sid, profitable=True, pnl=50.0)
    mock_get_conn.return_value = sqlite_db
    src = get_source_by_id(sid)
    assert src['articles_with_signals'] == 1
    assert src['profitable_signal_ratio'] == 1.0
    assert src['avg_signal_pnl'] == 50.0

    # Second losing trade
    mock_get_conn.return_value = sqlite_db
    update_signal_stats(sid, profitable=False, pnl=-20.0)
    mock_get_conn.return_value = sqlite_db
    src = get_source_by_id(sid)
    assert src['articles_with_signals'] == 2
    assert src['profitable_signal_ratio'] == 0.5
    assert src['avg_signal_pnl'] == 15.0  # (50 + -20) / 2


def test_derive_feed_name():
    from src.collectors.source_registry import _derive_feed_name
    assert _derive_feed_name('https://feeds.reuters.com/reuters/businessNews', 'financial') == 'Reuters Business'
    assert _derive_feed_name('https://news.google.com/rss/search?q=crypto', 'crypto') == 'Google News (crypto)'
    assert 'coindesk' in _derive_feed_name('https://www.coindesk.com/arc/outboundfeeds/rss/', 'crypto').lower()


def test_derive_tier():
    from src.collectors.source_registry import _derive_tier
    assert _derive_tier('regulatory') == 1
    assert _derive_tier('crypto') == 1
    assert _derive_tier('financial') == 2
    assert _derive_tier('ai') == 3
    assert _derive_tier('google_news') == 3


@patch('src.collectors.source_registry.release_db_connection')
@patch('src.collectors.source_registry.get_db_connection')
def test_seed_registry(mock_get_conn, mock_release, sqlite_db):
    mock_get_conn.return_value = sqlite_db
    from src.collectors.source_registry import seed_registry, get_source_count

    inserted = seed_registry()
    assert inserted > 0

    mock_get_conn.return_value = sqlite_db
    count = get_source_count()
    assert count > 50  # Should have RSS + scrapers


@patch('src.collectors.source_registry.release_db_connection')
@patch('src.collectors.source_registry.get_db_connection')
def test_seed_registry_idempotent(mock_get_conn, mock_release, sqlite_db):
    mock_get_conn.return_value = sqlite_db
    from src.collectors.source_registry import seed_registry

    first = seed_registry()
    mock_get_conn.return_value = sqlite_db
    second = seed_registry()
    assert second == 0  # No new insertions on second run


@patch('src.collectors.source_registry.release_db_connection')
@patch('src.collectors.source_registry.get_db_connection')
def test_load_rss_feeds_from_registry(mock_get_conn, mock_release, sqlite_db):
    mock_get_conn.return_value = sqlite_db
    from src.collectors.source_registry import (
        add_source, load_rss_feeds_from_registry,
    )

    add_source('rss', 'Test RSS', 'https://test.com/feed', category='crypto')
    mock_get_conn.return_value = sqlite_db

    feeds = load_rss_feeds_from_registry()
    assert feeds is not None
    assert len(feeds) == 1
    assert feeds[0]['url'] == 'https://test.com/feed'
    assert feeds[0]['source_name'] == 'Test RSS'
    assert 'source_id' in feeds[0]


@patch('src.collectors.source_registry.release_db_connection')
@patch('src.collectors.source_registry.get_db_connection')
def test_load_rss_feeds_empty_returns_none(mock_get_conn, mock_release, sqlite_db):
    mock_get_conn.return_value = sqlite_db
    from src.collectors.source_registry import load_rss_feeds_from_registry

    feeds = load_rss_feeds_from_registry()
    assert feeds is None  # Signals caller to use fallback
