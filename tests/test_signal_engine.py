# tests/test_signal_engine.py

import pytest
from src.analysis.signal_engine import generate_signal

# --- Test Cases for the `generate_signal` Function ---

def test_strong_buy_signal():
    """
    Test Case: Conditions align for a strong 'BUY' signal.
    - Uptrend (Price > SMA) -> +1 buy_score
    - Oversold (RSI < 30) -> +1 buy_score
    Total buy_score = 2, which is >= 2.
    """
    market_data = {'current_price': 105, 'sma': 100, 'rsi': 25}
    signal = generate_signal(symbol='BTCUSDT', market_data=market_data, signal_threshold=2)
    assert signal['signal'] == 'BUY'

def test_strong_sell_signal():
    """
    Test Case: Conditions align for a strong 'SELL' signal.
    - Downtrend (Price < SMA) -> +1 sell_score
    - Overbought (RSI > 70) -> +1 sell_score
    Total sell_score = 2, which is >= 2.
    """
    market_data = {'current_price': 95, 'sma': 100, 'rsi': 75}
    signal = generate_signal(symbol='BTCUSDT', market_data=market_data, signal_threshold=2)
    assert signal['signal'] == 'SELL'

def test_hold_signal_insufficient_score():
    """
    Test Case: Only one indicator is bullish, resulting in a 'HOLD'.
    - Uptrend (Price > SMA) -> +1 buy_score
    - Neutral RSI -> 0
    Total buy_score = 1, which is < 2.
    """
    market_data = {'current_price': 105, 'sma': 100, 'rsi': 50}
    signal = generate_signal(symbol='BTCUSDT', market_data=market_data, signal_threshold=2)
    assert signal['signal'] == 'HOLD'

def test_hold_signal_all_neutral():
    """
    Test Case: All indicators are in a neutral state.
    Should result in a 'HOLD'.
    """
    market_data = {'current_price': 101, 'sma': 100, 'rsi': 50}
    signal = generate_signal(symbol='BTCUSDT', market_data=market_data)
    assert signal['signal'] == 'HOLD'

def test_missing_market_data():
    """
    Test Case: One or more market data points are missing.
    Should result in a 'HOLD' and a clear reason.
    """
    market_data_missing_rsi = {'current_price': 100, 'sma': 95, 'rsi': None}
    signal = generate_signal(symbol='BTCUSDT', market_data=market_data_missing_rsi)
    assert signal['signal'] == 'HOLD'
    assert "Missing market data" in signal['reason']

# --- Tests for Sentiment Signal Mode ---

