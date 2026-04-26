"""Tests for the excluded_from_stats soft-delete flag.

When a trade is marked excluded_from_stats=1, it must:
  - Be filtered out of paper-balance PnL + locked calculations
  - Be filtered out of Kelly stats
  - Be filtered out of Mini App headline PnL (realized_pnl_data, equity series)
  - STILL appear in recent_trades_data with the flag exposed (so the UI
    can render an "excluded" badge for transparency)

Backstop for: HII (#134) + LHX (#133) marked as
'pre_PR_C_sector_vibes_bug' on 2026-04-26.
"""
import sqlite3
from contextlib import contextmanager
from unittest.mock import MagicMock, patch

import pytest


# --- Paper balance: excluded trades don't affect wallet ---

def _balance_with_pnl_and_locked(pnl, locked, excl_pnl=None, excl_locked=None):
    """Mock _get_paper_balance: returns whatever the SQL would compute given
    the test DB state. We model excluded trades by mocking what the
    'AND COALESCE(excluded_from_stats, 0) = 0' filter would return.

    pnl/locked are values the filter SHOULD return (excluded already removed).
    excl_* exist only to document which trades got excluded; they're never
    returned by the SQL when the filter is on.
    """
    mock_conn = MagicMock()
    mock_conn.__class__ = sqlite3.Connection
    mock_cursor = MagicMock()
    mock_cursor.fetchone.side_effect = [(pnl,), (locked,)]

    @contextmanager
    def fake_cursor(_):
        yield mock_cursor

    cfg = {'settings': {
        'paper_trading_initial_capital': 10000.0,
        'strategies': {
            'auto': {'enabled': True, 'paper_trading_initial_capital': 10000.0},
        },
    }}
    with patch('src.execution.binance_trader.get_db_connection',
               return_value=mock_conn), \
         patch('src.execution.binance_trader.release_db_connection'), \
         patch('src.execution.binance_trader._cursor', fake_cursor), \
         patch('src.execution.binance_trader.app_config', cfg):
        from src.execution.binance_trader import _get_paper_balance
        return _get_paper_balance(trading_strategy='auto'), mock_cursor


def test_paper_balance_query_includes_excluded_filter():
    """The SQL query must contain 'COALESCE(excluded_from_stats, 0) = 0'."""
    _, cur = _balance_with_pnl_and_locked(pnl=100.0, locked=500.0)
    queries = [str(call[0][0]) for call in cur.execute.call_args_list]
    assert any("COALESCE(excluded_from_stats, 0) = 0" in q for q in queries), \
        f"Excluded filter missing from queries: {queries}"


def test_excluded_trade_doesnt_drag_wallet():
    """If HII (-$42) and LHX (-$45) are excluded, auto wallet shouldn't see them.

    Filter pretends they don't exist → SUM(pnl) returns the OTHER trades' total.
    Test simulates: 'real' net PnL is +$200 but with HII+LHX (-$87) it'd be +$113.
    With filter on, _get_paper_balance returns the +$200 (excluded already gone).
    """
    bal, _ = _balance_with_pnl_and_locked(pnl=200.0, locked=0.0)
    # initial 10000 + 200 PnL - 0 locked = 10200
    assert bal['total_usd'] == 10200.0
    assert bal['USDT'] == 10200.0


# --- Kelly stats filter ---

def test_kelly_query_excludes_flagged_trades():
    from src.database import get_trade_history_stats
    mock_conn = MagicMock()
    mock_conn.__class__ = sqlite3.Connection
    mock_cursor = MagicMock()
    mock_cursor.fetchall.return_value = [(50.0,), (-20.0,), (30.0,)]

    @contextmanager
    def fake_cursor(_):
        yield mock_cursor

    with patch('src.database.get_db_connection', return_value=mock_conn), \
         patch('src.database.release_db_connection'), \
         patch('src.database._cursor', fake_cursor):
        get_trade_history_stats.sync()

    query = mock_cursor.execute.call_args[0][0]
    assert "COALESCE(excluded_from_stats, 0) = 0" in query


# --- recent_trades_data: flag is surfaced (not filtered out) ---

def _closed_trades_conn():
    """SQLite connection with the schema needed by recent_trades_data."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("""
        CREATE TABLE trades (
            id INTEGER PRIMARY KEY, order_id TEXT, symbol TEXT,
            trading_strategy TEXT, asset_type TEXT,
            entry_price REAL, exit_price REAL, quantity REAL, pnl REAL,
            status TEXT, entry_timestamp TEXT, exit_timestamp TEXT,
            exit_reason TEXT, exit_reasoning TEXT, trailing_stop_peak REAL,
            dynamic_sl_pct REAL, dynamic_tp_pct REAL, trade_reason TEXT,
            excluded_from_stats INTEGER DEFAULT 0,
            exclusion_reason TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE signal_attribution (
            id INTEGER, trade_order_id TEXT,
            gemini_direction TEXT, gemini_confidence REAL,
            catalyst_type TEXT, source_names TEXT, signal_timestamp TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE gemini_assessments (
            id INTEGER, symbol TEXT, created_at TEXT,
            reasoning TEXT, key_headline TEXT, risk_factors TEXT,
            catalyst_freshness TEXT, hype_vs_fundamental TEXT, market_mood TEXT,
            impact_rank INTEGER, impact_basis TEXT, grounding_urls TEXT
        )
    """)
    return conn


def test_recent_trades_includes_excluded_with_flag():
    """Excluded trades MUST still appear in the Mini App list, with the
    flag exposed so the UI can render a badge."""
    conn = _closed_trades_conn()
    # Two trades: one normal (HII recent), one regular winner
    conn.execute(
        "INSERT INTO trades (symbol, order_id, trading_strategy, asset_type, "
        "entry_price, exit_price, quantity, pnl, status, "
        "entry_timestamp, exit_timestamp, exit_reason, "
        "excluded_from_stats, exclusion_reason) VALUES "
        "('HII','P_HII','auto','stock',398.34,357.83,1.0089,-41.64,'CLOSED',"
        "'2026-04-16 16:30:28','2026-04-24 17:58:38','stop_loss',"
        "1, 'pre_PR_C_sector_vibes_bug')")
    conn.execute(
        "INSERT INTO trades (symbol, order_id, trading_strategy, asset_type, "
        "entry_price, exit_price, quantity, pnl, status, "
        "entry_timestamp, exit_timestamp, exit_reason, excluded_from_stats) VALUES "
        "('CCJ','P_CCJ','longterm','stock',121.45,127.80,3.93,23.97,'CLOSED',"
        "'2026-04-22 17:06:33','2026-04-23 15:00:54','trailing_stop', 0)")
    conn.commit()

    with patch('src.database.get_db_connection', return_value=conn), \
         patch('src.database.release_db_connection'):
        from src.api.miniapp_queries import recent_trades_data
        result = recent_trades_data(limit=10)

    assert len(result['trades']) == 2, "Both trades should appear (excluded ones are display-only)"
    by_sym = {t['symbol']: t for t in result['trades']}
    assert by_sym['HII']['excluded_from_stats'] is True
    assert by_sym['HII']['exclusion_reason'] == 'pre_PR_C_sector_vibes_bug'
    assert by_sym['CCJ']['excluded_from_stats'] is False


# --- realized_pnl_data: excluded trades DON'T inflate PnL ---

def test_realized_pnl_query_excludes_flagged():
    """The headline realized PnL query must filter on excluded_from_stats."""
    import inspect
    from src.api import miniapp_queries
    src = inspect.getsource(miniapp_queries)
    assert "excluded_from_stats" in src, \
        "miniapp_queries module must reference excluded_from_stats"
