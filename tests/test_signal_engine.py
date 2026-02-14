# tests/test_signal_engine.py

import pytest
from src.analysis.signal_engine import generate_signal

# --- Mock Data Fixtures ---

@pytest.fixture
def whales_bullish():
    """Fixture for bullish whale activity (net outflow from exchanges)."""
    return [{'from': {'owner_type': 'exchange'}, 'to': {'owner_type': 'wallet'}, 'amount_usd': 5000000, 'symbol': 'BTC'}]

@pytest.fixture
def whales_bearish():
    """Fixture for bearish whale activity (net inflow to exchanges)."""
    return [{'from': {'owner_type': 'wallet'}, 'to': {'owner_type': 'exchange'}, 'amount_usd': 5000000, 'symbol': 'BTC'}]

@pytest.fixture
def whales_neutral():
    """Fixture for neutral or no significant whale activity."""
    return []

@pytest.fixture
def high_interest_wallets():
    """Fixture for a sample list of high-interest wallet names."""
    return ["Grayscale", "US Government"]

# --- Test Cases for the New `generate_signal` Function ---

def test_strong_buy_signal(whales_bullish):
    """
    Test Case: Conditions align for a strong 'BUY' signal.
    - Uptrend (Price > SMA) -> +1 buy_score
    - Oversold (RSI < 30) -> +1 buy_score
    - Bullish whale activity -> +1 buy_score
    Total buy_score = 3, which is >= 2.
    """
    market_data = {'current_price': 105, 'sma': 100, 'rsi': 25}
    signal = generate_signal(symbol='BTCUSDT', whale_transactions=whales_bullish, market_data=market_data)
    assert signal['signal'] == 'BUY'

def test_strong_sell_signal(whales_bearish):
    """
    Test Case: Conditions align for a strong 'SELL' signal.
    - Downtrend (Price < SMA) -> +1 sell_score
    - Overbought (RSI > 70) -> +1 sell_score
    - Bearish whale activity -> +1 sell_score
    Total sell_score = 3, which is >= 2.
    """
    market_data = {'current_price': 95, 'sma': 100, 'rsi': 75}
    signal = generate_signal(symbol='BTCUSDT', whale_transactions=whales_bearish, market_data=market_data)
    assert signal['signal'] == 'SELL'

def test_hold_signal_insufficient_score(whales_neutral):
    """
    Test Case: Only one indicator is bullish, resulting in a 'HOLD'.
    - Uptrend (Price > SMA) -> +1 buy_score
    - Neutral RSI -> 0
    - Neutral whale activity -> 0
    Total buy_score = 1, which is < 2.
    """
    market_data = {'current_price': 105, 'sma': 100, 'rsi': 50}
    signal = generate_signal(symbol='BTCUSDT', whale_transactions=whales_neutral, market_data=market_data)
    assert signal['signal'] == 'HOLD'

def test_hold_signal_all_neutral():
    """
    Test Case: All indicators are in a neutral state.
    Should result in a 'HOLD'.
    """
    market_data = {'current_price': 101, 'sma': 100, 'rsi': 50}
    signal = generate_signal(symbol='BTCUSDT', whale_transactions=[], market_data=market_data)
    assert signal['signal'] == 'HOLD'

def test_missing_market_data():
    """
    Test Case: One or more market data points are missing.
    Should result in a 'HOLD' and a clear reason.
    """
    market_data_missing_rsi = {'current_price': 100, 'sma': 95, 'rsi': None}
    signal = generate_signal(symbol='BTCUSDT', whale_transactions=[], market_data=market_data_missing_rsi)
    assert signal['signal'] == 'HOLD'
    assert "Missing market data" in signal['reason']

# --- Tests for High-Priority Wallet Logic ---

def test_high_priority_sell_signal(high_interest_wallets):
    """
    Test Case: A high-interest wallet sends funds to an exchange, triggering a SELL.
    This should override any other technical indicators.
    """
    market_data = {'current_price': 101, 'sma': 100, 'rsi': 50}
    high_priority_tx = [{
        'from': {'owner': 'Grayscale', 'owner_type': 'institution'},
        'to': {'owner': 'Coinbase', 'owner_type': 'exchange'},
        'amount_usd': 50000000,
        'symbol': 'BTC'
    }]
    signal = generate_signal(symbol='BTCUSDT', whale_transactions=high_priority_tx, market_data=market_data, high_interest_wallets=high_interest_wallets)
    assert signal['signal'] == 'SELL'
    assert "High-priority signal" in signal['reason']

