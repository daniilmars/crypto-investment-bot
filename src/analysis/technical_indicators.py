from typing import Optional
import pandas as pd
from src.logger import log


# ---------------------------------------------------------------------------
# Market Regime Detection (ATR + ADX)
# ---------------------------------------------------------------------------

def calculate_atr(prices_high: list, prices_low: list, prices_close: list,
                  period: int = 14) -> Optional[float]:
    """
    Calculates the Average True Range (ATR).

    Args:
        prices_high: High prices, oldest to newest.
        prices_low: Low prices, oldest to newest.
        prices_close: Close prices, oldest to newest.
        period: Lookback period.

    Returns:
        The ATR value, or None if not enough data.
    """
    if len(prices_close) < period + 1:
        log.warning(f"Not enough data to calculate ATR. Need {period + 1}, have {len(prices_close)}.")
        return None

    high = pd.Series(prices_high, dtype=float)
    low = pd.Series(prices_low, dtype=float)
    close = pd.Series(prices_close, dtype=float)

    prev_close = close.shift(1)
    tr = pd.concat([
        high - low,
        (high - prev_close).abs(),
        (low - prev_close).abs()
    ], axis=1).max(axis=1)

    atr = tr.rolling(window=period).mean().iloc[-1]
    log.info(f"Calculated ATR({period}) as: {atr:.4f}")
    return float(atr)


def calculate_atr_from_closes(prices: list, period: int = 14) -> Optional[float]:
    """
    Simplified ATR using only close prices (estimates high/low from closes).
    Useful when only close prices are available (e.g., crypto hourly data).
    """
    if len(prices) < period + 1:
        return None
    close = pd.Series(prices, dtype=float)
    tr = close.diff().abs()
    atr = tr.rolling(window=period).mean().iloc[-1]
    return float(atr)


def calculate_adx(prices_high: list, prices_low: list, prices_close: list,
                  period: int = 14) -> Optional[float]:
    """
    Calculates the Average Directional Index (ADX).

    ADX > 25  → trending market
    ADX < 20  → ranging / consolidating market

    Args:
        prices_high, prices_low, prices_close: OHLC data, oldest to newest.
        period: Lookback period.

    Returns:
        The ADX value (0-100), or None if not enough data.
    """
    needed = period * 2 + 1
    if len(prices_close) < needed:
        log.warning(f"Not enough data to calculate ADX. Need {needed}, have {len(prices_close)}.")
        return None

    high = pd.Series(prices_high, dtype=float)
    low = pd.Series(prices_low, dtype=float)
    close = pd.Series(prices_close, dtype=float)

    prev_close = close.shift(1)
    tr = pd.concat([
        high - low,
        (high - prev_close).abs(),
        (low - prev_close).abs()
    ], axis=1).max(axis=1)

    # Directional movement
    up_move = high - high.shift(1)
    down_move = low.shift(1) - low
    plus_dm = up_move.where((up_move > down_move) & (up_move > 0), 0.0)
    minus_dm = down_move.where((down_move > up_move) & (down_move > 0), 0.0)

    # Smoothed averages (Wilder's smoothing)
    atr_smooth = tr.rolling(window=period).mean()
    plus_di = 100 * (plus_dm.rolling(window=period).mean() / atr_smooth)
    minus_di = 100 * (minus_dm.rolling(window=period).mean() / atr_smooth)

    dx = 100 * ((plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, 1))
    adx = dx.rolling(window=period).mean().iloc[-1]

    log.info(f"Calculated ADX({period}) as: {adx:.2f}")
    return float(adx)


def calculate_adx_from_closes(prices: list, period: int = 14) -> Optional[float]:
    """
    Simplified ADX estimated from close prices only.
    Uses close-to-close changes as a proxy for directional movement.
    """
    needed = period * 2 + 1
    if len(prices) < needed:
        return None

    close = pd.Series(prices, dtype=float)
    diff = close.diff()

    plus_dm = diff.where(diff > 0, 0.0)
    minus_dm = (-diff).where(diff < 0, 0.0)
    tr = diff.abs()

    atr_smooth = tr.rolling(window=period).mean()
    atr_smooth = atr_smooth.replace(0, float('nan'))
    plus_di = 100 * (plus_dm.rolling(window=period).mean() / atr_smooth)
    minus_di = 100 * (minus_dm.rolling(window=period).mean() / atr_smooth)

    di_sum = (plus_di + minus_di).replace(0, float('nan'))
    dx = 100 * ((plus_di - minus_di).abs() / di_sum)
    adx = dx.rolling(window=period).mean().iloc[-1]

    if pd.isna(adx):
        return None
    log.info(f"Calculated ADX({period}) from closes as: {adx:.2f}")
    return float(adx)