class TestSentimentMode:
    """Tests for signal_mode='sentiment' in the crypto signal engine."""

    BULLISH_GEMINI = {
        'gemini_assessment': {'direction': 'bullish', 'confidence': 0.85, 'reasoning': 'ETF inflows strong'},
        'avg_sentiment_score': 0,
    }
    BEARISH_GEMINI = {
        'gemini_assessment': {'direction': 'bearish', 'confidence': 0.80, 'reasoning': 'Regulatory crackdown'},
        'avg_sentiment_score': 0,
    }
    SENTIMENT_CONFIG = {
        'min_gemini_confidence': 0.7,
        'min_vader_score': 0.3,
        'rsi_buy_veto_threshold': 75,
        'rsi_sell_veto_threshold': 25,
    }

    def test_bullish_uptrend_generates_buy(self):
        """Gemini bullish + price > SMA + normal RSI = BUY."""
        market_data = {'current_price': 105, 'sma': 100, 'rsi': 50}
        signal = generate_signal(
            symbol='BTCUSDT', market_data=market_data,
            news_sentiment_data=self.BULLISH_GEMINI,
            signal_mode='sentiment', sentiment_config=self.SENTIMENT_CONFIG,
        )
        assert signal['signal'] == 'BUY'
        assert 'Gemini bullish' in signal['reason']

    def test_bearish_downtrend_generates_sell(self):
        """Gemini bearish + price < SMA + normal RSI = SELL."""
        market_data = {'current_price': 95, 'sma': 100, 'rsi': 50}
        signal = generate_signal(
            symbol='BTCUSDT', market_data=market_data,
            news_sentiment_data=self.BEARISH_GEMINI,
            signal_mode='sentiment', sentiment_config=self.SENTIMENT_CONFIG,
        )
        assert signal['signal'] == 'SELL'
        assert 'Gemini bearish' in signal['reason']

    def test_bullish_blocked_by_downtrend(self):
        """Gemini bullish but price < SMA = HOLD (don't trade against trend)."""
        market_data = {'current_price': 95, 'sma': 100, 'rsi': 50}
        signal = generate_signal(
            symbol='BTCUSDT', market_data=market_data,
            news_sentiment_data=self.BULLISH_GEMINI,
            signal_mode='sentiment', sentiment_config=self.SENTIMENT_CONFIG,
        )
        assert signal['signal'] == 'HOLD'
        assert 'downtrend' in signal['reason']

    def test_bearish_blocked_by_uptrend(self):
        """Gemini bearish but price > SMA = HOLD."""
        market_data = {'current_price': 105, 'sma': 100, 'rsi': 50}
        signal = generate_signal(
            symbol='BTCUSDT', market_data=market_data,
            news_sentiment_data=self.BEARISH_GEMINI,
            signal_mode='sentiment', sentiment_config=self.SENTIMENT_CONFIG,
        )
        assert signal['signal'] == 'HOLD'
        assert 'uptrend' in signal['reason']

    def test_rsi_veto_blocks_buy(self):
        """Gemini bullish + uptrend but RSI > 75 = HOLD (overbought veto)."""
        market_data = {'current_price': 105, 'sma': 100, 'rsi': 80}
        signal = generate_signal(
            symbol='BTCUSDT', market_data=market_data,
            news_sentiment_data=self.BULLISH_GEMINI,
            signal_mode='sentiment', sentiment_config=self.SENTIMENT_CONFIG,
        )
        assert signal['signal'] == 'HOLD'
        assert 'overbought veto' in signal['reason']

    def test_rsi_veto_blocks_sell(self):
        """Gemini bearish + downtrend but RSI < 25 = HOLD (oversold veto)."""
        market_data = {'current_price': 95, 'sma': 100, 'rsi': 20}
        signal = generate_signal(
            symbol='BTCUSDT', market_data=market_data,
            news_sentiment_data=self.BEARISH_GEMINI,
            signal_mode='sentiment', sentiment_config=self.SENTIMENT_CONFIG,
        )
        assert signal['signal'] == 'HOLD'
        assert 'oversold veto' in signal['reason']

    def test_no_sentiment_data_holds(self):
        """No news data at all = HOLD in sentiment mode."""
        market_data = {'current_price': 105, 'sma': 100, 'rsi': 50}
        signal = generate_signal(
            symbol='BTCUSDT', market_data=market_data,
            news_sentiment_data=None,
            signal_mode='sentiment', sentiment_config=self.SENTIMENT_CONFIG,
        )
        assert signal['signal'] == 'HOLD'
        assert 'No sentiment trigger' in signal['reason']

    def test_low_confidence_gemini_falls_back_to_vader(self):
        """Gemini confidence below threshold, VADER takes over."""
        news_data = {
            'gemini_assessment': {'direction': 'bullish', 'confidence': 0.5, 'reasoning': 'Low conf'},
            'avg_sentiment_score': 0.4,
        }
        market_data = {'current_price': 105, 'sma': 100, 'rsi': 50}
        signal = generate_signal(
            symbol='BTCUSDT', market_data=market_data,
            news_sentiment_data=news_data,
            signal_mode='sentiment', sentiment_config=self.SENTIMENT_CONFIG,
        )
        assert signal['signal'] == 'BUY'
        assert 'VADER bullish' in signal['reason']

    def test_vader_below_threshold_holds(self):
        """VADER score below min_vader_score = HOLD."""
        news_data = {
            'gemini_assessment': {'direction': 'bullish', 'confidence': 0.3, 'reasoning': 'Low conf'},
            'avg_sentiment_score': 0.1,  # Below 0.3 threshold
        }
        market_data = {'current_price': 105, 'sma': 100, 'rsi': 50}
        signal = generate_signal(
            symbol='BTCUSDT', market_data=market_data,
            news_sentiment_data=news_data,
            signal_mode='sentiment', sentiment_config=self.SENTIMENT_CONFIG,
        )
        assert signal['signal'] == 'HOLD'

    def test_scoring_mode_backward_compatible(self):
        """Explicit signal_mode='scoring' works identically to the default."""
        market_data = {'current_price': 105, 'sma': 100, 'rsi': 25}
        signal = generate_signal(
            symbol='BTCUSDT', market_data=market_data,
            signal_mode='scoring', signal_threshold=2,
        )
        assert signal['signal'] == 'BUY'

    def test_adx_blocks_weak_trend(self):
        """ADX below threshold blocks signal in sentiment mode."""
        # Generate 30 prices with very low volatility (ADX will be low)
        base_prices = [100.0 + (0.01 * (i % 3 - 1)) for i in range(30)]
        market_data = {'current_price': 105, 'sma': 100, 'rsi': 50}
        config = dict(self.SENTIMENT_CONFIG)
        config['adx_min_threshold'] = 50  # set very high so ADX will be below
        signal = generate_signal(
            symbol='BTCUSDT', market_data=market_data,
            news_sentiment_data=self.BULLISH_GEMINI,
            signal_mode='sentiment', sentiment_config=config,
            historical_prices=base_prices,
        )
        assert signal['signal'] == 'HOLD'
        assert 'ADX' in signal['reason']

    def test_adx_no_filter_when_not_configured(self):
        """Without adx_min_threshold in config, ADX filter does not block."""
        base_prices = [100.0 + (0.01 * (i % 3 - 1)) for i in range(30)]
        market_data = {'current_price': 105, 'sma': 100, 'rsi': 50}
        # No adx_min_threshold in config
        signal = generate_signal(
            symbol='BTCUSDT', market_data=market_data,
            news_sentiment_data=self.BULLISH_GEMINI,
            signal_mode='sentiment', sentiment_config=self.SENTIMENT_CONFIG,
            historical_prices=base_prices,
        )
        # Should pass through (BUY or whatever, but NOT blocked by ADX)
        assert signal['signal'] == 'BUY'
