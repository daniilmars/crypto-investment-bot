"""Tests for Gemini per-article sentiment scoring (score_articles_batch).

Apr 20 migration: switched from the legacy `vertexai` SDK to the new
`google.genai` SDK so we can pass `ThinkingConfig(thinking_budget=0)`.
Tests mock the `google.genai` client + `_make_genai_client` factory.
"""

import json
import pytest
from unittest.mock import patch, MagicMock

from src.analysis.gemini_news_analyzer import (
    score_articles_batch,
    _score_single_batch,
    _is_retryable_error,
    _call_with_retry,
)


def _make_articles(n):
    """Helper: create n test articles with title_hash."""
    return [
        {
            'title': f'Article {i}',
            'description': f'Description for article {i}',
            'title_hash': f'hash_{i}',
        }
        for i in range(n)
    ]


def _mock_gemini_response(text):
    """Helper: mock Gemini response object — `.text` attr is all we need."""
    mock_resp = MagicMock()
    mock_resp.text = text
    return mock_resp


def _make_mock_client(side_effect=None, return_value=None):
    """Build a mock `google.genai.Client`-like object.

    Call pattern: `client.models.generate_content(model=..., contents=..., config=...)`.
    """
    client = MagicMock()
    if side_effect is not None:
        client.models.generate_content.side_effect = side_effect
    if return_value is not None:
        client.models.generate_content.return_value = return_value
    return client


class TestScoreArticlesBatch:

    @patch('src.analysis.gemini_news_analyzer._make_genai_client')
    def test_scores_batch_of_articles(self, mock_factory):
        """Scores a batch and returns {title_hash: score} mapping."""
        client = _make_mock_client(
            return_value=_mock_gemini_response(json.dumps([0.6, -0.3, 0.0])))
        mock_factory.return_value = client

        articles = _make_articles(3)
        result = score_articles_batch(articles)

        assert result == {'hash_0': 0.6, 'hash_1': -0.3, 'hash_2': 0.0}
        client.models.generate_content.assert_called_once()
        # Verify thinking_budget=0 is passed
        kwargs = client.models.generate_content.call_args.kwargs
        assert kwargs['model'] == 'gemini-2.5-flash'
        assert kwargs['config'].thinking_config.thinking_budget == 0

    def test_empty_articles_returns_empty(self):
        """Empty input returns empty dict without any API call."""
        result = score_articles_batch([])
        assert result == {}

    @patch('src.analysis.gemini_news_analyzer._make_genai_client',
           return_value=None)
    def test_no_credentials_returns_empty(self, mock_factory):
        """Returns empty dict when no credentials are available."""
        articles = _make_articles(3)
        result = score_articles_batch(articles)
        assert result == {}

    @patch('src.analysis.gemini_news_analyzer._make_genai_client')
    def test_clamps_out_of_range_scores(self, mock_factory):
        """Scores outside [-1, 1] are clamped to the valid range."""
        client = _make_mock_client(
            return_value=_mock_gemini_response(json.dumps([1.5, -2.0])))
        mock_factory.return_value = client

        result = score_articles_batch(_make_articles(2))

        assert result['hash_0'] == 1.0
        assert result['hash_1'] == -1.0

    @patch('src.analysis.gemini_news_analyzer._make_genai_client')
    def test_mismatched_count_discards_batch(self, mock_factory):
        """If Gemini returns wrong number of scores, batch is discarded."""
        client = _make_mock_client(
            return_value=_mock_gemini_response(json.dumps([0.5, -0.2])))
        mock_factory.return_value = client

        result = score_articles_batch(_make_articles(3))
        assert result == {}

    @patch('src.analysis.gemini_news_analyzer._make_genai_client')
    def test_gemini_failure_returns_empty(self, mock_factory):
        """API failure returns empty dict."""
        client = _make_mock_client(side_effect=Exception("API error"))
        mock_factory.return_value = client

        result = score_articles_batch(_make_articles(3))
        assert result == {}

    @patch('src.analysis.gemini_news_analyzer._make_genai_client')
    def test_multiple_batches(self, mock_factory):
        """Articles exceeding batch_size are split into multiple API calls."""
        client = _make_mock_client(side_effect=[
            _mock_gemini_response(json.dumps([0.5, -0.1])),
            _mock_gemini_response(json.dumps([0.3])),
        ])
        mock_factory.return_value = client

        result = score_articles_batch(_make_articles(3), batch_size=2)

        assert len(result) == 3
        assert result['hash_0'] == 0.5
        assert result['hash_2'] == 0.3
        assert client.models.generate_content.call_count == 2

    @patch('src.analysis.gemini_news_analyzer._make_genai_client')
    def test_partial_batch_failure(self, mock_factory):
        """If one batch fails, others still return scores."""
        client = _make_mock_client(side_effect=[
            Exception("API error"),
            _mock_gemini_response(json.dumps([0.3])),
        ])
        mock_factory.return_value = client

        result = score_articles_batch(_make_articles(3), batch_size=2)

        assert len(result) == 1
        assert result['hash_2'] == 0.3

    @patch('src.analysis.gemini_news_analyzer._make_genai_client')
    def test_json_with_code_fence(self, mock_factory):
        """Handles Gemini response wrapped in code fences."""
        client = _make_mock_client(
            return_value=_mock_gemini_response("```json\n[0.6, -0.3]\n```"))
        mock_factory.return_value = client

        result = score_articles_batch(_make_articles(2))
        assert result == {'hash_0': 0.6, 'hash_1': -0.3}


