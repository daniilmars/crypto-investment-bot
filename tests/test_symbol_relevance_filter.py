"""Tests for src.analysis.symbol_relevance_filter (PR-E.1).

Covers the semantic gate that sits between keyword routing
(news_data._match_article_to_symbols) and per-article Gemini scoring.
"""
import json
from types import SimpleNamespace
from unittest.mock import patch, MagicMock

import pytest

from src.analysis.symbol_relevance_filter import (
    _build_prompt,
    _parse_response,
    clear_relevance_cache,
    filter_by_relevance,
)


@pytest.fixture(autouse=True)
def _reset_cache():
    clear_relevance_cache()
    yield
    clear_relevance_cache()


def _fake_response(text: str):
    return SimpleNamespace(text=text)


def _fake_client(response_text: str):
    client = MagicMock()
    client.models.generate_content.return_value = _fake_response(response_text)
    return client


# ---------- _build_prompt ----------

def test_prompt_includes_business_descriptions():
    article = {'title': 'Crude Jumps', 'description': 'Oil up 10%.'}
    out = _build_prompt(article, ['XOM', 'VLO'], {
        'XOM': 'Integrated oil major',
        'VLO': 'Pure-play refiner — hurt by crude spikes',
    })
    assert 'Integrated oil major' in out
    assert 'pure-play refiner' in out.lower()
    assert 'XOM' in out and 'VLO' in out


def test_prompt_handles_missing_descriptions():
    out = _build_prompt({'title': 'foo'}, ['UNKNOWN'], None)
    assert 'UNKNOWN' in out


# ---------- _parse_response ----------

def test_parse_valid_json():
    text = '{"XOM": "material", "VLO": "unrelated"}'
    assert _parse_response(text, ['XOM', 'VLO']) == {'XOM': 'material', 'VLO': 'unrelated'}


def test_parse_handles_code_fence():
    text = '```json\n{"XOM": "material"}\n```'
    assert _parse_response(text, ['XOM']) == {'XOM': 'material'}


def test_parse_normalises_case():
    text = '{"XOM": "Material", "BP.L": "TANGENTIAL"}'
    out = _parse_response(text, ['XOM', 'BP.L'])
    assert out == {'XOM': 'material', 'BP.L': 'tangential'}


def test_parse_drops_unknown_labels():
    text = '{"XOM": "material", "VLO": "WAT"}'
    out = _parse_response(text, ['XOM', 'VLO'])
    assert out == {'XOM': 'material'}


def test_parse_filters_to_known_candidates():
    """Gemini sometimes invents extra keys — drop those."""
    text = '{"XOM": "material", "TSLA": "material"}'
    assert _parse_response(text, ['XOM', 'BP.L']) == {'XOM': 'material'}


def test_parse_garbage_returns_empty():
    assert _parse_response("not json at all", ['XOM']) == {}
    assert _parse_response("", ['XOM']) == {}


# ---------- filter_by_relevance: edge cases ----------

def test_empty_candidates():
    kept, verdicts = filter_by_relevance({'title_hash': 'h'}, [])
    assert kept == [] and verdicts == {}


def test_single_candidate_skipped_no_call():
    """One candidate → no Gemini call, returned as-is."""
    with patch('src.analysis.gemini_news_analyzer._make_genai_client') as m:
        kept, verdicts = filter_by_relevance(
            {'title_hash': 'h', 'title': 'foo'}, ['XOM'])
    assert kept == ['XOM']
    assert verdicts == {}
    m.assert_not_called()


# ---------- filter_by_relevance: drop / keep behaviour ----------

def test_drops_unrelated_symbols(monkeypatch):
    # Simulates the KO/Coca-Cola Zone case: keyword router matched KO,
    # but the article is about gold mining; Gemini correctly says "unrelated".
    text = '{"KO": "unrelated", "GOLD": "material"}'
    monkeypatch.setattr(
        'src.analysis.gemini_news_analyzer._make_genai_client',
        lambda: _fake_client(text))

    kept, verdicts = filter_by_relevance(
        {'title_hash': 'mining_h', 'title': 'Gold trench at Coca Cola Zone'},
        ['KO', 'GOLD'])
    assert 'KO' not in kept
    assert 'GOLD' in kept
    assert verdicts == {'KO': 'unrelated', 'GOLD': 'material'}


