"""Tests for Gemini per-article sentiment scoring (score_articles_batch)."""

import json
import pytest
from unittest.mock import patch, MagicMock

from src.analysis.gemini_news_analyzer import (
    score_articles_batch,
    _score_single_batch,
    _is_retryable_error,
    _call_with_retry,
    clear_gemini_article_cache,
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
    """Helper: mock Gemini response object."""
    mock_resp = MagicMock()
    mock_resp.text = text
    return mock_resp


class TestScoreArticlesBatch:

    @patch('src.analysis.gemini_news_analyzer.vertexai')
    @patch('src.analysis.gemini_news_analyzer.GenerativeModel')
    @patch.dict('os.environ', {'GCP_PROJECT_ID': 'test-project'})
    def test_scores_batch_of_articles(self, mock_model_cls, mock_vertexai):
        """Scores a batch and returns {title_hash: score} mapping."""
        mock_model = MagicMock()
        mock_model_cls.return_value = mock_model
        mock_model.generate_content.return_value = _mock_gemini_response(
            json.dumps([0.6, -0.3, 0.0])
        )

        articles = _make_articles(3)
        result = score_articles_batch(articles)

        assert result == {'hash_0': 0.6, 'hash_1': -0.3, 'hash_2': 0.0}
        mock_model.generate_content.assert_called_once()

    def test_empty_articles_returns_empty(self):
        """Empty input returns empty dict without any API call."""
        result = score_articles_batch([])
        assert result == {}

    @patch.dict('os.environ', {}, clear=True)
    def test_no_gcp_project_returns_empty(self):
        """Returns empty dict when GCP_PROJECT_ID is not set."""
        articles = _make_articles(3)
        result = score_articles_batch(articles)
        assert result == {}

    @patch('src.analysis.gemini_news_analyzer.vertexai')
    @patch('src.analysis.gemini_news_analyzer.GenerativeModel')
    @patch.dict('os.environ', {'GCP_PROJECT_ID': 'test-project'})
    def test_clamps_out_of_range_scores(self, mock_model_cls, mock_vertexai):
        """Scores outside [-1, 1] are clamped to the valid range."""
        mock_model = MagicMock()
        mock_model_cls.return_value = mock_model
        mock_model.generate_content.return_value = _mock_gemini_response(
            json.dumps([1.5, -2.0])
        )

        articles = _make_articles(2)
        result = score_articles_batch(articles)

        assert result['hash_0'] == 1.0
        assert result['hash_1'] == -1.0

    @patch('src.analysis.gemini_news_analyzer.vertexai')
    @patch('src.analysis.gemini_news_analyzer.GenerativeModel')
    @patch.dict('os.environ', {'GCP_PROJECT_ID': 'test-project'})
    def test_mismatched_count_discards_batch(self, mock_model_cls, mock_vertexai):
        """If Gemini returns wrong number of scores, batch is discarded."""
        mock_model = MagicMock()
        mock_model_cls.return_value = mock_model
        # Return 2 scores for 3 articles
        mock_model.generate_content.return_value = _mock_gemini_response(
            json.dumps([0.5, -0.2])
        )

        articles = _make_articles(3)
        result = score_articles_batch(articles)

        assert result == {}

    @patch('src.analysis.gemini_news_analyzer.vertexai')
    @patch('src.analysis.gemini_news_analyzer.GenerativeModel')
    @patch.dict('os.environ', {'GCP_PROJECT_ID': 'test-project'})
    def test_gemini_failure_returns_empty(self, mock_model_cls, mock_vertexai):
        """API failure returns empty dict."""
        mock_model = MagicMock()
        mock_model_cls.return_value = mock_model
        mock_model.generate_content.side_effect = Exception("API error")

        articles = _make_articles(3)
        result = score_articles_batch(articles)

        assert result == {}

    @patch('src.analysis.gemini_news_analyzer.vertexai')
    @patch('src.analysis.gemini_news_analyzer.GenerativeModel')
    @patch.dict('os.environ', {'GCP_PROJECT_ID': 'test-project'})
    def test_multiple_batches(self, mock_model_cls, mock_vertexai):
        """Articles exceeding batch_size are split into multiple API calls."""
        mock_model = MagicMock()
        mock_model_cls.return_value = mock_model

        # First batch of 2, second batch of 1
        mock_model.generate_content.side_effect = [
            _mock_gemini_response(json.dumps([0.5, -0.1])),
            _mock_gemini_response(json.dumps([0.3])),
        ]

        articles = _make_articles(3)
        result = score_articles_batch(articles, batch_size=2)

        assert len(result) == 3
        assert result['hash_0'] == 0.5
        assert result['hash_2'] == 0.3
        assert mock_model.generate_content.call_count == 2

    @patch('src.analysis.gemini_news_analyzer.vertexai')
    @patch('src.analysis.gemini_news_analyzer.GenerativeModel')
    @patch.dict('os.environ', {'GCP_PROJECT_ID': 'test-project'})
    def test_partial_batch_failure(self, mock_model_cls, mock_vertexai):
        """If one batch fails, others still return scores."""
        mock_model = MagicMock()
        mock_model_cls.return_value = mock_model

        mock_model.generate_content.side_effect = [
            Exception("API error"),  # first batch fails
            _mock_gemini_response(json.dumps([0.3])),  # second succeeds
        ]

        articles = _make_articles(3)
        result = score_articles_batch(articles, batch_size=2)

        assert len(result) == 1
        assert result['hash_2'] == 0.3

    @patch('src.analysis.gemini_news_analyzer.vertexai')
    @patch('src.analysis.gemini_news_analyzer.GenerativeModel')
    @patch.dict('os.environ', {'GCP_PROJECT_ID': 'test-project'})
    def test_json_with_code_fence(self, mock_model_cls, mock_vertexai):
        """Handles Gemini response wrapped in code fences."""
        mock_model = MagicMock()
        mock_model_cls.return_value = mock_model
        mock_model.generate_content.return_value = _mock_gemini_response(
            "```json\n[0.6, -0.3]\n```"
        )

        articles = _make_articles(2)
        result = score_articles_batch(articles)

        assert result == {'hash_0': 0.6, 'hash_1': -0.3}


class TestScoreSingleBatch:

    def test_invalid_json_returns_none(self):
        """Non-JSON response returns None."""
        mock_model = MagicMock()
        mock_model.generate_content.return_value = _mock_gemini_response("not json")

        result = _score_single_batch(mock_model, _make_articles(2))
        assert result is None

    def test_non_list_response_returns_none(self):
        """JSON object (not array) returns None."""
        mock_model = MagicMock()
        mock_model.generate_content.return_value = _mock_gemini_response(
            json.dumps({"scores": [0.5]})
        )

        result = _score_single_batch(mock_model, _make_articles(1))
        assert result is None

    def test_invalid_score_value_returns_none(self):
        """Non-numeric score in array returns None."""
        mock_model = MagicMock()
        mock_model.generate_content.return_value = _mock_gemini_response(
            json.dumps([0.5, "not_a_number"])
        )

        result = _score_single_batch(mock_model, _make_articles(2))
        assert result is None


class TestRetryLogic:

    def test_retryable_429(self):
        exc = Exception("429 Resource Exhausted")
        assert _is_retryable_error(exc) is True

    def test_retryable_500(self):
        exc = Exception("500 Internal Server Error")
        assert _is_retryable_error(exc) is True

    def test_retryable_503(self):
        exc = Exception("503 Service Unavailable")
        assert _is_retryable_error(exc) is True

    def test_retryable_quota(self):
        exc = Exception("Quota exceeded for project")
        assert _is_retryable_error(exc) is True

    def test_retryable_rate_limit(self):
        exc = Exception("rate limit exceeded")
        assert _is_retryable_error(exc) is True

    def test_retryable_by_type_name(self):
        """Exceptions with known type names are retryable."""
        ResourceExhausted = type('ResourceExhausted', (Exception,), {})
        exc = ResourceExhausted("some error")
        assert _is_retryable_error(exc) is True

    def test_not_retryable_generic(self):
        exc = Exception("API error")
        assert _is_retryable_error(exc) is False

    def test_not_retryable_json(self):
        exc = json.JSONDecodeError("bad json", "", 0)
        assert _is_retryable_error(exc) is False

    def test_not_retryable_value_error(self):
        exc = ValueError("invalid input")
        assert _is_retryable_error(exc) is False

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
        assert fn.call_count == 4  # 1 initial + 3 retries
        assert mock_sleep.call_count == 3

    @patch('src.analysis.gemini_news_analyzer.time.sleep')
    def test_retry_exponential_backoff(self, mock_sleep):
        """Delay increases exponentially between retries."""
        fn = MagicMock(side_effect=Exception("429 rate limit"))
        with pytest.raises(Exception):
            _call_with_retry(fn, "arg1")
        delays = [call.args[0] for call in mock_sleep.call_args_list]
        # Base delay 2.0: attempt 0 → 2*1+jitter, attempt 1 → 2*2+jitter, attempt 2 → 2*4+jitter
        assert delays[0] < delays[1] < delays[2]

    def test_non_retryable_raises_immediately(self):
        """Non-retryable errors raise without any retry."""
        fn = MagicMock(side_effect=ValueError("bad input"))
        with pytest.raises(ValueError, match="bad input"):
            _call_with_retry(fn, "arg1")
        assert fn.call_count == 1

    @patch('src.analysis.gemini_news_analyzer.time.sleep')
    def test_retry_passes_args_and_kwargs(self, mock_sleep):
        """Arguments and keyword arguments are forwarded correctly."""
        fn = MagicMock(side_effect=[
            Exception("500 Internal"),
            "result",
        ])
        result = _call_with_retry(fn, "a", "b", key="val")
        fn.assert_called_with("a", "b", key="val")
        assert result == "result"

    @patch('src.analysis.gemini_news_analyzer.time.sleep')
    @patch('src.analysis.gemini_news_analyzer.vertexai')
    @patch('src.analysis.gemini_news_analyzer.GenerativeModel')
    @patch.dict('os.environ', {'GCP_PROJECT_ID': 'test-project'})
    def test_score_batch_retries_on_429(self, mock_model_cls, mock_vertexai, mock_sleep):
        """score_articles_batch retries on 429 and succeeds."""
        mock_model = MagicMock()
        mock_model_cls.return_value = mock_model
        mock_model.generate_content.side_effect = [
            Exception("429 Resource Exhausted"),
            _mock_gemini_response(json.dumps([0.5, -0.2])),
        ]

        articles = _make_articles(2)
        result = score_articles_batch(articles)

        assert result == {'hash_0': 0.5, 'hash_1': -0.2}
        assert mock_model.generate_content.call_count == 2
        mock_sleep.assert_called_once()
