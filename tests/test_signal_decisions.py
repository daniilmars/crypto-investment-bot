"""Tests for signal decision tracking (Pillar 3A)."""

import sqlite3
from unittest.mock import patch

from src.database import record_signal_decision, get_signal_decisions


def _setup_test_db():
    """Create an in-memory SQLite DB with signal_decisions table."""
    conn = sqlite3.connect(':memory:')
    conn.execute("""
        CREATE TABLE signal_decisions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol TEXT NOT NULL,
            signal_type TEXT NOT NULL,
            asset_type TEXT DEFAULT 'crypto',
            decision TEXT NOT NULL,
            signal_strength REAL,
            gemini_confidence REAL,
            catalyst_freshness TEXT,
            reason TEXT,
            price REAL,
            decided_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.commit()
    return conn


@patch('src.database.release_db_connection')
@patch('src.database.get_db_connection')
def test_record_approved_decision(mock_get_conn, mock_release):
    """Approved signal should be recorded in signal_decisions."""
    conn = _setup_test_db()
    mock_get_conn.return_value = conn

    signal = {
        'symbol': 'BTC',
        'signal': 'BUY',
        'asset_type': 'crypto',
        'signal_strength': 0.75,
        'gemini_confidence': 0.8,
        'catalyst_freshness': 'breaking',
        'reason': 'Strong bullish momentum',
        'current_price': 50000.0,
    }
    record_signal_decision.sync(signal, 'approved')

    cursor = conn.execute("SELECT * FROM signal_decisions")
    rows = cursor.fetchall()
    assert len(rows) == 1
    # Check values (id, symbol, signal_type, asset_type, decision, ...)
    assert rows[0][1] == 'BTC'
    assert rows[0][2] == 'BUY'
    assert rows[0][4] == 'approved'
    assert rows[0][5] == 0.75  # signal_strength
    conn.close()


@patch('src.database.release_db_connection')
@patch('src.database.get_db_connection')
def test_record_rejected_decision(mock_get_conn, mock_release):
    """Rejected signal should be recorded in signal_decisions."""
    conn = _setup_test_db()
    mock_get_conn.return_value = conn

    signal = {
        'symbol': 'ETH',
        'signal': 'SELL',
        'asset_type': 'crypto',
        'signal_strength': 0.45,
        'reason': 'Weak sell signal',
        'current_price': 3000.0,
    }
    record_signal_decision.sync(signal, 'rejected')

    cursor = conn.execute("SELECT * FROM signal_decisions WHERE decision = 'rejected'")
    rows = cursor.fetchall()
    assert len(rows) == 1
    assert rows[0][1] == 'ETH'
    assert rows[0][2] == 'SELL'
    assert rows[0][4] == 'rejected'
    conn.close()


@patch('src.database.release_db_connection')
@patch('src.database.get_db_connection')
def test_query_decisions_by_type(mock_get_conn, mock_release):
    """get_signal_decisions with decision filter should return only matching rows."""
    conn = _setup_test_db()
    mock_get_conn.return_value = conn

    # Insert mixed decisions
    for decision in ('approved', 'rejected', 'approved', 'expired'):
        conn.execute(
            "INSERT INTO signal_decisions (symbol, signal_type, decision, price) "
            "VALUES (?, ?, ?, ?)",
            ('BTC', 'BUY', decision, 50000.0))
    conn.commit()

    results = get_signal_decisions.sync(limit=100, decision='approved')
    assert len(results) == 2
    assert all(r['decision'] == 'approved' for r in results)
    conn.close()


@patch('src.database.release_db_connection')
@patch('src.database.get_db_connection')
def test_expired_signal_recorded(mock_get_conn, mock_release):
    """Expired signal should be recorded with decision='expired'."""
    conn = _setup_test_db()
    mock_get_conn.return_value = conn

    signal = {
        'symbol': 'SOL',
        'signal': 'BUY',
        'asset_type': 'crypto',
        'signal_strength': 0.6,
        'current_price': 100.0,
        'reason': 'Timed out',
    }
    record_signal_decision.sync(signal, 'expired')

    results = get_signal_decisions.sync(limit=100, decision='expired')
    assert len(results) == 1
    assert results[0]['symbol'] == 'SOL'
    assert results[0]['decision'] == 'expired'
    conn.close()
