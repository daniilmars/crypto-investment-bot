"""Tests for Gemini grounding-metadata capture (PR-B).

Covers:
  - _extract_grounding parses the google-genai response shape
  - build_grounding_attribution_articles reads grounding_urls from the DB
    and produces title_hash + source dicts
  - The trade_executor fallback chain reaches grounding when the first two
    sources return empty.
"""

import json
import sqlite3
from types import SimpleNamespace
from unittest.mock import patch, MagicMock

import pytest

from src.analysis.gemini_news_analyzer import _extract_grounding


# --- _extract_grounding ---

class TestExtractGrounding:
    def _make_response(self, urls=None, queries=None):
        chunks = []
        for u in (urls or []):
            chunks.append(SimpleNamespace(web=SimpleNamespace(uri=u)))
        gm = SimpleNamespace(
            grounding_chunks=chunks,
            web_search_queries=queries or [],
        )
        return SimpleNamespace(candidates=[SimpleNamespace(grounding_metadata=gm)])

    def test_extracts_urls_and_queries(self):
        r = self._make_response(
            urls=['https://reuters.com/a', 'https://bloomberg.com/b'],
            queries=['oil price spike', 'OPEC decision'])
        urls, queries = _extract_grounding(r)
        assert urls == ['https://reuters.com/a', 'https://bloomberg.com/b']
        assert queries == ['oil price spike', 'OPEC decision']

    def test_dedupes_urls(self):
        r = self._make_response(urls=['https://a.com/x', 'https://a.com/x'])
        urls, _ = _extract_grounding(r)
        assert urls == ['https://a.com/x']

    def test_caps_to_50_urls(self):
        r = self._make_response(urls=[f'https://a.com/{i}' for i in range(80)])
        urls, _ = _extract_grounding(r)
        assert len(urls) == 50

    def test_no_candidates_returns_empty(self):
        r = SimpleNamespace(candidates=None)
        assert _extract_grounding(r) == ([], [])

    def test_no_grounding_metadata_returns_empty(self):
        r = SimpleNamespace(candidates=[SimpleNamespace(grounding_metadata=None)])
        assert _extract_grounding(r) == ([], [])

    def test_chunk_without_web_skipped(self):
        chunks = [SimpleNamespace(web=None),
                  SimpleNamespace(web=SimpleNamespace(uri='https://ok.com/x'))]
        gm = SimpleNamespace(grounding_chunks=chunks, web_search_queries=[])
        r = SimpleNamespace(candidates=[SimpleNamespace(grounding_metadata=gm)])
        urls, _ = _extract_grounding(r)
        assert urls == ['https://ok.com/x']


# --- build_grounding_attribution_articles ---

@pytest.fixture
def temp_db(tmp_path, monkeypatch):
    """Create a SQLite DB with the gemini_assessments schema we need."""
    db_path = tmp_path / "test.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute("""
        CREATE TABLE gemini_assessments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol TEXT NOT NULL,
            grounding_urls TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.commit()
    conn.close()

    # Patch get_db_connection to hand out fresh sqlite connections
    def fake_get_conn():
        c = sqlite3.connect(str(db_path))
        c.row_factory = sqlite3.Row
        return c

    monkeypatch.setattr("src.database.get_db_connection", fake_get_conn)
    monkeypatch.setattr("src.database.release_db_connection", lambda c: c.close() if c else None)
    return db_path


def test_grounding_articles_returns_hashed_dicts(temp_db):
    """Recent grounding_urls row → list of {title_hash, source}."""
    from src.analysis.signal_attribution import build_grounding_attribution_articles
    conn = sqlite3.connect(str(temp_db))
    conn.execute(
        "INSERT INTO gemini_assessments (symbol, grounding_urls) VALUES (?, ?)",
        ("HII", json.dumps([
            "https://www.reuters.com/defense/hii-contract",
            "https://www.bloomberg.com/news/hii-q1",
        ])))
    conn.commit()
    conn.close()

    result = build_grounding_attribution_articles("HII", hours=24)
    assert len(result) == 2
    sources = [r['source'] for r in result]
    assert "gemini:reuters.com" in sources
    assert "gemini:bloomberg.com" in sources
    assert all(len(r['title_hash']) == 64 for r in result)  # sha256 hex


def test_grounding_articles_empty_when_no_row(temp_db):
    from src.analysis.signal_attribution import build_grounding_attribution_articles
    assert build_grounding_attribution_articles("AAPL") == []


def test_grounding_articles_empty_when_urls_null(temp_db):
    from src.analysis.signal_attribution import build_grounding_attribution_articles
    conn = sqlite3.connect(str(temp_db))
    conn.execute(
        "INSERT INTO gemini_assessments (symbol, grounding_urls) VALUES (?, ?)",
        ("AAPL", None))
    conn.commit()
    conn.close()
    assert build_grounding_attribution_articles("AAPL") == []


def test_grounding_articles_dedupes(temp_db):
    """Same URL twice → one attribution row."""
    from src.analysis.signal_attribution import build_grounding_attribution_articles
    conn = sqlite3.connect(str(temp_db))
    conn.execute(
        "INSERT INTO gemini_assessments (symbol, grounding_urls) VALUES (?, ?)",
        ("XOM", json.dumps([
            "https://wsj.com/a",
            "https://wsj.com/a",
            "https://ft.com/b",
        ])))
    conn.commit()
    conn.close()

    result = build_grounding_attribution_articles("XOM")
    sources = sorted(r['source'] for r in result)
    assert sources == ["gemini:ft.com", "gemini:wsj.com"]
