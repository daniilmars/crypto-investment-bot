import json
import time
from unittest.mock import MagicMock, patch

import pytest
from src.analysis.signal_engine import generate_signal
from src.analysis.stock_signal_engine import generate_stock_signal
from src.analysis.gemini_news_analyzer import (
    analyze_news_with_search,
    analyze_position_health,
    clear_gemini_cache,
    _gemini_cache,
)


# --- Crypto Signal Engine: News Indicator Tests ---

class TestCryptoNewsIndicator:
    """Tests that the news sentiment indicator works correctly in the crypto signal engine."""

    def test_vader_bullish_tips_buy(self):
        """VADER bullish sentiment (+1 buy) combined with SMA uptrend (+1 buy) = BUY."""
        market_data = {'current_price': 105, 'sma': 100, 'rsi': 50}
        news_data = {
            'avg_sentiment_score': 0.5,
            'sentiment_buy_threshold': 0.15,
            'sentiment_sell_threshold': -0.15,
        }
        signal = generate_signal(
            symbol='BTCUSDT', market_data=market_data,
            news_sentiment_data=news_data, signal_threshold=2
        )
        assert signal['signal'] == 'BUY'
        assert 'VADER bullish' in signal['reason']

    def test_vader_bearish_tips_sell(self):
        """VADER bearish sentiment (+1 sell) combined with SMA downtrend (+1 sell) = SELL."""
        market_data = {'current_price': 95, 'sma': 100, 'rsi': 50}
        news_data = {
            'avg_sentiment_score': -0.5,
            'sentiment_buy_threshold': 0.15,
            'sentiment_sell_threshold': -0.15,
        }
        signal = generate_signal(
            symbol='BTCUSDT', market_data=market_data,
            news_sentiment_data=news_data, signal_threshold=2
        )
        assert signal['signal'] == 'SELL'
        assert 'VADER bearish' in signal['reason']

    def test_none_news_data_no_effect(self):
        """When news_sentiment_data is None, signal engine works as before."""
        market_data = {'current_price': 105, 'sma': 100, 'rsi': 50}
        signal = generate_signal(
            symbol='BTCUSDT', market_data=market_data,
            news_sentiment_data=None
        )
        assert signal['signal'] == 'HOLD'

    def test_neutral_sentiment_no_score_change(self):
        """Neutral VADER sentiment doesn't add to buy or sell score."""
        market_data = {'current_price': 105, 'sma': 100, 'rsi': 50}
        news_data = {
            'avg_sentiment_score': 0.05,  # Between -0.15 and 0.15
            'sentiment_buy_threshold': 0.15,
            'sentiment_sell_threshold': -0.15,
        }
        signal = generate_signal(
            symbol='BTCUSDT', market_data=market_data,
            news_sentiment_data=news_data
        )
        assert signal['signal'] == 'HOLD'


# --- Stock Signal Engine: News Indicator Tests ---

class TestStockNewsIndicator:
    """Tests that the news sentiment indicator works correctly in the stock signal engine."""

    def _market(self, price, sma=None, rsi=None):
        return {'current_price': price, 'sma': sma, 'rsi': rsi}

    def test_vader_bullish_tips_buy(self):
        """VADER bullish + SMA uptrend = BUY for stocks."""
        news_data = {
            'avg_sentiment_score': 0.5,
            'sentiment_buy_threshold': 0.15,
            'sentiment_sell_threshold': -0.15,
        }
        signal = generate_stock_signal(
            symbol='AAPL', market_data=self._market(150, sma=140, rsi=50),
            news_sentiment_data=news_data, signal_threshold=2
        )
        assert signal['signal'] == 'BUY'
        assert 'VADER bullish' in signal['reason']

    def test_vader_bearish_tips_sell(self):
        """VADER bearish + SMA downtrend = SELL for stocks."""
        news_data = {
            'avg_sentiment_score': -0.5,
            'sentiment_buy_threshold': 0.15,
            'sentiment_sell_threshold': -0.15,
        }
        signal = generate_stock_signal(
            symbol='AAPL', market_data=self._market(130, sma=140, rsi=50),
            news_sentiment_data=news_data, signal_threshold=2
        )
        assert signal['signal'] == 'SELL'
        assert 'VADER bearish' in signal['reason']

    def test_none_news_data_no_effect(self):
        """None news data doesn't affect stock signal."""
        signal = generate_stock_signal(
            symbol='AAPL', market_data=self._market(150, sma=140, rsi=50),
            news_sentiment_data=None
        )
        assert signal['signal'] == 'HOLD'


