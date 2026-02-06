import json
import pytest
from unittest.mock import patch, MagicMock

from src.analysis.news_impact_analyzer import (
    _get_client,
    _build_prompt,
    analyze_news_impact,
)


class TestGetClient:
    @patch('src.analysis.news_impact_analyzer.app_config', {'api_keys': {'anthropic': None}})
    def test_missing_key_returns_none(self):
        assert _get_client() is None

    @patch('src.analysis.news_impact_analyzer.app_config', {'api_keys': {'anthropic': 'YOUR_ANTHROPIC_API_KEY'}})
    def test_placeholder_key_returns_none(self):
        assert _get_client() is None


class TestBuildPrompt:
    def test_prompt_contains_symbols_and_headlines(self):
        headlines = {'BTC': ['Bitcoin surges', 'BTC at new high']}
        prices = {'BTC': 50000.0}
        prompt = _build_prompt(headlines, prices)
        assert 'BTC' in prompt
        assert 'Bitcoin surges' in prompt
        assert '50000' in prompt
        assert 'JSON' in prompt

    def test_prompt_multiple_symbols(self):
        headlines = {
            'BTC': ['Bitcoin up'],
            'ETH': ['Ethereum rally'],
        }
        prices = {'BTC': 50000, 'ETH': 3000}
        prompt = _build_prompt(headlines, prices)
        assert 'BTC' in prompt
        assert 'ETH' in prompt
        assert 'Ethereum rally' in prompt


class TestAnalyzeNewsImpact:
    def test_empty_headlines_returns_none(self):
        assert analyze_news_impact({}, {}) is None

    @patch('src.analysis.news_impact_analyzer._get_client', return_value=None)
    def test_no_client_returns_none(self, mock_client):
        result = analyze_news_impact({'BTC': ['headline']}, {'BTC': 50000})
        assert result is None

    @patch('src.analysis.news_impact_analyzer._get_client')
    def test_valid_response_parsed(self, mock_get_client):
        mock_client = MagicMock()
        mock_get_client.return_value = mock_client

        valid_response = {
            'symbol_assessments': {
                'BTC': {
                    'direction': 'bullish',
                    'confidence': 0.8,
                    'reasoning': 'Positive news flow',
                }
            },
            'market_mood': 'Optimistic',
        }

        mock_message = MagicMock()
        mock_message.content = [MagicMock(text=json.dumps(valid_response))]
        mock_client.messages.create.return_value = mock_message

        result = analyze_news_impact({'BTC': ['BTC surges']}, {'BTC': 50000})
        assert result is not None
        assert result['symbol_assessments']['BTC']['direction'] == 'bullish'
        assert result['symbol_assessments']['BTC']['confidence'] == 0.8
        assert result['market_mood'] == 'Optimistic'

    @patch('src.analysis.news_impact_analyzer._get_client')
    def test_malformed_json_returns_none(self, mock_get_client):
        mock_client = MagicMock()
        mock_get_client.return_value = mock_client

        mock_message = MagicMock()
        mock_message.content = [MagicMock(text='not valid json {{{')]
        mock_client.messages.create.return_value = mock_message

        result = analyze_news_impact({'BTC': ['headline']}, {'BTC': 50000})
        assert result is None

    @patch('src.analysis.news_impact_analyzer._get_client')
    def test_missing_assessments_key_returns_none(self, mock_get_client):
        mock_client = MagicMock()
        mock_get_client.return_value = mock_client

        mock_message = MagicMock()
        mock_message.content = [MagicMock(text=json.dumps({'market_mood': 'ok'}))]
        mock_client.messages.create.return_value = mock_message

        result = analyze_news_impact({'BTC': ['headline']}, {'BTC': 50000})
        assert result is None

    @patch('src.analysis.news_impact_analyzer._get_client')
    def test_api_error_returns_none(self, mock_get_client):
        mock_client = MagicMock()
        mock_get_client.return_value = mock_client
        mock_client.messages.create.side_effect = Exception('API timeout')

        result = analyze_news_impact({'BTC': ['headline']}, {'BTC': 50000})
        assert result is None
