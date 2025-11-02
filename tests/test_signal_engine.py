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