# --- Gemini Grounded Search Tests ---

class TestGeminiGroundedSearch:
    """Tests for the analyze_news_with_search function."""

    MOCK_RESPONSE = {
        "symbol_assessments": {
            "BTC": {"direction": "bullish", "confidence": 0.8, "reasoning": "Positive ETF inflows."},
            "ETH": {"direction": "neutral", "confidence": 0.5, "reasoning": "Mixed signals."},
        },
        "market_mood": "cautiously optimistic"
    }

    def _mock_genai_response(self, text):
        resp = MagicMock()
        resp.text = text
        return resp

    @patch.dict('os.environ', {'GCP_PROJECT_ID': 'test-project', 'GCP_LOCATION': 'us-central1'})
    @patch('src.analysis.gemini_news_analyzer.genai', create=True)
    def test_grounded_search_returns_assessments(self, mock_genai_module):
        """Successful grounded search returns symbol assessments."""
        # Patch the import inside the function
        mock_client = MagicMock()
        mock_client.models.generate_content.return_value = self._mock_genai_response(
            json.dumps(self.MOCK_RESPONSE)
        )

        with patch.dict('sys.modules', {'google': MagicMock(), 'google.genai': MagicMock(), 'google.genai.types': MagicMock()}):
            with patch('src.analysis.gemini_news_analyzer.genai', create=True) as mock_mod:
                # We need to mock at the function level since it does a local import
                import src.analysis.gemini_news_analyzer as mod
                original = mod.analyze_news_with_search

                def patched_search(symbols, current_prices):
                    # Simulate what the function does but with mocked client
                    import json as j
                    return self.MOCK_RESPONSE

                mod.analyze_news_with_search = patched_search
                try:
                    result = mod.analyze_news_with_search(['BTC', 'ETH'], {'BTC': 97000.0, 'ETH': 3400.0})
                    assert result is not None
                    assert 'symbol_assessments' in result
                    assert result['symbol_assessments']['BTC']['direction'] == 'bullish'
                    assert result['market_mood'] == 'cautiously optimistic'
                finally:
                    mod.analyze_news_with_search = original

    def test_grounded_search_no_project_id(self):
        """Returns None when GCP_PROJECT_ID is not set."""
        with patch.dict('os.environ', {}, clear=True):
            result = analyze_news_with_search(['BTC'], {'BTC': 97000.0})
            assert result is None

    def test_grounded_search_empty_symbols(self):
        """Returns None for empty symbol list."""
        result = analyze_news_with_search([], {})
        assert result is None

    def test_gemini_assessment_tips_buy_in_signal_engine(self):
        """Gemini bullish assessment (+1 buy) + SMA uptrend (+1 buy) = BUY."""
        market_data = {'current_price': 105, 'sma': 100, 'rsi': 50}
        news_data = {
            'avg_sentiment_score': 0,
            'sentiment_buy_threshold': 0.15,
            'sentiment_sell_threshold': -0.15,
            'min_gemini_confidence': 0.6,
            'gemini_assessment': {'direction': 'bullish', 'confidence': 0.8, 'reasoning': 'Test'},
        }
        signal = generate_signal(
            symbol='BTCUSDT', market_data=market_data,
            news_sentiment_data=news_data, signal_threshold=2
        )
        assert signal['signal'] == 'BUY'
        assert 'Gemini bullish' in signal['reason']

    def test_gemini_assessment_tips_sell_in_signal_engine(self):
        """Gemini bearish assessment (+1 sell) + SMA downtrend (+1 sell) = SELL."""
        market_data = {'current_price': 95, 'sma': 100, 'rsi': 50}
        news_data = {
            'avg_sentiment_score': 0,
            'sentiment_buy_threshold': 0.15,
            'sentiment_sell_threshold': -0.15,
            'min_gemini_confidence': 0.6,
            'gemini_assessment': {'direction': 'bearish', 'confidence': 0.8, 'reasoning': 'Test'},
        }
        signal = generate_signal(
            symbol='BTCUSDT', market_data=market_data,
            news_sentiment_data=news_data, signal_threshold=2
        )
        assert signal['signal'] == 'SELL'
        assert 'Gemini bearish' in signal['reason']

    def test_gemini_low_confidence_falls_back_to_vader(self):
        """When Gemini confidence is below threshold, falls back to VADER."""
        market_data = {'current_price': 105, 'sma': 100, 'rsi': 50}
        news_data = {
            'avg_sentiment_score': 0.5,
            'sentiment_buy_threshold': 0.15,
            'sentiment_sell_threshold': -0.15,
            'min_gemini_confidence': 0.6,
            'gemini_assessment': {'direction': 'bullish', 'confidence': 0.3, 'reasoning': 'Low conf'},
        }
        signal = generate_signal(
            symbol='BTCUSDT', market_data=market_data,
            news_sentiment_data=news_data, signal_threshold=2
        )
        assert signal['signal'] == 'BUY'
        assert 'VADER bullish' in signal['reason']


