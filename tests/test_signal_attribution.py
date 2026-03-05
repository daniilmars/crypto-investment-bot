"""Tests for src/analysis/signal_attribution.py"""

import pytest
from unittest.mock import patch, MagicMock
import sqlite3


@pytest.fixture
def sqlite_db():
    """Create an in-memory SQLite DB with the signal_attribution table."""
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
    conn.commit()
    return conn


@patch('src.analysis.signal_attribution.release_db_connection')
@patch('src.analysis.signal_attribution.get_db_connection')
def test_record_signal_attribution(mock_get_conn, mock_release, sqlite_db):
    mock_get_conn.return_value = sqlite_db
    from src.analysis.signal_attribution import record_signal_attribution

    signal = {'symbol': 'BTC', 'signal': 'BUY', 'current_price': 50000}
    articles = [
        {'title_hash': 'abc123', 'source': 'CoinDesk'},
        {'title_hash': 'def456', 'source': 'Reuters'},
    ]
    gemini = {'direction': 'bullish', 'confidence': 0.8}

    attr_id = record_signal_attribution(signal, articles=articles,
                                         gemini_assessment=gemini)
    assert attr_id is not None
    assert attr_id > 0

    # Verify record was created
    cur = sqlite_db.cursor()
    cur.execute("SELECT * FROM signal_attribution WHERE id = ?", (attr_id,))
    row = cur.fetchone()
    assert row is not None
    # Check fields: symbol=BTC, signal_type=BUY, article_hashes contains abc123
    assert row[2] == 'BTC'  # symbol
    assert row[3] == 'BUY'  # signal_type
    assert 'abc123' in row[6]  # article_hashes
    assert 'CoinDesk' in row[7]  # source_names
    assert row[8] == 'bullish'  # gemini_direction


@patch('src.analysis.signal_attribution.release_db_connection')
@patch('src.analysis.signal_attribution.get_db_connection')
def test_record_attribution_no_articles(mock_get_conn, mock_release, sqlite_db):
    mock_get_conn.return_value = sqlite_db
    from src.analysis.signal_attribution import record_signal_attribution

    signal = {'symbol': 'ETH', 'signal': 'SELL', 'current_price': 3000}
    attr_id = record_signal_attribution(signal)
    assert attr_id is not None


@patch('src.analysis.signal_attribution.release_db_connection')
@patch('src.analysis.signal_attribution.get_db_connection')
def test_link_attribution_to_order(mock_get_conn, mock_release, sqlite_db):
    mock_get_conn.return_value = sqlite_db
    from src.analysis.signal_attribution import (
        record_signal_attribution, link_attribution_to_order,
    )

    signal = {'symbol': 'BTC', 'signal': 'BUY'}
    attr_id = record_signal_attribution(signal)

    mock_get_conn.return_value = sqlite_db
    link_attribution_to_order(attr_id, 'ORD-123')

    cur = sqlite_db.cursor()
    cur.execute("SELECT trade_order_id FROM signal_attribution WHERE id = ?", (attr_id,))
    assert cur.fetchone()[0] == 'ORD-123'


@patch('src.analysis.signal_attribution.release_db_connection')
@patch('src.analysis.signal_attribution.get_db_connection')
def test_resolve_attribution(mock_get_conn, mock_release, sqlite_db):
    mock_get_conn.return_value = sqlite_db
    from src.analysis.signal_attribution import (
        record_signal_attribution, link_attribution_to_order, resolve_attribution,
    )

    signal = {'symbol': 'BTC', 'signal': 'BUY'}
    attr_id = record_signal_attribution(signal)

    mock_get_conn.return_value = sqlite_db
    link_attribution_to_order(attr_id, 'ORD-456')

    mock_get_conn.return_value = sqlite_db
    updated = resolve_attribution('ORD-456', pnl=150.0, pnl_pct=0.03,
                                  duration_hours=5.0, exit_reason='take_profit')
    assert updated == 1

    cur = sqlite_db.cursor()
    cur.execute("SELECT trade_pnl, exit_reason, resolved_at FROM signal_attribution WHERE id = ?", (attr_id,))
    row = cur.fetchone()
    assert row[0] == 150.0
    assert row[1] == 'take_profit'
    assert row[2] is not None  # resolved_at set