def test_high_priority_buy_signal(high_interest_wallets):
    """
    Test Case: A high-interest wallet receives funds from an exchange, triggering a BUY.
    This should override any other technical indicators.
    """
    market_data = {'current_price': 101, 'sma': 100, 'rsi': 50}
    high_priority_tx = [{
        'from': {'owner': 'Binance', 'owner_type': 'exchange'},
        'to': {'owner': 'Grayscale', 'owner_type': 'institution'},
        'amount_usd': 50000000,
        'symbol': 'BTC'
    }]
    signal = generate_signal(symbol='BTCUSDT', whale_transactions=high_priority_tx, market_data=market_data, high_interest_wallets=high_interest_wallets)
    assert signal['signal'] == 'BUY'
    assert "High-priority signal" in signal['reason']

# --- Tests for Stablecoin Flow Logic ---

def test_stablecoin_inflow_buy_signal():
    """
    Test Case: A large inflow of stablecoins to exchanges triggers a market-wide BUY.
    """
    market_data = {'current_price': 95, 'sma': 100, 'rsi': 75} # Techincals suggest SELL
    stablecoin_data = {'stablecoin_inflow_usd': 150000000} # Above 100M threshold
    
    signal = generate_signal(symbol='BTCUSDT', whale_transactions=[], market_data=market_data, stablecoin_data=stablecoin_data, stablecoin_threshold=100000000)
    assert signal['signal'] == 'BUY'
    assert "Large stablecoin inflow" in signal['reason']

def test_stablecoin_inflow_no_signal(whales_bearish):
    """
    Test Case: Stablecoin inflow is below the threshold, so no priority signal is fired.
    The engine should fall back to the standard technical analysis.
    """
    market_data = {'current_price': 95, 'sma': 100, 'rsi': 75} # Techincals suggest SELL
    stablecoin_data = {'stablecoin_inflow_usd': 50000000} # Below 100M threshold
    
    signal = generate_signal(symbol='BTCUSDT', whale_transactions=whales_bearish, market_data=market_data, stablecoin_data=stablecoin_data, stablecoin_threshold=100000000)
    assert signal['signal'] == 'SELL'

# --- Tests for Volatility Anomaly Logic ---

def test_volatility_warning_signal():
    """
    Test Case: A transaction velocity anomaly triggers a VOLATILITY_WARNING signal,
    overriding a potential BUY signal.
    """
    market_data = {'current_price': 105, 'sma': 100, 'rsi': 25} # Techincals suggest BUY
    velocity_data = {'current_count': 20, 'baseline_avg': 2.0}
    
    signal = generate_signal(symbol='BTCUSDT', whale_transactions=[], market_data=market_data, velocity_data=velocity_data, velocity_threshold_multiplier=5.0)
    assert signal['signal'] == 'VOLATILITY_WARNING'
    assert "Transaction velocity anomaly detected" in signal['reason']

