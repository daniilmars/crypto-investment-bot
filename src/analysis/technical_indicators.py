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
