import pandas as pd
from src.logger import log
from src.analysis.technical_indicators import calculate_macd, calculate_bollinger_bands

def generate_signal(symbol, whale_transactions, market_data, high_interest_wallets=None, stablecoin_data=None, stablecoin_threshold=100000000, velocity_data=None, velocity_threshold_multiplier=5.0, rsi_overbought_threshold=70, rsi_oversold_threshold=30, news_sentiment_data=None, historical_prices=None):
    """
    Generates a trading signal based on on-chain data and technical indicators.
    Prioritizes anomalies and high-priority events.
    """
    if high_interest_wallets is None: high_interest_wallets = []
    if stablecoin_data is None: stablecoin_data = {}
    if velocity_data is None: velocity_data = {}

    # --- 1. Check for Transaction Velocity Anomaly ---
    current_count = velocity_data.get('current_count', 0)
    baseline_avg = velocity_data.get('baseline_avg', 0.0)
    if baseline_avg > 0 and current_count > (baseline_avg * velocity_threshold_multiplier):
        reason = f"Transaction velocity anomaly detected: {current_count} txns in last hour vs. baseline of {baseline_avg:.1f} (threshold: {baseline_avg * velocity_threshold_multiplier:.1f})."
        return {"signal": "VOLATILITY_WARNING", "symbol": symbol, "reason": reason}

    # --- 2. Check for High-Priority Stablecoin Inflow ---
    inflow = stablecoin_data.get('stablecoin_inflow_usd', 0)
    if inflow > stablecoin_threshold:
        reason = f"Large stablecoin inflow of ${inflow:,.2f} detected, exceeding threshold of ${stablecoin_threshold:,.2f}."
        return {"signal": "BUY", "symbol": symbol, "reason": "High-priority signal: " + reason}

    # --- 3. Check for High-Priority Signals from Watched Wallets ---
    base_symbol = symbol.replace('USDT', '')
    if whale_transactions:
        for tx in whale_transactions:
            tx_symbol = tx.get('symbol', '').upper()
            if tx_symbol != base_symbol:
                continue

            from_owner = tx.get('from', {}).get('owner', 'unknown')
            to_owner = tx.get('to', {}).get('owner', 'unknown')
            to_owner_type = tx.get('to', {}).get('owner_type', 'unknown')
            from_owner_type = tx.get('from', {}).get('owner_type', 'unknown')
            amount_usd = tx.get('amount_usd', 0)

            if from_owner in high_interest_wallets and to_owner_type == 'exchange':
                reason = f"High-interest wallet '{from_owner}' sent ${amount_usd:,.2f} of {symbol} to {to_owner}."
                return {"signal": "SELL", "symbol": symbol, "reason": "High-priority signal: " + reason}

            if to_owner in high_interest_wallets and from_owner_type == 'exchange':
                reason = f"High-interest wallet '{to_owner}' received ${amount_usd:,.2f} of {symbol} from {from_owner}."
                return {"signal": "BUY", "symbol": symbol, "reason": "High-priority signal: " + reason}

    # --- 4. Standard Technical and On-Chain Analysis ---
    current_price = market_data.get('current_price')
    sma = market_data.get('sma')
    rsi = market_data.get('rsi')

    log.debug(f"[{symbol}] Signal Check: Price={current_price}, SMA={sma}, RSI={rsi}")

    if current_price is None or sma is None or rsi is None:
        log.debug(f"[{symbol}] HOLD: Missing market data (price, SMA, or RSI).")
        return {"signal": "HOLD", "symbol": symbol, "reason": "Missing market data (price, SMA, or RSI)."}

    # --- Scoring System ---
    buy_score = 0
    sell_score = 0

    # Indicator 1: Trend (SMA)
    if current_price > sma:
        buy_score += 1
    elif current_price < sma:
        sell_score += 1

    # Indicator 2: Momentum (RSI)
    if rsi < rsi_oversold_threshold:
        buy_score += 1
    elif rsi > rsi_overbought_threshold:
        sell_score += 1

    # Indicator 3: On-Chain Flow (Whale Transactions)
    net_flow = 0
    symbol_whale_transactions = [tx for tx in whale_transactions if tx.get('symbol', '').upper() == base_symbol.upper()]
    if symbol_whale_transactions:
        exchange_inflow = sum(tx['amount_usd'] for tx in symbol_whale_transactions if tx.get('to', {}).get('owner_type', '') == 'exchange')
        exchange_outflow = sum(tx['amount_usd'] for tx in symbol_whale_transactions if tx.get('from', {}).get('owner_type', '') == 'exchange')
        net_flow = exchange_inflow - exchange_outflow
    
    log.debug(f"[{symbol}] Whale Net Flow: ${net_flow:,.2f}")

    if net_flow < 0: # More leaving exchanges than entering
        buy_score += 1
    elif net_flow > 0: # More entering exchanges than leaving
        sell_score += 1

    # Indicator 4: News Sentiment (Gemini preferred, VADER fallback)
    news_reason = ""
    if news_sentiment_data:
        gemini = news_sentiment_data.get('gemini_assessment')
        min_conf = news_sentiment_data.get('min_gemini_confidence', 0.6)
        used_gemini = False

        if gemini and gemini.get('confidence', 0) >= min_conf:
            direction = gemini.get('direction', 'neutral')
            confidence = gemini.get('confidence', 0)
            if direction == 'bullish':
                buy_score += 1
                news_reason = f", News: Gemini bullish ({confidence:.2f})"
                used_gemini = True
            elif direction == 'bearish':
                sell_score += 1
                news_reason = f", News: Gemini bearish ({confidence:.2f})"
                used_gemini = True

        if not used_gemini:
            avg_sentiment = news_sentiment_data.get('avg_sentiment_score', 0)
            buy_threshold = news_sentiment_data.get('sentiment_buy_threshold', 0.15)
            sell_threshold = news_sentiment_data.get('sentiment_sell_threshold', -0.15)
            if avg_sentiment > buy_threshold:
                buy_score += 1
                news_reason = f", News: VADER bullish ({avg_sentiment:.3f})"
            elif avg_sentiment < sell_threshold:
                sell_score += 1
                news_reason = f", News: VADER bearish ({avg_sentiment:.3f})"

    # Indicator 5: MACD Momentum
    macd_reason = ""
    if historical_prices and len(historical_prices) >= 26:
        macd = calculate_macd(historical_prices)
        if macd:
            histogram = macd['histogram']
            if histogram > 0:
                buy_score += 1
                macd_reason = f", MACD: bullish (hist {histogram:.4f})"
            elif histogram < 0:
                sell_score += 1
                macd_reason = f", MACD: bearish (hist {histogram:.4f})"

    # Indicator 6: Bollinger Position
    bollinger_reason = ""
    if historical_prices and len(historical_prices) >= 20:
        bb = calculate_bollinger_bands(historical_prices)
        if bb:
            if current_price < bb['lower_band']:
                buy_score += 1
                bollinger_reason = f", BB: oversold (price < {bb['lower_band']:,.2f})"
            elif current_price > bb['upper_band']:
                sell_score += 1
                bollinger_reason = f", BB: overbought (price > {bb['upper_band']:,.2f})"

    # --- Signal Generation ---
    reason = f"Price: ${current_price:,.2f}, SMA: ${sma:,.2f}, RSI: {rsi:.2f}, Whale Net Flow: ${net_flow:,.2f}{news_reason}{macd_reason}{bollinger_reason}. Buy Score: {buy_score}, Sell Score: {sell_score}."

    if buy_score >= 2 and buy_score > sell_score:
        log.info(f"[{symbol}] BUY signal generated. {reason}")
        return {"signal": "BUY", "symbol": symbol, "reason": reason, "current_price": current_price}

    if sell_score >= 2 and sell_score > buy_score:
        log.info(f"[{symbol}] SELL signal generated. {reason}")
        return {"signal": "SELL", "symbol": symbol, "reason": reason, "current_price": current_price}

    log.debug(f"[{symbol}] HOLD: No strong signal detected. {reason}")
    return {"signal": "HOLD", "symbol": symbol, "reason": "No strong signal detected. " + reason}