def test_no_volatility_warning_signal(whales_bullish):
    """
    Test Case: Transaction velocity is high but below the anomaly threshold,
    allowing the standard technical signal to be generated.
    """
    market_data = {'current_price': 105, 'sma': 100, 'rsi': 25} # Techincals suggest BUY
    velocity_data = {'current_count': 8, 'baseline_avg': 2.0}
    
    signal = generate_signal(symbol='BTCUSDT', whale_transactions=whales_bullish, market_data=market_data, velocity_data=velocity_data, velocity_threshold_multiplier=5.0)
    assert signal['signal'] == 'BUY'


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
            symbol='BTCUSDT', whale_transactions=[], market_data=market_data,
            news_sentiment_data=self.BULLISH_GEMINI,
            signal_mode='sentiment', sentiment_config=self.SENTIMENT_CONFIG,
        )
        assert signal['signal'] == 'BUY'
        assert 'Gemini bullish' in signal['reason']

    def test_bearish_downtrend_generates_sell(self):
        """Gemini bearish + price < SMA + normal RSI = SELL."""
        market_data = {'current_price': 95, 'sma': 100, 'rsi': 50}
        signal = generate_signal(
            symbol='BTCUSDT', whale_transactions=[], market_data=market_data,
            news_sentiment_data=self.BEARISH_GEMINI,
            signal_mode='sentiment', sentiment_config=self.SENTIMENT_CONFIG,
        )
        assert signal['signal'] == 'SELL'
        assert 'Gemini bearish' in signal['reason']

    def test_bullish_blocked_by_downtrend(self):
        """Gemini bullish but price < SMA = HOLD (don't trade against trend)."""
        market_data = {'current_price': 95, 'sma': 100, 'rsi': 50}
        signal = generate_signal(
            symbol='BTCUSDT', whale_transactions=[], market_data=market_data,
            news_sentiment_data=self.BULLISH_GEMINI,
            signal_mode='sentiment', sentiment_config=self.SENTIMENT_CONFIG,
        )
        assert signal['signal'] == 'HOLD'
        assert 'downtrend' in signal['reason']

    def test_bearish_blocked_by_uptrend(self):
        """Gemini bearish but price > SMA = HOLD."""
        market_data = {'current_price': 105, 'sma': 100, 'rsi': 50}
        signal = generate_signal(
            symbol='BTCUSDT', whale_transactions=[], market_data=market_data,
            news_sentiment_data=self.BEARISH_GEMINI,
            signal_mode='sentiment', sentiment_config=self.SENTIMENT_CONFIG,
        )
        assert signal['signal'] == 'HOLD'
        assert 'uptrend' in signal['reason']

    def test_rsi_veto_blocks_buy(self):
        """Gemini bullish + uptrend but RSI > 75 = HOLD (overbought veto)."""
        market_data = {'current_price': 105, 'sma': 100, 'rsi': 80}
        signal = generate_signal(
            symbol='BTCUSDT', whale_transactions=[], market_data=market_data,
            news_sentiment_data=self.BULLISH_GEMINI,
            signal_mode='sentiment', sentiment_config=self.SENTIMENT_CONFIG,
        )
        assert signal['signal'] == 'HOLD'
        assert 'overbought veto' in signal['reason']

    def test_rsi_veto_blocks_sell(self):
        """Gemini bearish + downtrend but RSI < 25 = HOLD (oversold veto)."""
        market_data = {'current_price': 95, 'sma': 100, 'rsi': 20}
        signal = generate_signal(
            symbol='BTCUSDT', whale_transactions=[], market_data=market_data,
            news_sentiment_data=self.BEARISH_GEMINI,
            signal_mode='sentiment', sentiment_config=self.SENTIMENT_CONFIG,
        )
        assert signal['signal'] == 'HOLD'
        assert 'oversold veto' in signal['reason']

    def test_no_sentiment_data_holds(self):
        """No news data at all = HOLD in sentiment mode."""
        market_data = {'current_price': 105, 'sma': 100, 'rsi': 50}
        signal = generate_signal(
            symbol='BTCUSDT', whale_transactions=[], market_data=market_data,
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
            symbol='BTCUSDT', whale_transactions=[], market_data=market_data,
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
            symbol='BTCUSDT', whale_transactions=[], market_data=market_data,
            news_sentiment_data=news_data,
            signal_mode='sentiment', sentiment_config=self.SENTIMENT_CONFIG,
        )
        assert signal['signal'] == 'HOLD'

    def test_high_priority_bypass_still_works(self):
        """High-priority wallet signal overrides sentiment mode."""
        market_data = {'current_price': 95, 'sma': 100, 'rsi': 50}
        high_priority_tx = [{
            'from': {'owner': 'Grayscale', 'owner_type': 'institution'},
            'to': {'owner': 'Coinbase', 'owner_type': 'exchange'},
            'amount_usd': 50000000,
            'symbol': 'BTC'
        }]
        signal = generate_signal(
            symbol='BTCUSDT', whale_transactions=high_priority_tx,
            market_data=market_data,
            high_interest_wallets=['Grayscale'],
            signal_mode='sentiment', sentiment_config=self.SENTIMENT_CONFIG,
        )
        assert signal['signal'] == 'SELL'
        assert 'High-priority signal' in signal['reason']

    def test_scoring_mode_backward_compatible(self):
        """Explicit signal_mode='scoring' works identically to the default."""
        market_data = {'current_price': 105, 'sma': 100, 'rsi': 25}
        whales = [{'from': {'owner_type': 'exchange'}, 'to': {'owner_type': 'wallet'}, 'amount_usd': 5000000, 'symbol': 'BTC'}]
        signal = generate_signal(
            symbol='BTCUSDT', whale_transactions=whales, market_data=market_data,
            signal_mode='scoring',
        )
        assert signal['signal'] == 'BUY'

