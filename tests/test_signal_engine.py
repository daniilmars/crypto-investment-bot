# tests/test_signal_engine.py

import pytest
from src.analysis.signal_engine import generate_signal

# --- Mock Data Fixtures ---

@pytest.fixture
def whales_bullish():
    """Fixture for bullish whale activity (net outflow from exchanges)."""
    return [{'from': {'owner_type': 'exchange'}, 'to': {'owner_type': 'wallet'}, 'amount_usd': 5000000}]

@pytest.fixture
def whales_bearish():
    """Fixture for bearish whale activity (net inflow to exchanges)."""
    return [{'from': {'owner_type': 'wallet'}, 'to': {'owner_type': 'exchange'}, 'amount_usd': 5000000}]

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
    - Uptrend (Price > SMA)
    - Oversold (RSI < 30)
    - Bullish whale activity
    """
    market_data = {'current_price': 105, 'sma': 100, 'rsi': 25}
    signal = generate_signal(whales_bullish, market_data)
    assert signal['signal'] == 'BUY'
    assert "Uptrend, oversold, and whale activity confirms BUY" in signal['reason']

def test_strong_sell_signal(whales_bearish):
    """
    Test Case: Conditions align for a strong 'SELL' signal.
    - Downtrend (Price < SMA)
    - Overbought (RSI > 70)
    - Bearish whale activity
    """
    market_data = {'current_price': 95, 'sma': 100, 'rsi': 75}
    signal = generate_signal(whales_bearish, market_data)
    assert signal['signal'] == 'SELL'
    assert "Downtrend, overbought, and whale activity confirms SELL" in signal['reason']

def test_hold_signal_due_to_rsi(whales_bullish):
    """
    Test Case: Uptrend and bullish whales, but RSI is neutral (not oversold).
    Should result in a 'HOLD'.
    """
    market_data = {'current_price': 105, 'sma': 100, 'rsi': 50}
    signal = generate_signal(whales_bullish, market_data)
    assert signal['signal'] == 'HOLD'
    assert "No strong signal detected" in signal['reason']

def test_hold_signal_due_to_trend(whales_bullish):
    """
    Test Case: Oversold and bullish whales, but price is in a downtrend.
    Should result in a 'HOLD'.
    """
    market_data = {'current_price': 95, 'sma': 100, 'rsi': 25}
    signal = generate_signal(whales_bullish, market_data)
    assert signal['signal'] == 'HOLD'
    assert "No strong signal detected" in signal['reason']

def test_hold_signal_due_to_whales(whales_neutral):
    """
    Test Case: Uptrend and oversold, but no confirming whale activity.
    Should result in a 'HOLD'.
    """
    market_data = {'current_price': 105, 'sma': 100, 'rsi': 25}
    signal = generate_signal(whales_neutral, market_data)
    assert signal['signal'] == 'HOLD'
    assert "No strong signal detected" in signal['reason']

def test_hold_signal_all_neutral():
    """
    Test Case: All indicators are in a neutral state.
    Should result in a 'HOLD'.
    """
    market_data = {'current_price': 101, 'sma': 100, 'rsi': 50}
    signal = generate_signal([], market_data)
    assert signal['signal'] == 'HOLD'

def test_missing_market_data():
    """
    Test Case: One or more market data points are missing.
    Should result in a 'HOLD' and a clear reason.
    """
    market_data_missing_rsi = {'current_price': 100, 'sma': 95, 'rsi': None}
    signal = generate_signal([], market_data_missing_rsi)
    assert signal['signal'] == 'HOLD'
    assert "Missing market data" in signal['reason']

    market_data_missing_sma = {'current_price': 100, 'sma': None, 'rsi': 50}
    signal = generate_signal([], market_data_missing_sma)
    assert signal['signal'] == 'HOLD'
    assert "Missing market data" in signal['reason']

# --- Tests for High-Priority Wallet Logic ---

def test_high_priority_sell_signal(high_interest_wallets):
    """
    Test Case: A high-interest wallet sends funds to an exchange, triggering a SELL.
    This should override any other technical indicators.
    """
    # Market data suggests HOLD, but the whale action should trigger a SELL.
    market_data = {'current_price': 101, 'sma': 100, 'rsi': 50}
    high_priority_tx = [{
        'from': {'owner': 'Grayscale', 'owner_type': 'institution'},
        'to': {'owner': 'Coinbase', 'owner_type': 'exchange'},
        'amount_usd': 50000000
    }]
    signal = generate_signal(high_priority_tx, market_data, high_interest_wallets)
    assert signal['signal'] == 'SELL'
    assert "High-priority signal" in signal['reason']
    assert "Grayscale" in signal['reason']

def test_high_priority_buy_signal(high_interest_wallets):
    """
    Test Case: A high-interest wallet receives funds from an exchange, triggering a BUY.
    This should override any other technical indicators.
    """
    # Market data suggests HOLD, but the whale action should trigger a BUY.
    market_data = {'current_price': 101, 'sma': 100, 'rsi': 50}
    high_priority_tx = [{
        'from': {'owner': 'Binance', 'owner_type': 'exchange'},
        'to': {'owner': 'Grayscale', 'owner_type': 'institution'},
        'amount_usd': 50000000
    }]
    signal = generate_signal(high_priority_tx, market_data, high_interest_wallets)
    assert signal['signal'] == 'BUY'
    assert "High-priority signal" in signal['reason']
    assert "Grayscale" in signal['reason']

# --- Tests for Stablecoin Flow Logic ---

def test_stablecoin_inflow_buy_signal():
    """
    Test Case: A large inflow of stablecoins to exchanges triggers a market-wide BUY.
    """
    market_data = {'current_price': 95, 'sma': 100, 'rsi': 75} # Techincals suggest SELL
    stablecoin_data = {'stablecoin_inflow_usd': 150000000} # Above 100M threshold
    
    signal = generate_signal([], market_data, [], stablecoin_data, stablecoin_threshold=100000000)
    assert signal['signal'] == 'BUY'
    assert "Massive stablecoin inflow" in signal['reason']

def test_stablecoin_inflow_no_signal(whales_bearish):
    """
    Test Case: Stablecoin inflow is below the threshold, so no priority signal is fired.
    The engine should fall back to the standard technical analysis.
    """
    market_data = {'current_price': 95, 'sma': 100, 'rsi': 75} # Techincals suggest SELL
    stablecoin_data = {'stablecoin_inflow_usd': 50000000} # Below 100M threshold
    
    signal = generate_signal(whales_bearish, market_data, [], stablecoin_data, stablecoin_threshold=100000000)
    assert signal['signal'] == 'SELL' # Falls back to the bearish technical signal
    assert "Downtrend, overbought" in signal['reason']

# --- Tests for Volatility Anomaly Logic ---

def test_volatility_warning_signal():
    """
    Test Case: A transaction velocity anomaly triggers a VOLATILITY_WARNING signal,
    overriding a potential BUY signal.
    """
    market_data = {'current_price': 105, 'sma': 100, 'rsi': 25} # Techincals suggest BUY
    velocity_data = {'is_anomaly': True, 'current_count': 20, 'baseline_avg': 2.0}
    
    signal = generate_signal([], market_data, velocity_data=velocity_data, velocity_threshold_multiplier=5.0)
    assert signal['signal'] == 'VOLATILITY_WARNING'
    assert "Transaction velocity anomaly detected" in signal['reason']

def test_no_volatility_warning_signal(whales_bullish):
    """
    Test Case: Transaction velocity is high but below the anomaly threshold,
    allowing the standard technical signal to be generated.
    """
    market_data = {'current_price': 105, 'sma': 100, 'rsi': 25} # Techincals suggest BUY
    # Anomaly flag is True, but multiplier check in signal engine should prevent firing
    velocity_data = {'is_anomaly': True, 'current_count': 8, 'baseline_avg': 2.0}
    
    signal = generate_signal(whales_bullish, market_data, velocity_data=velocity_data, velocity_threshold_multiplier=5.0)
    assert signal['signal'] == 'BUY' # Falls back to bullish technical signal
    assert "Uptrend, oversold" in signal['reason']