class TestScoreSingleBatch:

    def test_invalid_json_returns_none(self):
        """Non-JSON response returns None."""
        client = _make_mock_client(
            return_value=_mock_gemini_response("not json"))
        result = _score_single_batch(client, _make_articles(2))
        assert result is None

    def test_non_list_response_returns_none(self):
        """JSON object (not array) returns None."""
        client = _make_mock_client(
            return_value=_mock_gemini_response(json.dumps({"scores": [0.5]})))
        result = _score_single_batch(client, _make_articles(1))
        assert result is None

    def test_invalid_score_value_returns_none(self):
        """Non-numeric score in array returns None."""
        client = _make_mock_client(
            return_value=_mock_gemini_response(
                json.dumps([0.5, "not_a_number"])))
        result = _score_single_batch(client, _make_articles(2))
        assert result is None


class TestRetryLogic:

    def test_retryable_429(self):
        assert _is_retryable_error(Exception("429 Resource Exhausted")) is True

    def test_retryable_500(self):
        assert _is_retryable_error(Exception("500 Internal Server Error")) is True

    def test_retryable_503(self):
        assert _is_retryable_error(Exception("503 Service Unavailable")) is True

    def test_retryable_quota(self):
        assert _is_retryable_error(Exception("Quota exceeded for project")) is True

    def test_retryable_rate_limit(self):
        assert _is_retryable_error(Exception("rate limit exceeded")) is True

    def test_retryable_by_type_name(self):
        """Exceptions with known type names are retryable."""
        ResourceExhausted = type('ResourceExhausted', (Exception,), {})
        assert _is_retryable_error(ResourceExhausted("some error")) is True

    def test_not_retryable_generic(self):
        assert _is_retryable_error(Exception("API error")) is False

    def test_not_retryable_json(self):
        assert _is_retryable_error(json.JSONDecodeError("bad json", "", 0)) is False

    def test_not_retryable_value_error(self):
        assert _is_retryable_error(ValueError("invalid input")) is False

    @patch('src.analysis.gemini_news_analyzer.time.sleep')
    def test_retry_succeeds_after_transient_error(self, mock_sleep):
        """Retries on transient error and returns result on success."""
        fn = MagicMock(side_effect=[
            Exception("429 Resource Exhausted"),
            "success",
        ])
        result = _call_with_retry(fn, "arg1")
        assert result == "success"
        assert fn.call_count == 2
        mock_sleep.assert_called_once()

    @patch('src.analysis.gemini_news_analyzer.time.sleep')
    def test_retry_exhausted_raises(self, mock_sleep):
        """Raises after all retries are exhausted."""
        fn = MagicMock(side_effect=Exception("503 Service Unavailable"))
        with pytest.raises(Exception, match="503"):
            _call_with_retry(fn, "arg1")
        assert fn.call_count == 4
        assert mock_sleep.call_count == 3

    @patch('src.analysis.gemini_news_analyzer.time.sleep')
    def test_retry_exponential_backoff(self, mock_sleep):
        fn = MagicMock(side_effect=Exception("429 rate limit"))
        with pytest.raises(Exception):
            _call_with_retry(fn, "arg1")
        delays = [call.args[0] for call in mock_sleep.call_args_list]
        assert delays[0] < delays[1] < delays[2]

    def test_non_retryable_raises_immediately(self):
        fn = MagicMock(side_effect=ValueError("bad input"))
        with pytest.raises(ValueError, match="bad input"):
            _call_with_retry(fn, "arg1")
        assert fn.call_count == 1

    @patch('src.analysis.gemini_news_analyzer.time.sleep')
    def test_retry_passes_args_and_kwargs(self, mock_sleep):
        fn = MagicMock(side_effect=[
            Exception("500 Internal"),
            "result",
        ])
        result = _call_with_retry(fn, "a", "b", key="val")
        fn.assert_called_with("a", "b", key="val")
        assert result == "result"

    @patch('src.analysis.gemini_news_analyzer.time.sleep')
    @patch('src.analysis.gemini_news_analyzer._make_genai_client')
    def test_score_batch_retries_on_429(self, mock_factory, mock_sleep):
        """score_articles_batch retries on 429 and succeeds."""
        client = _make_mock_client(side_effect=[
            Exception("429 Resource Exhausted"),
            _mock_gemini_response(json.dumps([0.5, -0.2])),
        ])
        mock_factory.return_value = client

        result = score_articles_batch(_make_articles(2))

        assert result == {'hash_0': 0.5, 'hash_1': -0.2}
        assert client.models.generate_content.call_count == 2
        mock_sleep.assert_called_once()