def detect_market_regime(prices: list, atr_period: int = 14, adx_period: int = 14,
                         prices_high: list = None, prices_low: list = None) -> dict:
    """
    Classifies the current market regime based on ATR and ADX.

    Returns:
        dict with keys:
        - regime: 'trending' | 'ranging' | 'volatile'
        - adx: float | None
        - atr: float | None
        - atr_pct: float | None  (ATR as % of current price)
        - strategy_params: dict with recommended parameter adjustments
    """
    current_price = prices[-1] if prices else None

    # Use OHLC data if available, otherwise estimate from closes
    if prices_high and prices_low:
        adx = calculate_adx(prices_high, prices_low, prices, period=adx_period)
        atr = calculate_atr(prices_high, prices_low, prices, period=atr_period)
    else:
        adx = calculate_adx_from_closes(prices, period=adx_period)
        atr = calculate_atr_from_closes(prices, period=atr_period)

    atr_pct = (atr / current_price * 100) if (atr and current_price) else None

    # Classify regime
    if adx is not None and adx >= 25:
        regime = 'trending'
        # In trends: wider stops, follow momentum
        strategy_params = {
            'stop_loss_multiplier': 1.5,
            'take_profit_multiplier': 1.5,
            'signal_threshold': 2,         # normal threshold
            'risk_multiplier': 1.2,        # slightly more aggressive
        }
    elif atr_pct is not None and atr_pct > 3.0:
        regime = 'volatile'
        # In volatile markets: tighter risk, require stronger signals
        strategy_params = {
            'stop_loss_multiplier': 0.8,
            'take_profit_multiplier': 0.8,
            'signal_threshold': 3,         # require stronger consensus
            'risk_multiplier': 0.5,        # reduce position size
        }
    else:
        regime = 'ranging'
        # In ranging markets: tight stops, mean-reversion friendly
        strategy_params = {
            'stop_loss_multiplier': 0.7,
            'take_profit_multiplier': 0.7,
            'signal_threshold': 2,
            'risk_multiplier': 0.8,
        }

    log.info(f"Market regime: {regime} (ADX={adx}, ATR={atr}, ATR%={atr_pct})")
    return {
        'regime': regime,
        'adx': adx,
        'atr': atr,
        'atr_pct': atr_pct,
        'strategy_params': strategy_params,
    }


# ---------------------------------------------------------------------------
# Multi-Timeframe Signal Confirmation
# ---------------------------------------------------------------------------

def multi_timeframe_confirmation(prices: list, sma_period: int = 20,
                                  rsi_period: int = 14) -> dict:
    """
    Evaluates trend agreement across short, medium, and long timeframes
    by subsampling the price data.

    Uses three views of the data:
    - Short: last 25% of prices (recent action)
    - Medium: last 50% of prices
    - Long: all prices (full history)

    Returns:
        dict with:
        - confirmed_direction: 'bullish' | 'bearish' | 'mixed'
        - agreement_count: int (0-3, how many timeframes agree)
        - details: list of per-timeframe results
    """
    if len(prices) < max(sma_period, rsi_period) + 1:
        return {
            'confirmed_direction': 'mixed',
            'agreement_count': 0,
            'details': [],
        }

    n = len(prices)
    timeframes = {
        'short': prices[max(0, n - n // 4):],
        'medium': prices[max(0, n - n // 2):],
        'long': prices,
    }

    bullish_count = 0
    bearish_count = 0
    details = []

    for tf_name, tf_prices in timeframes.items():
        if len(tf_prices) < max(sma_period, rsi_period) + 1:
            details.append({'timeframe': tf_name, 'direction': 'insufficient_data'})
            continue

        current = tf_prices[-1]
        sma = calculate_sma(tf_prices, period=min(sma_period, len(tf_prices)))
        rsi = calculate_rsi(tf_prices, period=min(rsi_period, len(tf_prices) - 1))

        tf_bullish = 0
        tf_bearish = 0

        if sma is not None:
            if current > sma:
                tf_bullish += 1
            else:
                tf_bearish += 1

        if rsi is not None:
            if rsi < 40:
                tf_bullish += 1   # oversold = buy opportunity
            elif rsi > 60:
                tf_bearish += 1

        if tf_bullish > tf_bearish:
            direction = 'bullish'
            bullish_count += 1
        elif tf_bearish > tf_bullish:
            direction = 'bearish'
            bearish_count += 1
        else:
            direction = 'neutral'

        details.append({'timeframe': tf_name, 'direction': direction, 'sma': sma, 'rsi': rsi})

    if bullish_count >= 2:
        confirmed = 'bullish'
    elif bearish_count >= 2:
        confirmed = 'bearish'
    else:
        confirmed = 'mixed'

    agreement = max(bullish_count, bearish_count)
    log.info(f"Multi-TF confirmation: {confirmed} ({agreement}/3 agree)")
    return {
        'confirmed_direction': confirmed,
        'agreement_count': agreement,
        'details': details,
    }

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
