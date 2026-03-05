"""Tests for src/collectors/source_discovery.py and source_evaluator.py"""

import pytest
from unittest.mock import patch, MagicMock


def test_probe_paths():
    """Verify RSS probe paths list is sensible."""
    from src.collectors.source_discovery import RSS_PROBE_PATHS
    assert '/feed' in RSS_PROBE_PATHS
    assert '/rss' in RSS_PROBE_PATHS
    assert '/atom.xml' in RSS_PROBE_PATHS


def test_extract_cited_domains():
    from src.collectors.source_discovery import extract_cited_domains

    articles = [
        {'source_url': 'https://coindesk.com/article1',
         'description': 'Check https://theblock.co/news for more'},
    ] * 6  # Repeat to hit min_citations

    result = extract_cited_domains(articles, min_citations=5)
    assert any('theblock.co' in d for d, _ in result)


def test_extract_cited_domains_empty():
    from src.collectors.source_discovery import extract_cited_domains
    result = extract_cited_domains([])
    assert result == []


@patch('src.collectors.source_discovery.app_config', {
    'settings': {'autonomous_bot': {
        'source_discovery': {'enabled': False}
    }}
})
def test_discovery_cycle_disabled():
    from src.collectors.source_discovery import run_discovery_cycle
    result = run_discovery_cycle()
    assert result.get('skipped') is True


@patch('src.collectors.source_discovery.requests')
def test_probe_domain_for_rss_success(mock_requests):
    from src.collectors.source_discovery import _probe_domain_for_rss

    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.headers = {'content-type': 'application/xml'}
    mock_resp.text = '''<?xml version="1.0"?>
    <rss version="2.0"><channel><title>Test Feed</title>
    <item><title>Article 1</title></item>
    <item><title>Article 2</title></item>
    </channel></rss>'''
    mock_requests.get.return_value = mock_resp

    results = _probe_domain_for_rss('test.com')
    assert len(results) == 1
    assert results[0]['domain'] == 'test.com'
    assert results[0]['article_count'] == 2


@patch('src.collectors.source_discovery.requests')
def test_probe_domain_for_rss_failure(mock_requests):
    from src.collectors.source_discovery import _probe_domain_for_rss

    mock_requests.get.side_effect = Exception("Connection refused")
    results = _probe_domain_for_rss('nonexistent.invalid')
    assert results == []


# --- Source Evaluator Tests ---

def test_check_quality():
    from src.collectors.source_evaluator import _check_quality

    good_articles = [
        {'title': 'Bitcoin hits new high as institutional demand grows',
         'description': 'The price of Bitcoin surged past $60,000 today.'},
        {'title': 'Ethereum 2.0 upgrade timeline announced',
         'description': 'The Ethereum Foundation has released a new roadmap.'},
    ]
    score = _check_quality(good_articles)
    assert score > 0.5

    bad_articles = [
        {'title': '', 'description': ''},
        {'title': 'Hi', 'description': ''},
    ]
    score = _check_quality(bad_articles)
    assert score < 0.5


def test_check_quality_empty():
    from src.collectors.source_evaluator import _check_quality
    assert _check_quality([]) == 0.0


def test_check_quality_non_english():
    from src.collectors.source_evaluator import _check_quality
    articles = [
        {'title': '仮想通貨市場の最新ニュース', 'description': 'ビットコインは急騰しました'},
    ]
    score = _check_quality(articles)
    assert score == 0.0  # Should reject non-English


@patch('src.collectors.source_evaluator.requests')
def test_check_availability_success(mock_requests):
    from src.collectors.source_evaluator import _check_availability

    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.text = '''<?xml version="1.0"?>
    <rss version="2.0"><channel>
    <item><title>Article 1</title><description>Desc</description></item>
    <item><title>Article 2</title><description>Desc</description></item>
    </channel></rss>'''
    mock_requests.get.return_value = mock_resp

    articles = _check_availability('https://test.com/feed')
    assert articles is not None
    assert len(articles) == 2


@patch('src.collectors.source_evaluator.requests')
def test_check_availability_failure(mock_requests):
    from src.collectors.source_evaluator import _check_availability

    mock_resp = MagicMock()
    mock_resp.status_code = 404
    mock_requests.get.return_value = mock_resp

    articles = _check_availability('https://test.com/nonexistent')
    assert articles is None


def test_evaluate_candidate_unavailable():
    from src.collectors.source_evaluator import evaluate_candidate

    with patch('src.collectors.source_evaluator._check_availability', return_value=None):
        result = evaluate_candidate('https://bad.com/feed', 'Bad Feed')
        assert result['passed'] is False
        assert result['reason'] == 'feed_unavailable'


def test_evaluate_candidate_too_few_articles():
    from src.collectors.source_evaluator import evaluate_candidate

    with patch('src.collectors.source_evaluator._check_availability',
               return_value=[{'title': 'One', 'description': 'desc'}]):
        result = evaluate_candidate('https://sparse.com/feed', 'Sparse Feed')
        assert result['passed'] is False
        assert 'too_few_articles' in result['reason']
