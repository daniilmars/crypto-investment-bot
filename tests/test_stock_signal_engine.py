# tests/test_stock_signal_engine.py

import pytest
from src.analysis.stock_signal_engine import generate_stock_signal


# --- Helper to build market_data ---
def _market(price, sma=None, rsi=None):
    return {'current_price': price, 'sma': sma, 'rsi': rsi}


def _volume(current, avg, change_pct=0):
    return {'current_volume': current, 'avg_volume': avg, 'price_change_percent': change_pct}


def _fundamentals(pe=None, earnings_growth=None):
    return {'pe_ratio': pe, 'earnings_growth': earnings_growth}


# --- Strong BUY Tests ---

class TestStrongBuy:
    def test_buy_sma_and_rsi(self):
        """BUY when price > SMA and RSI < 30 (oversold)."""
        signal = generate_stock_signal(
            symbol="AAPL",
            market_data=_market(price=150, sma=140, rsi=25),
            signal_threshold=2,
        )
        assert signal['signal'] == "BUY"
        assert signal['symbol'] == "AAPL"
        assert signal['current_price'] == 150

    def test_buy_sma_and_fundamentals(self):
        """BUY when price > SMA and P/E < 25 with positive earnings growth."""
        signal = generate_stock_signal(
            symbol="MSFT",
            market_data=_market(price=300, sma=280, rsi=50),
            fundamental_data=_fundamentals(pe=20, earnings_growth=15),
            signal_threshold=2,
        )
        assert signal['signal'] == "BUY"

    def test_buy_volume_and_rsi(self):
        """BUY when volume spike with price up AND RSI < 30."""
        signal = generate_stock_signal(
            symbol="GOOGL",
            market_data=_market(price=140, sma=145, rsi=25),
            volume_data=_volume(current=2000000, avg=1000000, change_pct=3.5),
            signal_threshold=2,
        )
        assert signal['signal'] == "BUY"


# --- Strong SELL Tests ---

class TestStrongSell:
    def test_sell_sma_and_rsi(self):
        """SELL when price < SMA and RSI > 70 (overbought)."""
        signal = generate_stock_signal(
            symbol="TSLA",
            market_data=_market(price=200, sma=220, rsi=75),
            signal_threshold=2,
        )
        assert signal['signal'] == "SELL"

    def test_sell_fundamentals_and_volume(self):
        """SELL when P/E > 40 (overvalued) and volume spike with price down."""
        signal = generate_stock_signal(
            symbol="NVDA",
            market_data=_market(price=500, sma=480, rsi=55),
            volume_data=_volume(current=5000000, avg=2000000, change_pct=-4.0),
            fundamental_data=_fundamentals(pe=45, earnings_growth=10),
            signal_threshold=2,
        )
        assert signal['signal'] == "SELL"

    def test_sell_negative_earnings_and_sma(self):
        """SELL when earnings growth is very negative and price < SMA."""
        signal = generate_stock_signal(
            symbol="META",
            market_data=_market(price=300, sma=320, rsi=55),
            fundamental_data=_fundamentals(pe=30, earnings_growth=-15),
            signal_threshold=2,
        )
        assert signal['signal'] == "SELL"


# --- HOLD Tests ---

class TestHold:
    def test_hold_insufficient_score(self):
        """HOLD when only one indicator triggers."""
        signal = generate_stock_signal(
            symbol="AMZN",
            market_data=_market(price=180, sma=175, rsi=50),
        )
        assert signal['signal'] == "HOLD"

    def test_hold_missing_data(self):
        """HOLD when SMA and RSI are both None (insufficient data)."""
        signal = generate_stock_signal(
            symbol="AAPL",
            market_data=_market(price=150, sma=None, rsi=None),
        )
        assert signal['signal'] == "HOLD"

    def test_hold_conflicting_signals(self):
        """HOLD when buy and sell indicators conflict (1 buy + 1 sell)."""
        signal = generate_stock_signal(
            symbol="TSLA",
            market_data=_market(price=250, sma=240, rsi=75),  # SMA=buy, RSI=sell
        )
        assert signal['signal'] == "HOLD"

    def test_hold_missing_price(self):
        """HOLD when current price is None."""
        signal = generate_stock_signal(
            symbol="AAPL",
            market_data={'current_price': None, 'sma': 150, 'rsi': 50},
        )
        assert signal['signal'] == "HOLD"
        assert signal['current_price'] == 0


# --- Edge Case Tests ---

class TestEdgeCases:
    def test_no_volume_data(self):
        """Signal works correctly with no volume data passed."""
        signal = generate_stock_signal(
            symbol="AAPL",
            market_data=_market(price=150, sma=140, rsi=25),
            volume_data=None,
            signal_threshold=2,
        )
        assert signal['signal'] == "BUY"  # SMA + RSI still trigger

    def test_no_fundamentals_data(self):
        """Signal works correctly with no fundamental data passed."""
        signal = generate_stock_signal(
            symbol="TSLA",
            market_data=_market(price=200, sma=220, rsi=75),
            fundamental_data=None,
            signal_threshold=2,
        )
        assert signal['signal'] == "SELL"  # SMA + RSI still trigger

    def test_signal_format(self):
        """Verify the signal dict has the expected keys."""
        signal = generate_stock_signal(
            symbol="AAPL",
            market_data=_market(price=150, sma=140, rsi=50),
        )
        assert 'signal' in signal
        assert 'symbol' in signal
        assert 'reason' in signal
        assert 'current_price' in signal

    def test_custom_thresholds(self):
        """BUY with custom RSI thresholds (RSI 40 with oversold at 45)."""
        signal = generate_stock_signal(
            symbol="AAPL",
            market_data=_market(price=150, sma=140, rsi=40),
            rsi_oversold_threshold=45,
            signal_threshold=2,
        )
        assert signal['signal'] == "BUY"


