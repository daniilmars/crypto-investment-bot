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


# --- build_attribution_articles ---

def _patch_get_recent_articles(return_value=None, side_effect=None):
    """Helper: patch the @async_db-wrapped get_recent_articles.sync."""
    mock = MagicMock()
    if side_effect is not None:
        mock.side_effect = side_effect
    else:
        mock.return_value = return_value
    return patch('src.database.get_recent_articles.sync', mock)


def test_build_attribution_articles_filters_incomplete_rows():
    from src.analysis.signal_attribution import build_attribution_articles

    rows = [
        {'title_hash': 'h1', 'source': 'CoinDesk', 'title': 't1'},
        {'title_hash': 'h2', 'source': 'Reuters', 'title': 't2'},
        {'title_hash': '', 'source': 'NoHash', 'title': 't3'},   # dropped
        {'title_hash': 'h4', 'source': '', 'title': 't4'},        # dropped
        {'title_hash': 'h5', 'source': 'Bloomberg', 'title': 't5'},
    ]
    with _patch_get_recent_articles(return_value=rows):
        result = build_attribution_articles('BTC')
    assert len(result) == 3
    assert result[0] == {'title_hash': 'h1', 'source': 'CoinDesk'}
    assert result[2] == {'title_hash': 'h5', 'source': 'Bloomberg'}


def test_build_attribution_articles_returns_empty_on_error():
    from src.analysis.signal_attribution import build_attribution_articles
    with _patch_get_recent_articles(side_effect=Exception("db down")):
        result = build_attribution_articles('BTC')
    assert result == []


# --- _record_trade_attribution integration ---

@patch('src.analysis.signal_attribution.release_db_connection')
@patch('src.analysis.signal_attribution.get_db_connection')
def test_record_trade_attribution_populates_order_id(mock_get_conn, mock_release,
                                                      sqlite_db):
    """The post-order hook should write a single row with order_id linked."""
    mock_get_conn.return_value = sqlite_db
    articles = [
        {'title_hash': 'h1', 'source': 'CoinDesk'},
        {'title_hash': 'h2', 'source': 'Reuters'},
    ]
    from src.orchestration.trade_executor import _record_trade_attribution

    signal = {
        'symbol': 'NVDA', 'signal': 'BUY', 'current_price': 180,
        'gemini_confidence': 0.75, 'gemini_direction': 'bullish',
        'catalyst_type': 'earnings',
    }
    with _patch_get_recent_articles(return_value=articles):
        _record_trade_attribution('NVDA', signal, 'P_42', 'auto')

    cur = sqlite_db.cursor()
    cur.execute(
        "SELECT symbol, trade_order_id, article_hashes, source_names, "
        "gemini_direction, gemini_confidence, catalyst_type "
        "FROM signal_attribution WHERE trade_order_id = ?", ('P_42',))
    row = cur.fetchone()
    assert row is not None
    assert row[0] == 'NVDA'
    assert row[1] == 'P_42'
    assert 'h1' in row[2] and 'h2' in row[2]
    assert 'CoinDesk' in row[3] and 'Reuters' in row[3]
    assert row[4] == 'bullish'
    assert row[5] == 0.75
    assert row[6] == 'earnings'


@patch('src.analysis.signal_attribution.record_signal_attribution',
       side_effect=Exception("oops"))
def test_record_trade_attribution_swallows_errors(mock_rec):
    """Attribution failure must never raise — it's non-critical telemetry."""
    from src.orchestration.trade_executor import _record_trade_attribution
    # Should not raise:
    _record_trade_attribution('BTC', {'symbol': 'BTC', 'signal': 'BUY'},
                              'P_1', 'auto')


# --- WS1: signal['attribution_articles'] preferred over DB lookup ---

