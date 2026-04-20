"""Tests for src/analysis/recent_assessment.py — the analyst-concur lookup
used by position_monitor to tag trailing-stop exits."""

import sqlite3
from datetime import datetime, timedelta, timezone
from unittest.mock import patch


def _make_conn_with_row(symbol, direction, hours_ago):
    """Build an in-memory SQLite DB seeded with one assessment row."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("""
        CREATE TABLE gemini_assessments (
            id INTEGER PRIMARY KEY,
            symbol TEXT, direction TEXT, confidence REAL,
            catalyst_type TEXT, catalyst_freshness TEXT,
            catalyst_count INTEGER, hype_vs_fundamental TEXT,
            risk_factors TEXT, reasoning TEXT, key_headline TEXT,
            market_mood TEXT, created_at TIMESTAMP
        )
    """)
    ts = (datetime.now(timezone.utc) - timedelta(hours=hours_ago)).strftime(
        "%Y-%m-%d %H:%M:%S")
    conn.execute(
        "INSERT INTO gemini_assessments (symbol, direction, confidence, created_at) "
        "VALUES (?, ?, ?, ?)",
        (symbol, direction, 0.78, ts))
    conn.commit()
    return conn


def test_returns_none_when_no_bearish_assessment():
    conn = _make_conn_with_row("NVDA", "bullish", hours_ago=2)
    with patch("src.analysis.recent_assessment.get_db_connection", return_value=conn), \
         patch("src.analysis.recent_assessment.release_db_connection"):
        from src.analysis.recent_assessment import get_recent_bearish_assessment
        assert get_recent_bearish_assessment("NVDA", 8) is None


def test_returns_row_when_recent_bearish_exists():
    conn = _make_conn_with_row("NVDA", "bearish", hours_ago=2)
    with patch("src.analysis.recent_assessment.get_db_connection", return_value=conn), \
         patch("src.analysis.recent_assessment.release_db_connection"):
        from src.analysis.recent_assessment import get_recent_bearish_assessment
        row = get_recent_bearish_assessment("NVDA", 8)
        assert row is not None
        assert row["symbol"] == "NVDA"
        assert row["direction"] == "bearish"


def test_ignores_bearish_older_than_window():
    conn = _make_conn_with_row("NVDA", "bearish", hours_ago=24)
    with patch("src.analysis.recent_assessment.get_db_connection", return_value=conn), \
         patch("src.analysis.recent_assessment.release_db_connection"):
        from src.analysis.recent_assessment import get_recent_bearish_assessment
        assert get_recent_bearish_assessment("NVDA", 8) is None


def test_returns_most_recent_when_multiple():
    conn = _make_conn_with_row("NVDA", "bearish", hours_ago=3)
    # Add a second, more recent bearish row
    ts = (datetime.now(timezone.utc) - timedelta(hours=1)).strftime(
        "%Y-%m-%d %H:%M:%S")
    conn.execute(
        "INSERT INTO gemini_assessments (symbol, direction, confidence, created_at) "
        "VALUES (?, ?, ?, ?)",
        ("NVDA", "bearish", 0.91, ts))
    conn.commit()
    with patch("src.analysis.recent_assessment.get_db_connection", return_value=conn), \
         patch("src.analysis.recent_assessment.release_db_connection"):
        from src.analysis.recent_assessment import get_recent_bearish_assessment
        row = get_recent_bearish_assessment("NVDA", 8)
        assert row is not None
        assert row["confidence"] == 0.91  # the more recent one


def test_empty_symbol_returns_none():
    from src.analysis.recent_assessment import get_recent_bearish_assessment
    assert get_recent_bearish_assessment("", 8) is None
    assert get_recent_bearish_assessment(None, 8) is None


def test_db_error_returns_none():
    """Transient DB failure must not block the trailing-stop SELL."""
    with patch("src.analysis.recent_assessment.get_db_connection",
               side_effect=Exception("connection lost")):
        from src.analysis.recent_assessment import get_recent_bearish_assessment
        assert get_recent_bearish_assessment("NVDA", 8) is None


# --- get_recent_assessment (direction-agnostic — for rotation attribution) ---

def test_recent_assessment_returns_any_direction():
    conn = _make_conn_with_row("XOM", "bullish", hours_ago=0.2)
    conn.execute(
        "UPDATE gemini_assessments SET catalyst_type=?, catalyst_freshness=? "
        "WHERE symbol=?",
        ("macro", "recent", "XOM"))
    conn.commit()
    with patch("src.analysis.recent_assessment.get_db_connection",
               return_value=conn), \
         patch("src.analysis.recent_assessment.release_db_connection"):
        from src.analysis.recent_assessment import get_recent_assessment
        row = get_recent_assessment("XOM", hours=0.5)
        assert row is not None
        assert row["direction"] == "bullish"
        assert row["catalyst_type"] == "macro"


def test_recent_assessment_respects_window():
    """30-min lookup window — 2h-old row is outside 0.5h window."""
    conn = _make_conn_with_row("XOM", "bullish", hours_ago=2)
    with patch("src.analysis.recent_assessment.get_db_connection",
               return_value=conn), \
         patch("src.analysis.recent_assessment.release_db_connection"):
        from src.analysis.recent_assessment import get_recent_assessment
        assert get_recent_assessment("XOM", hours=0.5) is None


def test_recent_assessment_empty_symbol():
    from src.analysis.recent_assessment import get_recent_assessment
    assert get_recent_assessment("", hours=0.5) is None
    assert get_recent_assessment(None, hours=0.5) is None


def test_recent_assessment_db_error_returns_none():
    with patch("src.analysis.recent_assessment.get_db_connection",
               side_effect=Exception("connection lost")):
        from src.analysis.recent_assessment import get_recent_assessment
        assert get_recent_assessment("NVDA", hours=0.5) is None
