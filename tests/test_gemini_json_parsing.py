"""Tests for Gemini JSON parsing hardening (W2)."""

import json
import logging

import pytest

from src.analysis.gemini_news_analyzer import (
    _extract_first_json_object,
    _parse_gemini_json,
    _validate_gemini_response,
)


class TestExtractFirstJsonObject:
    """Tests for _extract_first_json_object helper."""

    def test_clean_json(self):
        text = '{"key": "value"}'
        assert _extract_first_json_object(text) == text

    def test_extra_data_after_json(self):
        text = '{"key": "value"}\n\nHere is some commentary from Gemini.'
        assert _extract_first_json_object(text) == '{"key": "value"}'

    def test_two_json_objects(self):
        text = '{"first": 1}\n{"second": 2}'
        assert _extract_first_json_object(text) == '{"first": 1}'

    def test_nested_braces(self):
        text = '{"a": {"b": {"c": 1}}} extra'
        assert _extract_first_json_object(text) == '{"a": {"b": {"c": 1}}}'

    def test_braces_in_strings(self):
        text = '{"msg": "use {braces} here"} trailing'
        assert _extract_first_json_object(text) == '{"msg": "use {braces} here"}'

    def test_escaped_quotes_in_strings(self):
        text = '{"msg": "say \\"hello\\""} extra'
        assert _extract_first_json_object(text) == '{"msg": "say \\"hello\\""}'

    def test_no_json_object(self):
        text = 'no json here'
        assert _extract_first_json_object(text) == text

    def test_leading_text_before_json(self):
        text = 'Here is the result:\n{"key": "value"} done'
        assert _extract_first_json_object(text) == '{"key": "value"}'


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

    def test_extra_data_after_json(self):
        """JSON followed by extra text (Gemini commentary) is handled."""
        text = '{"symbol_assessments": {}, "market_mood": "neutral"}\n\nNote: analysis above...'
        result = _parse_gemini_json(text)
        assert result == {"symbol_assessments": {}, "market_mood": "neutral"}

    def test_duplicate_json_objects(self):
        """Two concatenated JSON objects — first one is used."""
        text = '{"first": true}\n{"second": true}'
        result = _parse_gemini_json(text)
        assert result == {"first": True}


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