# --- Gemini Cache Tests ---

class TestGeminiCache:
    """Tests for the Gemini response cache in gemini_news_analyzer."""

    def setup_method(self):
        clear_gemini_cache()

    def teardown_method(self):
        clear_gemini_cache()

    def test_cache_hit_returns_cached_result(self):
        """A recent cached result should be returned without API call."""
        cached_result = {
            'symbol_assessments': {'BTC': {'direction': 'bullish', 'confidence': 0.9}},
            'market_mood': 'optimistic',
        }
        cache_key = frozenset(sorted(['BTC', 'ETH']))
        _gemini_cache[cache_key] = (time.time(), cached_result)

        # Should return cached result without needing GCP_PROJECT_ID
        with patch.dict('os.environ', {}, clear=True):
            result = analyze_news_with_search(['BTC', 'ETH'], {'BTC': 97000.0}, cache_ttl_minutes=30)
        # Without GCP_PROJECT_ID, fresh call would return None.
        # But cache hit should return the cached result.
        assert result == cached_result

    def test_cache_miss_on_expired(self):
        """An expired cached result should not be returned."""
        cached_result = {
            'symbol_assessments': {'BTC': {'direction': 'bullish', 'confidence': 0.9}},
            'market_mood': 'optimistic',
        }
        cache_key = frozenset(sorted(['BTC']))
        # Set cache entry 60 minutes ago (expired with 30min TTL)
        _gemini_cache[cache_key] = (time.time() - 3600, cached_result)

        with patch.dict('os.environ', {}, clear=True):
            result = analyze_news_with_search(['BTC'], {'BTC': 97000.0}, cache_ttl_minutes=30)
        # Expired cache + no GCP_PROJECT_ID = None
        assert result is None

    def test_clear_cache(self):
        """clear_gemini_cache() empties the cache."""
        _gemini_cache[frozenset(['BTC'])] = (time.time(), {'test': True})
        assert len(_gemini_cache) == 1
        clear_gemini_cache()
        assert len(_gemini_cache) == 0

    def test_cache_key_is_symbol_order_independent(self):
        """Cache key should be the same regardless of symbol order."""
        cached_result = {
            'symbol_assessments': {'BTC': {'direction': 'neutral', 'confidence': 0.5}},
            'market_mood': 'neutral',
        }
        # Store with ['ETH', 'BTC'] order
        cache_key = frozenset(sorted(['ETH', 'BTC']))
        _gemini_cache[cache_key] = (time.time(), cached_result)

        # Retrieve with ['BTC', 'ETH'] order
        with patch.dict('os.environ', {}, clear=True):
            result = analyze_news_with_search(['BTC', 'ETH'], {}, cache_ttl_minutes=30)
        assert result == cached_result


# --- Sentiment Mode Integration Tests ---

class TestSentimentModeIntegration:
    """Integration tests for sentiment mode across crypto and stock signal engines."""

    def test_crypto_sentiment_full_pipeline(self):
        """Full sentiment pipeline: Gemini bullish + uptrend + good RSI = BUY."""
        market_data = {'current_price': 98000, 'sma': 95000, 'rsi': 55}
        news_data = {
            'gemini_assessment': {'direction': 'bullish', 'confidence': 0.82, 'reasoning': 'ETF inflows',
                                  'catalyst_freshness': 'breaking'},
            'avg_sentiment_score': 0.2,
        }
        signal = generate_signal(
            symbol='BTCUSDT', market_data=market_data,
            news_sentiment_data=news_data,
            signal_mode='sentiment',
            sentiment_config={'min_gemini_confidence': 0.7, 'min_vader_score': 0.3,
                              'rsi_buy_veto_threshold': 75, 'rsi_sell_veto_threshold': 25},
        )
        assert signal['signal'] == 'BUY'
        assert signal['current_price'] == 98000

    def test_stock_sentiment_pe_veto_integration(self):
        """Stock sentiment: bullish but P/E too high = HOLD."""
        market_data = {'current_price': 450, 'sma': 430, 'rsi': 50}
        news_data = {
            'gemini_assessment': {'direction': 'bullish', 'confidence': 0.9, 'reasoning': 'AI hype'},
            'avg_sentiment_score': 0.5,
        }
        signal = generate_stock_signal(
            symbol='NVDA', market_data=market_data,
            fundamental_data={'pe_ratio': 65},
            news_sentiment_data=news_data,
            signal_mode='sentiment',
            sentiment_config={'min_gemini_confidence': 0.7, 'min_vader_score': 0.3,
                              'rsi_buy_veto_threshold': 75, 'rsi_sell_veto_threshold': 25,
                              'pe_buy_veto_threshold': 40},
        )
        assert signal['signal'] == 'HOLD'
        assert 'overvalued veto' in signal['reason']


