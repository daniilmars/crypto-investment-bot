"""Tests for src/analysis/auto_tuner.py"""

import pytest
from unittest.mock import patch, MagicMock
import sqlite3


@pytest.fixture
def sqlite_db():
    """Create an in-memory SQLite DB with required tables."""
    conn = sqlite3.connect(':memory:')
    conn.execute("""
        CREATE TABLE trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol TEXT NOT NULL,
            order_id TEXT UNIQUE,
            side TEXT NOT NULL,
            entry_price REAL NOT NULL,
            quantity REAL NOT NULL,
            status TEXT NOT NULL,
            pnl REAL,
            exit_price REAL,
            entry_timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            exit_timestamp TIMESTAMP,
            trading_mode TEXT DEFAULT 'paper',
            exchange_order_id TEXT,
            fees REAL DEFAULT 0,
            fill_price REAL,
            fill_quantity REAL,
            asset_type TEXT DEFAULT 'crypto',
            trailing_stop_peak REAL,
            trading_strategy TEXT DEFAULT 'manual'
        )
    """)
    conn.execute("""
        CREATE TABLE tuning_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            tuning_run_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            parameter_name TEXT NOT NULL,
            old_value REAL NOT NULL,
            new_value REAL NOT NULL,
            sample_trades INTEGER,
            old_sharpe REAL,
            new_sharpe REAL,
            old_win_rate REAL,
            new_win_rate REAL,
            applied INTEGER DEFAULT 0,
            reverted INTEGER DEFAULT 0,
            reverted_at TIMESTAMP,
            revert_reason TEXT
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
    conn.commit()
    return conn


def _insert_trades(conn, n=25, win_rate=0.6):
    """Insert n closed trades with given win rate."""
    import random
    for i in range(n):
        entry = 100.0
        is_win = random.random() < win_rate
        exit_p = entry * (1.05 if is_win else 0.97)
        pnl = (exit_p - entry) * 1.0
        conn.execute(
            "INSERT INTO trades (symbol, order_id, side, entry_price, quantity, "
            "status, pnl, exit_price, exit_timestamp) VALUES "
            "(?, ?, 'BUY', ?, 1.0, 'CLOSED', ?, ?, datetime('now'))",
            (f'BTC', f'ORD-{i}', entry, pnl, exit_p))
    conn.commit()


def test_evaluate_params():
    from src.analysis.auto_tuner import _evaluate_params

    trades = [
        {'entry_price': 100, 'exit_price': 105, 'pnl': 5},
        {'entry_price': 100, 'exit_price': 97, 'pnl': -3},
        {'entry_price': 100, 'exit_price': 108, 'pnl': 8},
        {'entry_price': 100, 'exit_price': 96, 'pnl': -4},
        {'entry_price': 100, 'exit_price': 103, 'pnl': 3},
    ]

    params = {'stop_loss_percentage': 0.035, 'take_profit_percentage': 0.08}
    metrics = _evaluate_params(trades, params)

    assert 'sharpe' in metrics
    assert 'win_rate' in metrics
    assert 'total_return' in metrics
    assert metrics['win_rate'] > 0
    assert isinstance(metrics['sharpe'], float)


def test_evaluate_params_empty():
    from src.analysis.auto_tuner import _evaluate_params
    metrics = _evaluate_params([], {})
    assert metrics['sharpe'] == 0.0
    assert metrics['win_rate'] == 0.0


def test_std():
    from src.analysis.auto_tuner import _std
    assert _std([1, 2, 3, 4, 5]) == pytest.approx(1.5811, rel=0.01)
    assert _std([]) == 0.0
    assert _std([5]) == 0.0


@patch('src.analysis.auto_tuner.app_config', {
    'settings': {
        'autonomous_bot': {
            'auto_tuner': {'enabled': False}
        }
    }
})
def test_auto_tune_disabled():
    from src.analysis.auto_tuner import run_auto_tune
    result = run_auto_tune()
    assert result.get('skipped') is True
    assert 'disabled' in result.get('reason', '')


@patch('src.analysis.auto_tuner.release_db_connection')
@patch('src.analysis.auto_tuner.get_db_connection')
@patch('src.analysis.auto_tuner.app_config', {
    'settings': {
        'stop_loss_percentage': 0.035,
        'take_profit_percentage': 0.08,
        'autonomous_bot': {
            'auto_tuner': {
                'enabled': True,
                'min_sample_trades': 5,
                'min_sharpe_improvement': 0.1,
                'max_param_changes_per_cycle': 2,
            }
        }
    }
})
def test_auto_tune_insufficient_trades(mock_get_conn, mock_release, sqlite_db):
    mock_get_conn.return_value = sqlite_db
    from src.analysis.auto_tuner import run_auto_tune

    # No trades in DB
    result = run_auto_tune()
    assert result.get('skipped') is True
    assert 'insufficient_trades' in result.get('reason', '')


@patch('src.analysis.auto_tuner.release_db_connection')
@patch('src.analysis.auto_tuner.get_db_connection')
@patch('src.analysis.feedback_loop.release_db_connection')
@patch('src.analysis.feedback_loop.get_db_connection')
@patch('src.analysis.auto_tuner.app_config', {
    'settings': {
        'stop_loss_percentage': 0.035,
        'take_profit_percentage': 0.08,
        'autonomous_bot': {
            'auto_tuner': {
                'enabled': True,
                'min_sample_trades': 5,
                'min_sharpe_improvement': 0.01,  # Low threshold for testing
                'max_param_changes_per_cycle': 2,
                'sl_min': 0.015,
                'sl_max': 0.06,
                'tp_min': 0.03,
                'tp_max': 0.15,
            }
        }
    }
})
def test_auto_tune_with_trades(mock_fb_conn, mock_fb_release,
                                mock_get_conn, mock_release, sqlite_db):
    mock_get_conn.return_value = sqlite_db
    mock_fb_conn.return_value = sqlite_db
    _insert_trades(sqlite_db, n=30, win_rate=0.5)

    from src.analysis.auto_tuner import run_auto_tune
    result = run_auto_tune()

    assert not result.get('skipped')
    assert result.get('trades_analyzed') == 30
    assert 'current_sharpe' in result
    assert 'improvements_found' in result


def test_safety_bounds():
    """Verify that default safety bounds are reasonable."""
    from src.analysis.auto_tuner import DEFAULT_BOUNDS
    assert DEFAULT_BOUNDS['stop_loss_percentage'] == (0.05, 0.15)
    assert DEFAULT_BOUNDS['take_profit_percentage'] == (0.08, 0.50)
    assert DEFAULT_BOUNDS['min_gemini_confidence'] == (0.35, 0.75)


@patch('src.analysis.auto_tuner.release_db_connection')
@patch('src.analysis.auto_tuner.get_db_connection')
def test_get_tuning_history(mock_get_conn, mock_release, sqlite_db):
    mock_get_conn.return_value = sqlite_db
    from src.analysis.auto_tuner import get_tuning_history

    # Insert a test record
    sqlite_db.execute(
        "INSERT INTO tuning_history (parameter_name, old_value, new_value, sample_trades) "
        "VALUES ('stop_loss_percentage', 0.035, 0.04, 25)")
    sqlite_db.commit()

    mock_get_conn.return_value = sqlite_db
    history = get_tuning_history(limit=10)
    assert len(history) == 1
    assert history[0]['parameter_name'] == 'stop_loss_percentage'


@patch('src.analysis.auto_tuner.release_db_connection')
@patch('src.analysis.auto_tuner.get_db_connection')
@patch('src.analysis.auto_tuner.app_config', {
    'settings': {
        'stop_loss_percentage': 0.035,
        'take_profit_percentage': 0.08,
        'signal_cooldown_hours': 4,
    }
})
def test_get_current_vs_suggested(mock_get_conn, mock_release, sqlite_db):
    mock_get_conn.return_value = sqlite_db
    from src.analysis.auto_tuner import get_current_vs_suggested

    info = get_current_vs_suggested()
    assert 'current_params' in info
    assert 'current_metrics' in info
    assert info['current_params']['stop_loss_percentage'] == 0.035
    assert info['current_params']['take_profit_percentage'] == 0.08