# --- Tests for Stock Sentiment Signal Mode ---

class TestStockSentimentMode:
    """Tests for signal_mode='sentiment' in the stock signal engine."""

    BULLISH_GEMINI = {
        'gemini_assessment': {'direction': 'bullish', 'confidence': 0.85, 'reasoning': 'Strong earnings'},
        'avg_sentiment_score': 0,
    }
    BEARISH_GEMINI = {
        'gemini_assessment': {'direction': 'bearish', 'confidence': 0.80, 'reasoning': 'Revenue miss'},
        'avg_sentiment_score': 0,
    }
    SENTIMENT_CONFIG = {
        'min_gemini_confidence': 0.7,
        'min_vader_score': 0.3,
        'rsi_buy_veto_threshold': 75,
        'rsi_sell_veto_threshold': 25,
        'pe_buy_veto_threshold': 40,
    }

    def test_bullish_uptrend_generates_buy(self):
        """Gemini bullish + price > SMA + normal RSI = BUY."""
        signal = generate_stock_signal(
            symbol="AAPL", market_data=_market(price=150, sma=140, rsi=50),
            news_sentiment_data=self.BULLISH_GEMINI,
            signal_mode='sentiment', sentiment_config=self.SENTIMENT_CONFIG,
        )
        assert signal['signal'] == "BUY"
        assert 'Gemini bullish' in signal['reason']

    def test_bearish_downtrend_generates_sell(self):
        """Gemini bearish + price < SMA + normal RSI = SELL."""
        signal = generate_stock_signal(
            symbol="AAPL", market_data=_market(price=130, sma=140, rsi=50),
            news_sentiment_data=self.BEARISH_GEMINI,
            signal_mode='sentiment', sentiment_config=self.SENTIMENT_CONFIG,
        )
        assert signal['signal'] == "SELL"
        assert 'Gemini bearish' in signal['reason']

    def test_bullish_blocked_by_downtrend(self):
        """Gemini bullish but price < SMA = HOLD."""
        signal = generate_stock_signal(
            symbol="AAPL", market_data=_market(price=130, sma=140, rsi=50),
            news_sentiment_data=self.BULLISH_GEMINI,
            signal_mode='sentiment', sentiment_config=self.SENTIMENT_CONFIG,
        )
        assert signal['signal'] == "HOLD"
        assert 'downtrend' in signal['reason']

    def test_rsi_veto_blocks_buy(self):
        """Gemini bullish + uptrend but RSI > 75 = HOLD."""
        signal = generate_stock_signal(
            symbol="AAPL", market_data=_market(price=150, sma=140, rsi=80),
            news_sentiment_data=self.BULLISH_GEMINI,
            signal_mode='sentiment', sentiment_config=self.SENTIMENT_CONFIG,
        )
        assert signal['signal'] == "HOLD"
        assert 'overbought veto' in signal['reason']

    def test_pe_veto_blocks_buy(self):
        """Gemini bullish + uptrend + good RSI but P/E > 40 = HOLD."""
        signal = generate_stock_signal(
            symbol="TSLA", market_data=_market(price=250, sma=240, rsi=50),
            fundamental_data=_fundamentals(pe=55),
            news_sentiment_data=self.BULLISH_GEMINI,
            signal_mode='sentiment', sentiment_config=self.SENTIMENT_CONFIG,
        )
        assert signal['signal'] == "HOLD"
        assert 'overvalued veto' in signal['reason']

    def test_pe_veto_does_not_block_sell(self):
        """P/E veto only applies to BUY, not SELL."""
        signal = generate_stock_signal(
            symbol="TSLA", market_data=_market(price=230, sma=240, rsi=50),
            fundamental_data=_fundamentals(pe=55),
            news_sentiment_data=self.BEARISH_GEMINI,
            signal_mode='sentiment', sentiment_config=self.SENTIMENT_CONFIG,
        )
        assert signal['signal'] == "SELL"

    def test_no_sentiment_holds(self):
        """No news data = HOLD in sentiment mode."""
        signal = generate_stock_signal(
            symbol="AAPL", market_data=_market(price=150, sma=140, rsi=50),
            news_sentiment_data=None,
            signal_mode='sentiment', sentiment_config=self.SENTIMENT_CONFIG,
        )
        assert signal['signal'] == "HOLD"
        assert 'No sentiment trigger' in signal['reason']

    def test_vader_fallback(self):
        """Gemini below threshold, VADER bullish takes over."""
        news_data = {
            'gemini_assessment': {'direction': 'bullish', 'confidence': 0.5, 'reasoning': 'Low conf'},
            'avg_sentiment_score': 0.5,
        }
        signal = generate_stock_signal(
            symbol="AAPL", market_data=_market(price=150, sma=140, rsi=50),
            news_sentiment_data=news_data,
            signal_mode='sentiment', sentiment_config=self.SENTIMENT_CONFIG,
        )
        assert signal['signal'] == "BUY"
        assert 'VADER bullish' in signal['reason']

    def test_scoring_mode_backward_compatible(self):
        """Explicit signal_mode='scoring' works identically to the default."""
        signal = generate_stock_signal(
            symbol="AAPL",
            market_data=_market(price=150, sma=140, rsi=25),
            signal_mode='scoring',
            signal_threshold=2,
        )
        assert signal['signal'] == "BUY"
