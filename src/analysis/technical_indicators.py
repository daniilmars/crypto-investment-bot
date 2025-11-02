from typing import Optional
import pandas as pd
from src.logger import log

def calculate_sma(prices: list, period: int = 20) -> Optional[float]:
    """
    Calculates the Simple Moving Average (SMA) for a given list of prices.
    
    Args:
        prices (list): A list of historical prices, oldest to newest.
        period (int): The lookback period for the SMA calculation.
        
    Returns:
        float | None: The calculated SMA value, or None if there is not enough data.
    """
    if len(prices) < period:
        log.warning(f"Not enough data to calculate SMA. Need {period} prices, have {len(prices)}.")
        return None

    price_series = pd.Series(prices)
    sma = price_series.rolling(window=period).mean().iloc[-1]
    
    log.info(f"Calculated SMA({period}) as: {sma:.2f}")
    return sma

def calculate_rsi(prices: list, period: int = 14) -> Optional[float]:
    """
    Calculates the Relative Strength Index (RSI) for a given list of prices.
    
    Args:
        prices (list): A list of historical prices, oldest to newest.
        period (int): The lookback period for the RSI calculation.
        
    Returns:
        float | None: The calculated RSI value, or None if there is not enough data.
    """
    if len(prices) < period + 1:
        log.warning(f"Not enough data to calculate RSI. Need {period + 1} prices, have {len(prices)}.")
        return None

    price_series = pd.Series(prices)
    
    # Calculate price changes
    delta = price_series.diff()
    
    # Separate gains and losses
    gain = delta.where(delta > 0, 0)
    loss = -delta.where(delta < 0, 0)
    
    # Calculate the average gain and loss over the initial period
    avg_gain = gain.rolling(window=period, min_periods=period).mean().iloc[-1]
    avg_loss = loss.rolling(window=period, min_periods=period).mean().iloc[-1]
    
    if avg_loss == 0:
        # If average loss is zero, RSI is 100 (strong uptrend)
        return 100.0

    # Calculate Relative Strength (RS)
    rs = avg_gain / avg_loss
    
    # Calculate RSI
    rsi = 100 - (100 / (1 + rs))
    
    log.info(f"Calculated RSI({period}) as: {rsi:.2f}")
    return rsi

def calculate_transaction_velocity(symbol: str, recent_transactions: list, historical_timestamps: list, baseline_hours: int):
    """
    Analyzes the frequency of recent transactions against a historical baseline to detect anomalies.

    Args:
        symbol (str): The symbol of the crypto asset to analyze.
        recent_transactions (list): Transactions from the last hour.
        historical_timestamps (list): All transaction timestamps from the baseline period.
        baseline_hours (int): The total lookback period in hours for the baseline.

    Returns:
        dict: A dictionary containing the current count, baseline average, and an anomaly flag.
    """
    # Count transactions for the given symbol in the most recent period (last hour)
    current_hourly_count = sum(1 for tx in recent_transactions if tx.get('symbol') == symbol)
    
    # Calculate the baseline average hourly frequency
    if not historical_timestamps:
        baseline_hourly_avg = 0.0
    else:
        baseline_hourly_avg = len(historical_timestamps) / baseline_hours

    # Anomaly detection
    is_anomaly = False
    # A simple multiplier is used for now. More advanced stats can be used later.
    if baseline_hourly_avg > 0: # Only flag anomalies if there's a meaningful baseline
        if current_hourly_count > (baseline_hourly_avg * 5.0):
            is_anomaly = True
    
    log.info(f"Transaction Velocity for {symbol}: Current hour count: {current_hourly_count}, Baseline avg: {baseline_hourly_avg:.2f}/hr.")

    return {
        'symbol': symbol,
        'current_count': current_hourly_count,
        'baseline_avg': baseline_hourly_avg,
        'is_anomaly': is_anomaly
    }

def calculate_macd(prices: list, fast_period: int = 12, slow_period: int = 26, signal_period: int = 9) -> Optional[dict]:
    """
    Calculates the Moving Average Convergence Divergence (MACD) for a given list of prices.
    
    Args:
        prices (list): A list of historical prices, oldest to newest.
        fast_period (int): The lookback period for the fast EMA.
        slow_period (int): The lookback period for the slow EMA.
        signal_period (int): The lookback period for the signal line EMA.
        
    Returns:
        dict | None: A dictionary containing the MACD line, signal line, and histogram, or None if there is not enough data.
    """
    if len(prices) < slow_period:
        log.warning(f"Not enough data to calculate MACD. Need {slow_period} prices, have {len(prices)}.")
        return None

    price_series = pd.Series(prices)
    
    # Calculate the Fast and Slow EMAs
    ema_fast = price_series.ewm(span=fast_period, adjust=False).mean()
    ema_slow = price_series.ewm(span=slow_period, adjust=False).mean()
    
    # Calculate the MACD line
    macd_line = ema_fast - ema_slow
    
    # Calculate the Signal line
    signal_line = macd_line.ewm(span=signal_period, adjust=False).mean()
    
    # Calculate the Histogram
    histogram = macd_line - signal_line
    
    macd_values = {
        'macd_line': macd_line.iloc[-1],
        'signal_line': signal_line.iloc[-1],
        'histogram': histogram.iloc[-1]
    }
    
    log.info(f"Calculated MACD({fast_period}, {slow_period}, {signal_period}) as: {macd_values}")
    return macd_values

def calculate_bollinger_bands(prices: list, period: int = 20, std_dev: int = 2) -> Optional[dict]:
    """
    Calculates the Bollinger Bands for a given list of prices.
    
    Args:
        prices (list): A list of historical prices, oldest to newest.
        period (int): The lookback period for the moving average.
        std_dev (int): The number of standard deviations to use for the bands.
        
    Returns:
        dict | None: A dictionary containing the upper, middle, and lower bands, or None if there is not enough data.
    """
    if len(prices) < period:
        log.warning(f"Not enough data to calculate Bollinger Bands. Need {period} prices, have {len(prices)}.")
        return None

    price_series = pd.Series(prices)
    
    # Calculate the Middle Band (SMA)
    middle_band = price_series.rolling(window=period).mean()
    
    # Calculate the Standard Deviation
    rolling_std = price_series.rolling(window=period).std()
    
    # Calculate the Upper and Lower Bands
    upper_band = middle_band + (rolling_std * std_dev)
    lower_band = middle_band - (rolling_std * std_dev)
    
    bollinger_bands = {
        'upper_band': upper_band.iloc[-1],
        'middle_band': middle_band.iloc[-1],
        'lower_band': lower_band.iloc[-1]
    }
    
    log.info(f"Calculated Bollinger Bands({period}, {std_dev}) as: {bollinger_bands}")
    return bollinger_bands
