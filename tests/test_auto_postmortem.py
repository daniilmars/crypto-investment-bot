"""Tests for auto post-mortem analysis (Pillar 4)."""

import sqlite3
from unittest.mock import patch
from datetime import datetime, timedelta, timezone

from src.analysis.auto_postmortem import generate_auto_postmortem, _generate_recommendations


def _setup_test_db(trades=None):
    """Create an in-memory SQLite DB with trades and signal_attribution tables."""
    conn = sqlite3.connect(':memory:')
    conn.execute("""
        CREATE TABLE trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol TEXT, order_id TEXT, side TEXT,
            entry_price REAL, exit_price REAL, quantity REAL,
            status TEXT, pnl REAL,
            entry_timestamp TIMESTAMP, exit_timestamp TIMESTAMP,
            trading_strategy TEXT DEFAULT 'manual',
            exit_reason TEXT, asset_type TEXT DEFAULT 'crypto'
        )
    """)
    conn.execute("""
        CREATE TABLE signal_attribution (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            trade_order_id TEXT,
            symbol TEXT, signal_type TEXT,
            signal_timestamp TIMESTAMP,
            signal_confidence REAL,
            catalyst_type TEXT,
            source_names TEXT
        )
    """)
    if trades:
        for t in trades:
            conn.execute(
                "INSERT INTO trades (symbol, order_id, side, entry_price, exit_price, "
                "quantity, status, pnl, entry_timestamp, exit_timestamp, "
                "trading_strategy, exit_reason) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (t.get('symbol'), t.get('order_id'), 'BUY',
                 t.get('entry_price', 100), t.get('exit_price', 100),
                 t.get('quantity', 1), t.get('status', 'CLOSED'),
                 t.get('pnl', 0), t.get('entry_timestamp'),
                 t.get('exit_timestamp'), t.get('trading_strategy', 'auto'),
                 t.get('exit_reason')))
            if t.get('signal_confidence') is not None:
                conn.execute(
                    "INSERT INTO signal_attribution "
                    "(trade_order_id, symbol, signal_type, signal_timestamp, "
                    "signal_confidence, catalyst_type) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (t.get('order_id'), t.get('symbol'), 'BUY',
                     t.get('entry_timestamp'),
                     t.get('signal_confidence'),
                     t.get('catalyst_type')))
    conn.commit()
    return conn


def _recent_ts(hours_ago=1):
    return (datetime.now(timezone.utc) - timedelta(hours=hours_ago)).isoformat()


@patch('src.analysis.auto_postmortem.release_db_connection')
@patch('src.analysis.auto_postmortem.get_db_connection')
def test_postmortem_empty_trades(mock_get_conn, mock_release):
    """Empty result set should return zeroed summary."""
    conn = _setup_test_db()
    mock_get_conn.return_value = conn

    report = generate_auto_postmortem(days=30)
    assert report['summary']['total'] == 0
    assert report['summary']['total_pnl'] == 0
    assert report['recommendations'] == []
    conn.close()


@patch('src.analysis.auto_postmortem.release_db_connection')
@patch('src.analysis.auto_postmortem.get_db_connection')
def test_postmortem_summary_calculations(mock_get_conn, mock_release):
    """Summary should correctly compute wins, losses, PnL, and win rate."""
    ts = _recent_ts()
    trades = [
        {'symbol': 'BTC', 'order_id': 'o1', 'pnl': 50.0, 'exit_timestamp': ts,
         'entry_timestamp': ts, 'exit_reason': 'take_profit'},
        {'symbol': 'ETH', 'order_id': 'o2', 'pnl': -20.0, 'exit_timestamp': ts,
         'entry_timestamp': ts, 'exit_reason': 'stop_loss'},
        {'symbol': 'SOL', 'order_id': 'o3', 'pnl': 30.0, 'exit_timestamp': ts,
         'entry_timestamp': ts, 'exit_reason': 'take_profit'},
        {'symbol': 'ADA', 'order_id': 'o4', 'pnl': -10.0, 'exit_timestamp': ts,
         'entry_timestamp': ts, 'exit_reason': 'stop_loss'},
    ]
    conn = _setup_test_db(trades)
    mock_get_conn.return_value = conn

    report = generate_auto_postmortem(days=30)
    s = report['summary']
    assert s['total'] == 4
    assert s['wins'] == 2
    assert s['losses'] == 2
    assert s['win_rate'] == 0.5
    assert s['total_pnl'] == 50.0
    assert s['avg_win'] == 40.0  # (50+30)/2
    assert s['avg_loss'] == -15.0  # (-20+-10)/2
    conn.close()


@patch('src.analysis.auto_postmortem.release_db_connection')
@patch('src.analysis.auto_postmortem.get_db_connection')
def test_postmortem_exit_reason_breakdown(mock_get_conn, mock_release):
    """Exit reason breakdown should group trades correctly."""
    ts = _recent_ts()
    trades = [
        {'symbol': 'BTC', 'order_id': 'o1', 'pnl': -20, 'exit_timestamp': ts,
         'entry_timestamp': ts, 'exit_reason': 'stop_loss'},
        {'symbol': 'ETH', 'order_id': 'o2', 'pnl': -15, 'exit_timestamp': ts,
         'entry_timestamp': ts, 'exit_reason': 'stop_loss'},
        {'symbol': 'SOL', 'order_id': 'o3', 'pnl': 40, 'exit_timestamp': ts,
         'entry_timestamp': ts, 'exit_reason': 'take_profit'},
    ]
    conn = _setup_test_db(trades)
    mock_get_conn.return_value = conn

    report = generate_auto_postmortem(days=30)
    by_reason = report['by_exit_reason']
    assert 'stop_loss' in by_reason
    assert by_reason['stop_loss']['count'] == 2
    assert by_reason['stop_loss']['pnl'] == -35
    assert 'take_profit' in by_reason
    assert by_reason['take_profit']['count'] == 1
    conn.close()


def test_recommendations_high_stoploss_rate():
    """Recommendations should flag when stop-loss exits dominate losses."""
    summary = {'total': 10, 'wins': 2, 'losses': 8,
               'win_rate': 0.2, 'total_pnl': -50, 'avg_win': 10, 'avg_loss': -8.75}
    by_exit_reason = {
        'stop_loss': {'count': 7, 'pnl': -60, 'wins': 1},
        'signal_sell': {'count': 3, 'pnl': 10, 'wins': 1},
    }
    by_confidence = {
        'low': {'count': 0, 'pnl': 0, 'wins': 0},
        'med': {'count': 5, 'pnl': -20, 'wins': 1},
        'high': {'count': 5, 'pnl': -30, 'wins': 1},
    }
    by_symbol = {}

    recs = _generate_recommendations(summary, by_exit_reason, by_confidence, by_symbol)
    assert any('stop-loss' in r.lower() or 'Stop-loss' in r for r in recs)
    assert any('win rate' in r.lower() for r in recs)
