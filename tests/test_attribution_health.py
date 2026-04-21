"""Tests for src/analysis/attribution_health.py."""

import sqlite3
from unittest.mock import patch

import pytest


def _make_conn():
    """In-memory SQLite with signal_attribution + attribution_coverage_history."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("""
        CREATE TABLE signal_attribution (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol TEXT, signal_type TEXT,
            source_names TEXT, article_hashes TEXT,
            trade_order_id TEXT, resolved_at TIMESTAMP,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.execute("""
        CREATE TABLE attribution_coverage_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            computed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            window_days INTEGER NOT NULL,
            total_attributions INTEGER NOT NULL,
            with_sources INTEGER NOT NULL,
            with_hashes INTEGER NOT NULL,
            with_trade INTEGER NOT NULL,
            with_resolution INTEGER NOT NULL,
            coverage_pct_sources REAL NOT NULL
        )
    """)
    conn.commit()
    return conn


def _seed_attribution(conn, with_sources_count: int, without_count: int):
    """Seed the signal_attribution table with N rows that have sources and
    M rows that don't. All rows have created_at=now so they're inside any
    reasonable window."""
    for i in range(with_sources_count):
        conn.execute(
            "INSERT INTO signal_attribution "
            "(symbol, signal_type, source_names, article_hashes, "
            "trade_order_id, resolved_at) "
            "VALUES (?, 'BUY', 'reuters,cnbc', 'h1,h2', ?, ?)",
            (f"SYM{i}", f"ord_{i}", "2026-04-21 12:00:00"))
    for i in range(without_count):
        conn.execute(
            "INSERT INTO signal_attribution "
            "(symbol, signal_type, source_names, article_hashes, "
            "trade_order_id, resolved_at) "
            "VALUES (?, 'BUY', NULL, NULL, ?, NULL)",
            (f"NOSRC{i}", f"ord_no_{i}"))
    conn.commit()


# --- compute_coverage ---

def test_compute_coverage_empty_db_returns_zeros():
    conn = _make_conn()
    with patch("src.analysis.attribution_health.get_db_connection", return_value=conn), \
         patch("src.analysis.attribution_health.release_db_connection"):
        from src.analysis.attribution_health import compute_coverage
        snap = compute_coverage(conn, window_days=7)
    assert snap["total_attributions"] == 0
    assert snap["with_sources"] == 0
    assert snap["coverage_pct_sources"] == 0.0
    assert snap["window_days"] == 7


def test_compute_coverage_mixed_rows_computes_pct():
    conn = _make_conn()
    _seed_attribution(conn, with_sources_count=6, without_count=4)
    with patch("src.analysis.attribution_health.get_db_connection", return_value=conn), \
         patch("src.analysis.attribution_health.release_db_connection"):
        from src.analysis.attribution_health import compute_coverage
        snap = compute_coverage(conn, window_days=7)
    assert snap["total_attributions"] == 10
    assert snap["with_sources"] == 6
    assert snap["with_hashes"] == 6
    assert snap["with_trade"] == 10
    assert snap["with_resolution"] == 6
    assert snap["coverage_pct_sources"] == 60.0


# --- save_snapshot ---

def test_save_snapshot_persists_row():
    conn = _make_conn()
    from src.analysis.attribution_health import save_snapshot
    snap = {
        "window_days": 7, "total_attributions": 16,
        "with_sources": 6, "with_hashes": 6,
        "with_trade": 16, "with_resolution": 6,
        "coverage_pct_sources": 37.5,
    }
    save_snapshot(conn, snap)
    cur = conn.execute(
        "SELECT window_days, total_attributions, coverage_pct_sources "
        "FROM attribution_coverage_history")
    row = cur.fetchone()
    assert row[0] == 7
    assert row[1] == 16
    assert row[2] == pytest.approx(37.5)


# --- compute_and_save_coverage (top-level wrapper) ---

def test_compute_and_save_coverage_handles_empty_db():
    conn = _make_conn()
    with patch("src.analysis.attribution_health.get_db_connection", return_value=conn), \
         patch("src.analysis.attribution_health.release_db_connection"):
        from src.analysis.attribution_health import compute_and_save_coverage
        result = compute_and_save_coverage(windows=(7, 30))
    assert "error" not in result
    assert result["persisted"] == 2  # 7d + 30d snapshots, both zero
    assert len(result["snapshots"]) == 2


def test_compute_and_save_coverage_no_db_returns_error():
    with patch("src.analysis.attribution_health.get_db_connection", return_value=None):
        from src.analysis.attribution_health import compute_and_save_coverage
        result = compute_and_save_coverage(windows=(7,))
    assert result.get("error") == "no_db_connection"
    assert result["persisted"] == 0


def test_compute_and_save_coverage_persists_both_windows():
    conn = _make_conn()
    _seed_attribution(conn, with_sources_count=3, without_count=1)
    with patch("src.analysis.attribution_health.get_db_connection", return_value=conn), \
         patch("src.analysis.attribution_health.release_db_connection"):
        from src.analysis.attribution_health import compute_and_save_coverage
        result = compute_and_save_coverage(windows=(7, 30))
    cur = conn.execute(
        "SELECT window_days, coverage_pct_sources "
        "FROM attribution_coverage_history ORDER BY window_days")
    rows = cur.fetchall()
    assert [r[0] for r in rows] == [7, 30]
    # 3 with sources / 4 total = 75%
    assert rows[0][1] == pytest.approx(75.0)
    assert rows[1][1] == pytest.approx(75.0)
    assert result["persisted"] == 2


# --- get_recent_trajectory ---

def test_get_recent_trajectory_returns_newest_first():
    conn = _make_conn()
    from src.analysis.attribution_health import save_snapshot, get_recent_trajectory

    # Insert 3 snapshots at different timestamps
    conn.execute(
        "INSERT INTO attribution_coverage_history "
        "(computed_at, window_days, total_attributions, with_sources, "
        "with_hashes, with_trade, with_resolution, coverage_pct_sources) "
        "VALUES ('2026-04-21 10:00:00', 7, 16, 6, 6, 16, 6, 37.5)")
    conn.execute(
        "INSERT INTO attribution_coverage_history "
        "(computed_at, window_days, total_attributions, with_sources, "
        "with_hashes, with_trade, with_resolution, coverage_pct_sources) "
        "VALUES ('2026-04-22 10:00:00', 7, 18, 10, 10, 18, 10, 55.6)")
    conn.execute(
        "INSERT INTO attribution_coverage_history "
        "(computed_at, window_days, total_attributions, with_sources, "
        "with_hashes, with_trade, with_resolution, coverage_pct_sources) "
        "VALUES ('2026-04-23 10:00:00', 7, 20, 18, 18, 20, 18, 90.0)")
    # Different window shouldn't leak into 7d query
    conn.execute(
        "INSERT INTO attribution_coverage_history "
        "(computed_at, window_days, total_attributions, with_sources, "
        "with_hashes, with_trade, with_resolution, coverage_pct_sources) "
        "VALUES ('2026-04-22 10:00:00', 30, 100, 80, 80, 100, 80, 80.0)")
    conn.commit()

    with patch("src.analysis.attribution_health.get_db_connection", return_value=conn), \
         patch("src.analysis.attribution_health.release_db_connection"):
        rows = get_recent_trajectory(window_days=7, limit=14)

    assert len(rows) == 3
    # Newest-first ordering
    pcts = [r["coverage_pct_sources"] for r in rows]
    assert pcts == [90.0, 55.6, 37.5]