def test_keeps_material_drops_unrelated_keeps_tangential(monkeypatch):
    """Default behaviour: keep material+tangential, drop only unrelated."""
    text = '{"XOM": "material", "BP.L": "tangential", "VLO": "unrelated"}'
    monkeypatch.setattr(
        'src.analysis.gemini_news_analyzer._make_genai_client',
        lambda: _fake_client(text))

    kept, _ = filter_by_relevance(
        {'title_hash': 'oil_h', 'title': 'Oil prices spike'},
        ['XOM', 'BP.L', 'VLO'])
    assert kept == ['XOM', 'BP.L']  # tangential kept by default; unrelated dropped


def test_drop_tangential_strict_mode(monkeypatch):
    """drop_tangential=True keeps only material."""
    text = '{"XOM": "material", "BP.L": "tangential", "VLO": "unrelated"}'
    monkeypatch.setattr(
        'src.analysis.gemini_news_analyzer._make_genai_client',
        lambda: _fake_client(text))

    kept, _ = filter_by_relevance(
        {'title_hash': 'h', 'title': 'foo'},
        ['XOM', 'BP.L', 'VLO'],
        drop_tangential=True)
    assert kept == ['XOM']


def test_unjudged_symbols_kept_fail_open(monkeypatch):
    """If Gemini returns judgement for some but not all, keep the rest."""
    text = '{"XOM": "material"}'  # BP.L missing
    monkeypatch.setattr(
        'src.analysis.gemini_news_analyzer._make_genai_client',
        lambda: _fake_client(text))

    kept, _ = filter_by_relevance(
        {'title_hash': 'h', 'title': 'foo'},
        ['XOM', 'BP.L'])
    assert 'XOM' in kept
    assert 'BP.L' in kept  # kept by fail-open


# ---------- caching ----------

def test_cache_hit_avoids_second_call(monkeypatch):
    calls = []

    def fake_client_factory():
        c = MagicMock()
        def gen(model, contents):
            calls.append(contents)
            return _fake_response('{"XOM": "material", "VLO": "unrelated"}')
        c.models.generate_content.side_effect = gen
        return c

    monkeypatch.setattr(
        'src.analysis.gemini_news_analyzer._make_genai_client', fake_client_factory)

    article = {'title_hash': 'h1', 'title': 'foo'}
    candidates = ['XOM', 'VLO']
    filter_by_relevance(article, candidates)
    filter_by_relevance(article, candidates)
    filter_by_relevance(article, candidates)
    assert len(calls) == 1, "second + third calls should hit cache"


def test_cache_keyed_on_candidate_set(monkeypatch):
    """Different candidate sets → different cache entries."""
    monkeypatch.setattr(
        'src.analysis.gemini_news_analyzer._make_genai_client',
        lambda: _fake_client('{"XOM": "material"}'))

    article = {'title_hash': 'h1', 'title': 'foo'}
    kept1, _ = filter_by_relevance(article, ['XOM', 'BP.L'])
    kept2, _ = filter_by_relevance(article, ['XOM', 'CVX'])
    # Both calls hit Gemini (different candidate sets) — verify by re-calling
    # the second pair: should now hit cache.
    with patch('src.analysis.gemini_news_analyzer._make_genai_client') as m:
        kept3, _ = filter_by_relevance(article, ['XOM', 'CVX'])
        m.assert_not_called()


# ---------- fail-open ----------

def test_no_genai_client_fails_open(monkeypatch):
    """If credentials are missing, return original list unchanged."""
    monkeypatch.setattr(
        'src.analysis.gemini_news_analyzer._make_genai_client', lambda: None)
    kept, verdicts = filter_by_relevance(
        {'title_hash': 'h', 'title': 'foo'}, ['XOM', 'BP.L'])
    assert kept == ['XOM', 'BP.L']
    assert verdicts == {}


def test_gemini_exception_fails_open(monkeypatch):
    def boom():
        c = MagicMock()
        c.models.generate_content.side_effect = RuntimeError("API down")
        return c

    monkeypatch.setattr(
        'src.analysis.gemini_news_analyzer._make_genai_client', boom)
    kept, _ = filter_by_relevance(
        {'title_hash': 'h', 'title': 'foo'}, ['XOM', 'BP.L'])
    assert kept == ['XOM', 'BP.L']  # fail-open


def test_garbage_response_fails_open(monkeypatch):
    """Gemini returns un-parseable text → keep original list."""
    monkeypatch.setattr(
        'src.analysis.gemini_news_analyzer._make_genai_client',
        lambda: _fake_client("Sure! Here's the answer: probably yes."))

    kept, verdicts = filter_by_relevance(
        {'title_hash': 'h', 'title': 'foo'}, ['XOM', 'BP.L'])
    assert kept == ['XOM', 'BP.L']
    assert verdicts == {}