@patch('src.analysis.signal_attribution.release_db_connection')
@patch('src.analysis.signal_attribution.get_db_connection')
def test_attribution_uses_signal_articles_when_present(mock_get_conn, mock_release,
                                                       sqlite_db):
    """Articles attached to the signal short-circuit the DB lookup and end
    up in the inserted row. Source/title_hash come from cycle_runner."""
    mock_get_conn.return_value = sqlite_db
    from src.orchestration.trade_executor import _record_trade_attribution

    signal = {
        'symbol': 'XOM', 'signal': 'BUY', 'current_price': 110,
        'gemini_confidence': 0.85, 'gemini_direction': 'bullish',
        'catalyst_type': 'macro',
        'attribution_articles': [
            {'title': 'Strait of Hormuz reopens', 'score': 0.9,
             'source': 'Bloomberg Markets', 'title_hash': 'sha_abc'},
            {'title': 'Oil tankers resume', 'score': 0.8,
             'source': 'CNBC', 'title_hash': 'sha_def'},
        ],
    }
    # DB lookup must NOT be called when signal has its own articles
    with patch('src.analysis.signal_attribution.build_attribution_articles') as mock_build:
        _record_trade_attribution('XOM', signal, 'P_77', 'auto')
        mock_build.assert_not_called()

    cur = sqlite_db.cursor()
    cur.execute(
        "SELECT article_hashes, source_names FROM signal_attribution "
        "WHERE trade_order_id = ?", ('P_77',))
    row = cur.fetchone()
    assert row is not None
    assert 'sha_abc' in row[0] and 'sha_def' in row[0]
    assert 'Bloomberg Markets' in row[1] and 'CNBC' in row[1]


@patch('src.analysis.signal_attribution.release_db_connection')
@patch('src.analysis.signal_attribution.get_db_connection')
def test_attribution_falls_back_to_db_when_no_signal_articles(
        mock_get_conn, mock_release, sqlite_db):
    """If the signal has no attribution_articles (legacy path), the DB
    lookup is used so the existing behavior is preserved."""
    mock_get_conn.return_value = sqlite_db
    from src.orchestration.trade_executor import _record_trade_attribution

    fallback = [{'title_hash': 'h_old', 'source': 'Reuters'}]
    signal = {
        'symbol': 'NVDA', 'signal': 'BUY', 'current_price': 200,
        'gemini_confidence': 0.7, 'gemini_direction': 'bullish',
        'catalyst_type': 'earnings',
    }
    with _patch_get_recent_articles(return_value=fallback):
        _record_trade_attribution('NVDA', signal, 'P_88', 'auto')

    cur = sqlite_db.cursor()
    cur.execute(
        "SELECT article_hashes, source_names FROM signal_attribution "
        "WHERE trade_order_id = ?", ('P_88',))
    row = cur.fetchone()
    assert row[0] == 'h_old'
    assert row[1] == 'Reuters'


@patch('src.analysis.signal_attribution.release_db_connection')
@patch('src.analysis.signal_attribution.get_db_connection')
def test_attribution_articles_list_capped_at_20(mock_get_conn, mock_release,
                                                 sqlite_db):
    """50 articles attached → only first 50 hashes stored, source dedup."""
    mock_get_conn.return_value = sqlite_db
    from src.orchestration.trade_executor import _record_trade_attribution

    arts = [
        {'title': f'a{i}', 'score': 0.5, 'source': 'CoinDesk',
         'title_hash': f'h{i}'}
        for i in range(50)
    ]
    signal = {
        'symbol': 'BTC', 'signal': 'BUY', 'current_price': 75000,
        'gemini_confidence': 0.7, 'gemini_direction': 'bullish',
        'catalyst_type': 'macro',
        'attribution_articles': arts,
    }
    _record_trade_attribution('BTC', signal, 'P_99', 'auto')
    cur = sqlite_db.cursor()
    cur.execute(
        "SELECT article_hashes, source_names FROM signal_attribution "
        "WHERE trade_order_id = ?", ('P_99',))
    hashes, sources = cur.fetchone()
    # record_signal_attribution caps hashes at 50, sources at 20
    assert len(hashes.split(',')) <= 50
    # All 50 articles share one source — dedup leaves just 'CoinDesk'
    assert sources == 'CoinDesk'


