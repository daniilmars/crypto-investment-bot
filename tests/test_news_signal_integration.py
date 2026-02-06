import pytest
from src.analysis.signal_engine import generate_signal
from src.analysis.stock_signal_engine import generate_stock_signal


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
            symbol='BTCUSDT', whale_transactions=[], market_data=market_data,
            news_sentiment_data=news_data
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
            symbol='BTCUSDT', whale_transactions=[], market_data=market_data,
            news_sentiment_data=news_data
        )
        assert signal['signal'] == 'SELL'
        assert 'VADER bearish' in signal['reason']

    def test_claude_bullish_overrides_vader(self):
        """Claude bullish with high confidence overrides VADER scoring."""
        market_data = {'current_price': 105, 'sma': 100, 'rsi': 50}
        news_data = {
            'avg_sentiment_score': -0.3,  # VADER is bearish
            'claude_assessment': {
                'direction': 'bullish',
                'confidence': 0.8,
            },
            'min_claude_confidence': 0.6,
        }
        signal = generate_signal(
            symbol='BTCUSDT', whale_transactions=[], market_data=market_data,
            news_sentiment_data=news_data
        )
        assert signal['signal'] == 'BUY'
        assert 'Claude bullish' in signal['reason']

    def test_claude_bearish_tips_sell(self):
        """Claude bearish with high confidence tips sell signal."""
        market_data = {'current_price': 95, 'sma': 100, 'rsi': 50}
        news_data = {
            'avg_sentiment_score': 0.3,  # VADER is bullish
            'claude_assessment': {
                'direction': 'bearish',
                'confidence': 0.9,
            },
            'min_claude_confidence': 0.6,
        }
        signal = generate_signal(
            symbol='BTCUSDT', whale_transactions=[], market_data=market_data,
            news_sentiment_data=news_data
        )
        assert signal['signal'] == 'SELL'
        assert 'Claude bearish' in signal['reason']

    def test_low_confidence_claude_ignored_uses_vader(self):
        """When Claude confidence is below threshold, falls back to VADER."""
        market_data = {'current_price': 105, 'sma': 100, 'rsi': 50}
        news_data = {
            'avg_sentiment_score': 0.5,
            'sentiment_buy_threshold': 0.15,
            'sentiment_sell_threshold': -0.15,
            'claude_assessment': {
                'direction': 'bearish',  # Claude says bearish but low confidence
                'confidence': 0.3,
            },
            'min_claude_confidence': 0.6,
        }
        signal = generate_signal(
            symbol='BTCUSDT', whale_transactions=[], market_data=market_data,
            news_sentiment_data=news_data
        )
        assert signal['signal'] == 'BUY'
        assert 'VADER bullish' in signal['reason']

    def test_none_news_data_no_effect(self):
        """When news_sentiment_data is None, signal engine works as before."""
        market_data = {'current_price': 105, 'sma': 100, 'rsi': 50}
        signal = generate_signal(
            symbol='BTCUSDT', whale_transactions=[], market_data=market_data,
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
            symbol='BTCUSDT', whale_transactions=[], market_data=market_data,
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
            news_sentiment_data=news_data
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
            news_sentiment_data=news_data
        )
        assert signal['signal'] == 'SELL'
        assert 'VADER bearish' in signal['reason']

    def test_claude_bullish_high_confidence(self):
        """Claude bullish + SMA uptrend = BUY for stocks."""
        news_data = {
            'avg_sentiment_score': 0.0,
            'claude_assessment': {
                'direction': 'bullish',
                'confidence': 0.8,
            },
            'min_claude_confidence': 0.6,
        }
        signal = generate_stock_signal(
            symbol='AAPL', market_data=self._market(150, sma=140, rsi=50),
            news_sentiment_data=news_data
        )
        assert signal['signal'] == 'BUY'
        assert 'Claude bullish' in signal['reason']

    def test_none_news_data_no_effect(self):
        """None news data doesn't affect stock signal."""
        signal = generate_stock_signal(
            symbol='AAPL', market_data=self._market(150, sma=140, rsi=50),
            news_sentiment_data=None
        )
        assert signal['signal'] == 'HOLD'

    def test_low_confidence_claude_falls_back_to_vader(self):
        """Low confidence Claude falls back to VADER for stocks."""
        news_data = {
            'avg_sentiment_score': -0.4,
            'sentiment_buy_threshold': 0.15,
            'sentiment_sell_threshold': -0.15,
            'claude_assessment': {
                'direction': 'bullish',
                'confidence': 0.2,
            },
            'min_claude_confidence': 0.6,
        }
        signal = generate_stock_signal(
            symbol='AAPL', market_data=self._market(130, sma=140, rsi=50),
            news_sentiment_data=news_data
        )
        assert signal['signal'] == 'SELL'
        assert 'VADER bearish' in signal['reason']