@patch('src.analysis.signal_attribution.release_db_connection')
@patch('src.analysis.signal_attribution.get_db_connection')
def test_resolve_nonexistent_order(mock_get_conn, mock_release, sqlite_db):
    mock_get_conn.return_value = sqlite_db
    from src.analysis.signal_attribution import resolve_attribution

    updated = resolve_attribution('NONEXISTENT', pnl=100.0)
    assert updated == 0


@patch('src.analysis.signal_attribution.release_db_connection')
@patch('src.analysis.signal_attribution.get_db_connection')
def test_get_signal_accuracy(mock_get_conn, mock_release, sqlite_db):
    mock_get_conn.return_value = sqlite_db
    from src.analysis.signal_attribution import (
        record_signal_attribution, link_attribution_to_order,
        resolve_attribution, get_signal_accuracy,
    )

    # Create two resolved attributions: one win, one loss
    for pnl in [100.0, -50.0]:
        mock_get_conn.return_value = sqlite_db
        aid = record_signal_attribution({'symbol': 'BTC', 'signal': 'BUY'})
        oid = f'ORD-{pnl}'
        mock_get_conn.return_value = sqlite_db
        link_attribution_to_order(aid, oid)
        mock_get_conn.return_value = sqlite_db
        resolve_attribution(oid, pnl=pnl, exit_reason='test')

    mock_get_conn.return_value = sqlite_db
    accuracy = get_signal_accuracy(days=30)
    assert accuracy['total'] == 2
    assert accuracy['wins'] == 1
    assert accuracy['losses'] == 1
    assert accuracy['win_rate'] == 0.5
    assert accuracy['avg_pnl'] == 25.0  # (100 - 50) / 2


@patch('src.analysis.signal_attribution.release_db_connection')
@patch('src.analysis.signal_attribution.get_db_connection')
def test_get_source_performance(mock_get_conn, mock_release, sqlite_db):
    mock_get_conn.return_value = sqlite_db
    from src.analysis.signal_attribution import (
        record_signal_attribution, link_attribution_to_order,
        resolve_attribution, get_source_performance,
    )

    articles = [{'title_hash': 'h1', 'source': 'CoinDesk'}]
    mock_get_conn.return_value = sqlite_db
    aid = record_signal_attribution(
        {'symbol': 'BTC', 'signal': 'BUY'}, articles=articles)
    mock_get_conn.return_value = sqlite_db
    link_attribution_to_order(aid, 'ORD-PERF')
    mock_get_conn.return_value = sqlite_db
    resolve_attribution('ORD-PERF', pnl=200.0, exit_reason='take_profit')

    mock_get_conn.return_value = sqlite_db
    perf = get_source_performance(source_name='CoinDesk', days=30)
    assert len(perf) == 1
    assert perf[0]['source_name'] == 'CoinDesk'
    assert perf[0]['total_pnl'] == 200.0
    assert perf[0]['wins'] == 1


@patch('src.analysis.signal_attribution.release_db_connection')
@patch('src.analysis.signal_attribution.get_db_connection')
def test_get_recent_attributions(mock_get_conn, mock_release, sqlite_db):
    mock_get_conn.return_value = sqlite_db
    from src.analysis.signal_attribution import record_signal_attribution, get_recent_attributions

    record_signal_attribution({'symbol': 'BTC', 'signal': 'BUY'})
    mock_get_conn.return_value = sqlite_db
    record_signal_attribution({'symbol': 'ETH', 'signal': 'SELL'})

    mock_get_conn.return_value = sqlite_db
    recent = get_recent_attributions(limit=10)
    assert len(recent) == 2

    mock_get_conn.return_value = sqlite_db
    btc_only = get_recent_attributions(symbol='BTC', limit=10)
    assert len(btc_only) == 1