@patch('src.analysis.signal_attribution.release_db_connection')
@patch('src.analysis.signal_attribution.get_db_connection')
def test_attribution_handles_articles_without_title_hash(
        mock_get_conn, mock_release, sqlite_db):
    """An article missing title_hash is skipped from hashes but its
    source is still recorded (only sources with non-empty values are kept)."""
    mock_get_conn.return_value = sqlite_db
    from src.orchestration.trade_executor import _record_trade_attribution

    signal = {
        'symbol': 'XOM', 'signal': 'BUY', 'current_price': 110,
        'gemini_confidence': 0.7, 'gemini_direction': 'bullish',
        'catalyst_type': 'macro',
        'attribution_articles': [
            {'title': 'has hash', 'score': 0.9, 'source': 'AP News',
             'title_hash': 'sha_aaa'},
            {'title': 'no hash', 'score': 0.8, 'source': 'WSJ',
             'title_hash': None},
        ],
    }
    _record_trade_attribution('XOM', signal, 'P_111', 'auto')
    cur = sqlite_db.cursor()
    cur.execute(
        "SELECT article_hashes, source_names FROM signal_attribution "
        "WHERE trade_order_id = ?", ('P_111',))
    hashes, sources = cur.fetchone()
    assert hashes == 'sha_aaa'  # null hash filtered
    # Both sources still recorded
    assert 'AP News' in sources and 'WSJ' in sources


@patch('src.analysis.signal_attribution.release_db_connection')
@patch('src.analysis.signal_attribution.get_db_connection')
def test_attribution_dedupes_repeated_sources(mock_get_conn, mock_release,
                                              sqlite_db):
    """Multiple articles from the same source — only one source name stored."""
    mock_get_conn.return_value = sqlite_db
    from src.orchestration.trade_executor import _record_trade_attribution

    arts = [
        {'title': 'a', 'score': 0.9, 'source': 'Bloomberg', 'title_hash': 'h1'},
        {'title': 'b', 'score': 0.8, 'source': 'Bloomberg', 'title_hash': 'h2'},
        {'title': 'c', 'score': 0.7, 'source': 'Bloomberg', 'title_hash': 'h3'},
    ]
    signal = {
        'symbol': 'XOM', 'signal': 'BUY', 'current_price': 110,
        'gemini_confidence': 0.7, 'gemini_direction': 'bullish',
        'catalyst_type': 'macro',
        'attribution_articles': arts,
    }
    _record_trade_attribution('XOM', signal, 'P_222', 'auto')
    cur = sqlite_db.cursor()
    cur.execute(
        "SELECT source_names FROM signal_attribution "
        "WHERE trade_order_id = ?", ('P_222',))
    sources = cur.fetchone()[0]
    # Single Bloomberg, no duplicate
    assert sources == 'Bloomberg'


@patch('src.analysis.signal_attribution.release_db_connection')
@patch('src.analysis.signal_attribution.get_db_connection')
def test_attribution_empty_articles_list_treated_as_fallback(
        mock_get_conn, mock_release, sqlite_db):
    """signal['attribution_articles'] = [] should fall back to DB lookup
    (truthy check via `or` operator)."""
    mock_get_conn.return_value = sqlite_db
    from src.orchestration.trade_executor import _record_trade_attribution

    fallback = [{'title_hash': 'h_db', 'source': 'FallbackSource'}]
    signal = {
        'symbol': 'BTC', 'signal': 'BUY', 'current_price': 75000,
        'gemini_confidence': 0.7, 'gemini_direction': 'bullish',
        'catalyst_type': 'macro',
        'attribution_articles': [],   # empty — should fall back
    }
    with _patch_get_recent_articles(return_value=fallback):
        _record_trade_attribution('BTC', signal, 'P_333', 'auto')
    cur = sqlite_db.cursor()
    cur.execute(
        "SELECT source_names FROM signal_attribution "
        "WHERE trade_order_id = ?", ('P_333',))
    sources = cur.fetchone()[0]
    assert sources == 'FallbackSource'
