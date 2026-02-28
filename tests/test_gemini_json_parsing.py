"""Tests for Gemini JSON parsing hardening (W2)."""

import json
import logging

import pytest

from src.analysis.gemini_news_analyzer import _parse_gemini_json, _validate_gemini_response


class TestParseGeminiJson:

    def test_plain_json(self):
        """Plain JSON string without fences parses correctly."""
        text = '{"symbol_assessments": {}, "market_mood": "neutral"}'
        result = _parse_gemini_json(text)
        assert result == {"symbol_assessments": {}, "market_mood": "neutral"}

    def test_fenced_json_with_lang(self):
        """```json ... ``` fences are stripped correctly."""
        text = '```json\n{"key": "value"}\n```'
        result = _parse_gemini_json(text)
        assert result == {"key": "value"}

    def test_fenced_no_lang(self):
        """``` ... ``` fences (no language tag) are stripped correctly."""
        text = '```\n{"key": "value"}\n```'
        result = _parse_gemini_json(text)
        assert result == {"key": "value"}

    def test_invalid_json_raises(self):
        """Invalid JSON raises json.JSONDecodeError."""
        with pytest.raises(json.JSONDecodeError):
            _parse_gemini_json("not valid json at all")

    def test_fenced_with_trailing_whitespace(self):
        """Fenced JSON with extra whitespace and newlines."""
        text = '```json\n\n  {"a": 1}  \n\n```\n  '
        result = _parse_gemini_json(text)
        assert result == {"a": 1}


class TestValidateGeminiResponse:

    def test_missing_keys_warns(self, caplog):
        """Missing required keys produce a warning log."""
        result = {"market_mood": "bullish"}
        with caplog.at_level(logging.WARNING):
            out = _validate_gemini_response(result, ['symbol_assessments', 'market_mood'], 'test')
        assert out is result  # returned unchanged
        assert "missing keys" in caplog.text
        assert "symbol_assessments" in caplog.text

    def test_complete_keys_no_warning(self, caplog):
        """All required keys present produces no warning."""
        result = {"symbol_assessments": {}, "market_mood": "neutral"}
        with caplog.at_level(logging.WARNING):
            out = _validate_gemini_response(result, ['symbol_assessments', 'market_mood'], 'test')
        assert out is result
        assert "missing keys" not in caplog.text
