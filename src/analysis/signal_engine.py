import pandas as pd
from src.logger import log
from src.analysis.technical_indicators import calculate_macd, calculate_bollinger_bands

def generate_signal(symbol, whale_transactions, market_data, high_interest_wallets=None, stablecoin_data=None, stablecoin_threshold=100000000, velocity_data=None, velocity_threshold_multiplier=5.0, rsi_overbought_threshold=70, rsi_oversold_threshold=30, news_sentiment_data=None, historical_prices=None, volume_data=None, order_book_data=None, signal_threshold=3, signal_mode="scoring", sentiment_config=None):
    """
    Generates a trading signal based on on-chain data and technical indicators.
    Prioritizes anomalies and high-priority events.

    Args:
        signal_mode: "scoring" (legacy 8-indicator system) or "sentiment" (Gemini-first).
        sentiment_config: dict with min_gemini_confidence, min_vader_score,
                          rsi_buy_veto_threshold, rsi_sell_veto_threshold.
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

    # --- 4. Route to signal mode ---
    if signal_mode == "sentiment":
        return _generate_sentiment_signal(
            symbol=symbol,
            market_data=market_data,
            whale_transactions=whale_transactions,
            news_sentiment_data=news_sentiment_data,
            sentiment_config=sentiment_config or {},
        )
    else:
        return _generate_scoring_signal(
            symbol=symbol,
            whale_transactions=whale_transactions,
            market_data=market_data,
            rsi_overbought_threshold=rsi_overbought_threshold,
            rsi_oversold_threshold=rsi_oversold_threshold,
            news_sentiment_data=news_sentiment_data,
            historical_prices=historical_prices,
            volume_data=volume_data,
            order_book_data=order_book_data,
            signal_threshold=signal_threshold,
        )


def _generate_scoring_signal(symbol, whale_transactions, market_data,
                              rsi_overbought_threshold=70, rsi_oversold_threshold=30,
                              news_sentiment_data=None, historical_prices=None,
                              volume_data=None, order_book_data=None, signal_threshold=3):
    """Legacy 8-indicator scoring system. Zero behavior change from original."""
    current_price = market_data.get('current_price')
    sma = market_data.get('sma')
    rsi = market_data.get('rsi')
    base_symbol = symbol.replace('USDT', '')

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

    # Indicator 7: Volume (24hr stats from Binance)
    volume_reason = ""
    if volume_data is None:
        volume_data = {}
    vol_change = volume_data.get('price_change_percent', 0)
    vol_current = volume_data.get('volume', 0)
    vol_avg = volume_data.get('avg_volume', 0)
    vol_spike_multiplier = volume_data.get('volume_spike_multiplier', 1.5)

    if vol_current > 0 and vol_avg > 0 and vol_current > vol_avg * vol_spike_multiplier:
        if vol_change > 0:
            buy_score += 1
            volume_reason = f", Volume: spike ({vol_current:,.0f} > {vol_avg * vol_spike_multiplier:,.0f}) with price up {vol_change:.1f}%"
        elif vol_change < 0:
            sell_score += 1
            volume_reason = f", Volume: spike ({vol_current:,.0f} > {vol_avg * vol_spike_multiplier:,.0f}) with price down {vol_change:.1f}%"

    # Indicator 8: Order Book Depth Imbalance
    orderbook_reason = ""
    if order_book_data is None:
        order_book_data = {}
    bid_ask_ratio = order_book_data.get('bid_ask_ratio', 0)

    if bid_ask_ratio > 0:
        if bid_ask_ratio > 1.5:
            buy_score += 1
            orderbook_reason = f", OrderBook: bid-heavy ({bid_ask_ratio:.2f} ratio, buying pressure)"
        elif bid_ask_ratio < 0.67:
            sell_score += 1
            orderbook_reason = f", OrderBook: ask-heavy ({bid_ask_ratio:.2f} ratio, selling pressure)"

    # --- Signal Generation ---
    reason = f"Price: ${current_price:,.2f}, SMA: ${sma:,.2f}, RSI: {rsi:.2f}, Whale Net Flow: ${net_flow:,.2f}{news_reason}{macd_reason}{bollinger_reason}{volume_reason}{orderbook_reason}. Buy Score: {buy_score}, Sell Score: {sell_score}."

    if buy_score >= signal_threshold and buy_score > sell_score:
        log.info(f"[{symbol}] BUY signal generated. {reason}")
        return {"signal": "BUY", "symbol": symbol, "reason": reason, "current_price": current_price}

    if sell_score >= signal_threshold and sell_score > buy_score:
        log.info(f"[{symbol}] SELL signal generated. {reason}")
        return {"signal": "SELL", "symbol": symbol, "reason": reason, "current_price": current_price}

    log.debug(f"[{symbol}] HOLD: No strong signal detected. {reason}")
    return {"signal": "HOLD", "symbol": symbol, "reason": "No strong signal detected. " + reason}


def _generate_sentiment_signal(symbol, market_data, whale_transactions=None,
                                news_sentiment_data=None, sentiment_config=None):
    """
    Sentiment-first signal: Gemini is the primary trigger, with SMA trend filter
    and RSI sanity check as gatekeepers.

    Flow:
        1. Gemini direction + confidence >= threshold (primary trigger)
           - Fallback: VADER score >= threshold
        2. SMA trend must agree (don't trade against the trend)
        3. RSI must not veto (don't buy overbought, don't sell oversold)
    """
    if sentiment_config is None:
        sentiment_config = {}

    current_price = market_data.get('current_price')
    sma = market_data.get('sma')
    rsi = market_data.get('rsi')

    log.debug(f"[{symbol}] Sentiment Signal Check: Price={current_price}, SMA={sma}, RSI={rsi}")

    if current_price is None or sma is None or rsi is None:
        log.debug(f"[{symbol}] HOLD: Missing market data (price, SMA, or RSI).")
        return {"signal": "HOLD", "symbol": symbol, "reason": "Missing market data (price, SMA, or RSI)."}

    min_gemini_conf = sentiment_config.get('min_gemini_confidence', 0.7)
    min_vader_score = sentiment_config.get('min_vader_score', 0.3)
    rsi_buy_veto = sentiment_config.get('rsi_buy_veto_threshold', 75)
    rsi_sell_veto = sentiment_config.get('rsi_sell_veto_threshold', 25)

    # --- Step 1: Sentiment trigger (Gemini primary, VADER fallback) ---
    direction = None
    sentiment_reason = ""

    if news_sentiment_data:
        gemini = news_sentiment_data.get('gemini_assessment')

        if gemini and gemini.get('confidence', 0) >= min_gemini_conf:
            g_direction = gemini.get('direction', 'neutral')
            g_confidence = gemini.get('confidence', 0)
            g_reasoning = gemini.get('reasoning', '')
            if g_direction in ('bullish', 'bearish'):
                direction = g_direction
                sentiment_reason = f"Gemini {g_direction} ({g_confidence:.2f}): {g_reasoning}"

        # VADER fallback
        if direction is None:
            avg_sentiment = news_sentiment_data.get('avg_sentiment_score', 0)
            if avg_sentiment >= min_vader_score:
                direction = 'bullish'
                sentiment_reason = f"VADER bullish ({avg_sentiment:.3f})"
            elif avg_sentiment <= -min_vader_score:
                direction = 'bearish'
                sentiment_reason = f"VADER bearish ({avg_sentiment:.3f})"

    if direction is None:
        reason = f"No sentiment trigger. Price: ${current_price:,.2f}, SMA: ${sma:,.2f}, RSI: {rsi:.2f}."
        log.debug(f"[{symbol}] HOLD: {reason}")
        return {"signal": "HOLD", "symbol": symbol, "reason": reason}

    # --- Step 2: SMA trend filter (don't trade against the trend) ---
    if direction == 'bullish' and current_price < sma:
        reason = f"Sentiment bullish but price ${current_price:,.2f} < SMA ${sma:,.2f} (downtrend). {sentiment_reason}."
        log.debug(f"[{symbol}] HOLD: {reason}")
        return {"signal": "HOLD", "symbol": symbol, "reason": reason}

    if direction == 'bearish' and current_price > sma:
        reason = f"Sentiment bearish but price ${current_price:,.2f} > SMA ${sma:,.2f} (uptrend). {sentiment_reason}."
        log.debug(f"[{symbol}] HOLD: {reason}")
        return {"signal": "HOLD", "symbol": symbol, "reason": reason}

    # --- Step 3: RSI veto (don't buy overbought, don't sell oversold) ---
    if direction == 'bullish' and rsi > rsi_buy_veto:
        reason = f"Sentiment bullish but RSI {rsi:.2f} > {rsi_buy_veto} (overbought veto). {sentiment_reason}."
        log.debug(f"[{symbol}] HOLD: {reason}")
        return {"signal": "HOLD", "symbol": symbol, "reason": reason}

    if direction == 'bearish' and rsi < rsi_sell_veto:
        reason = f"Sentiment bearish but RSI {rsi:.2f} < {rsi_sell_veto} (oversold veto). {sentiment_reason}."
        log.debug(f"[{symbol}] HOLD: {reason}")
        return {"signal": "HOLD", "symbol": symbol, "reason": reason}

    # --- All gates passed: generate signal ---
    signal_type = "BUY" if direction == 'bullish' else "SELL"
    reason = f"{sentiment_reason}. Price: ${current_price:,.2f}, SMA: ${sma:,.2f}, RSI: {rsi:.2f}."
    log.info(f"[{symbol}] {signal_type} signal (sentiment mode). {reason}")
    return {"signal": signal_type, "symbol": symbol, "reason": reason, "current_price": current_price}