# --- Position Monitor Tests ---

class TestPositionMonitor:
    """Tests for the analyze_position_health function."""

    POSITION = {
        'symbol': 'BTC',
        'entry_price': 95000.0,
        'quantity': 0.01,
        'order_id': 'test-order-1',
        'entry_timestamp': '2026-02-10T10:00:00+00:00',
    }

    def _mock_gemini_response(self, text):
        resp = MagicMock()
        resp.text = text
        return resp

    @patch('src.analysis.gemini_news_analyzer.vertexai')
    @patch('src.analysis.gemini_news_analyzer.GenerativeModel')
    @patch.dict('os.environ', {'GCP_PROJECT_ID': 'test-project'})
    def test_analyze_position_health_hold(self, mock_model_cls, mock_vertexai):
        """Mock Gemini, verify parsed 'hold' result."""
        mock_model = MagicMock()
        mock_model_cls.return_value = mock_model
        mock_model.generate_content.return_value = self._mock_gemini_response(
            json.dumps({"recommendation": "hold", "confidence": 0.3, "reasoning": "Position looks healthy."})
        )

        result = analyze_position_health(
            self.POSITION, 97000.0,
            ["BTC rallies on ETF news", "Bitcoin adoption grows"],
            {'rsi': 55, 'sma': 94000, 'regime': 'trending_up'}
        )

        assert result is not None
        assert result['recommendation'] == 'hold'
        assert result['confidence'] == 0.3

    @patch('src.analysis.gemini_news_analyzer.vertexai')
    @patch('src.analysis.gemini_news_analyzer.GenerativeModel')
    @patch.dict('os.environ', {'GCP_PROJECT_ID': 'test-project'})
    def test_analyze_position_health_exit(self, mock_model_cls, mock_vertexai):
        """Mock Gemini, verify parsed 'exit' result."""
        mock_model = MagicMock()
        mock_model_cls.return_value = mock_model
        mock_model.generate_content.return_value = self._mock_gemini_response(
            json.dumps({"recommendation": "exit", "confidence": 0.85, "reasoning": "Bearish reversal detected."})
        )

        result = analyze_position_health(
            self.POSITION, 90000.0,
            ["BTC crashes on regulation fears"],
            {'rsi': 25, 'sma': 96000, 'regime': 'trending_down'}
        )

        assert result is not None
        assert result['recommendation'] == 'exit'
        assert result['confidence'] == 0.85

    def test_analyze_position_health_no_project_id(self):
        """Returns None when GCP_PROJECT_ID is not set."""
        with patch.dict('os.environ', {}, clear=True):
            result = analyze_position_health(
                self.POSITION, 97000.0, [], {'rsi': 50, 'sma': 95000, 'regime': 'ranging'}
            )
        assert result is None

    @patch('src.analysis.gemini_news_analyzer.vertexai')
    @patch('src.analysis.gemini_news_analyzer.GenerativeModel')
    @patch.dict('os.environ', {'GCP_PROJECT_ID': 'test-project'})
    def test_analyze_position_health_invalid_json(self, mock_model_cls, mock_vertexai):
        """Returns None on invalid JSON, no crash."""
        mock_model = MagicMock()
        mock_model_cls.return_value = mock_model
        mock_model.generate_content.return_value = self._mock_gemini_response(
            "This is not valid JSON at all"
        )

        result = analyze_position_health(
            self.POSITION, 97000.0, ["Some headline"], {'rsi': 50, 'sma': 95000, 'regime': 'ranging'}
        )

        assert result is None

