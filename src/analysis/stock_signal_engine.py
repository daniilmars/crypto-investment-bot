from src.logger import log
from src.analysis.technical_indicators import calculate_macd, calculate_bollinger_bands


def generate_stock_signal(symbol, market_data, volume_data=None, fundamental_data=None,
                          rsi_overbought_threshold=70, rsi_oversold_threshold=30,
                          pe_ratio_buy_threshold=25, pe_ratio_sell_threshold=40,
                          earnings_growth_sell_threshold=-10,
                          volume_spike_multiplier=1.5,
                          news_sentiment_data=None,
                          historical_prices=None,
                          signal_threshold=3,
                          signal_mode="scoring",
                          sentiment_config=None):
    """
    Generates a BUY/SELL/HOLD signal for a stock.

    Args:
        signal_mode: "scoring" (legacy multi-indicator) or "sentiment" (Gemini-first).
        sentiment_config: dict with min_gemini_confidence, min_vader_score,
                          rsi_buy_veto_threshold, rsi_sell_veto_threshold,
                          pe_buy_veto_threshold.
    """
    if signal_mode == "sentiment":
        return _generate_stock_sentiment_signal(
            symbol=symbol,
            market_data=market_data,
            fundamental_data=fundamental_data or {},
            news_sentiment_data=news_sentiment_data,
            sentiment_config=sentiment_config or {},
        )
    else:
        return _generate_stock_scoring_signal(
            symbol=symbol,
            market_data=market_data,
            volume_data=volume_data,
            fundamental_data=fundamental_data,
            rsi_overbought_threshold=rsi_overbought_threshold,
            rsi_oversold_threshold=rsi_oversold_threshold,
            pe_ratio_buy_threshold=pe_ratio_buy_threshold,
            pe_ratio_sell_threshold=pe_ratio_sell_threshold,
            earnings_growth_sell_threshold=earnings_growth_sell_threshold,
            volume_spike_multiplier=volume_spike_multiplier,
            news_sentiment_data=news_sentiment_data,
            historical_prices=historical_prices,
            signal_threshold=signal_threshold,
        )


def _generate_stock_scoring_signal(symbol, market_data, volume_data=None, fundamental_data=None,
                                    rsi_overbought_threshold=70, rsi_oversold_threshold=30,
                                    pe_ratio_buy_threshold=25, pe_ratio_sell_threshold=40,
                                    earnings_growth_sell_threshold=-10,
                                    volume_spike_multiplier=1.5,
                                    news_sentiment_data=None,
                                    historical_prices=None,
                                    signal_threshold=3):
    """
    Legacy multi-indicator scoring system for stocks. Zero behavior change from original.

    Scoring:
    | Indicator    | +1 BUY                              | +1 SELL                                  |
    |-------------|--------------------------------------|------------------------------------------|
    | SMA Trend   | price > SMA20                       | price < SMA20                            |
    | RSI         | RSI < oversold threshold             | RSI > overbought threshold               |
    | Volume      | vol > multiplier * avg AND price up  | vol > multiplier * avg AND price down    |
    | Fundamentals| P/E < buy threshold AND growth > 0   | P/E > sell threshold OR growth < -10%    |

    Returns:
        dict: {"signal": "BUY|SELL|HOLD", "symbol": str, "reason": str, "current_price": float}
    """
    if volume_data is None:
        volume_data = {}
    if fundamental_data is None:
        fundamental_data = {}

    current_price = market_data.get('current_price')
    sma = market_data.get('sma')
    rsi = market_data.get('rsi')

    if current_price is None:
        return {"signal": "HOLD", "symbol": symbol, "reason": "Missing current price data.",
                "current_price": 0}

    buy_score = 0
    sell_score = 0
    reasons = []

    # --- Indicator 1: SMA Trend ---
    if sma is not None:
        if current_price > sma:
            buy_score += 1
            reasons.append(f"Price ${current_price:,.2f} > SMA ${sma:,.2f}")
        elif current_price < sma:
            sell_score += 1
            reasons.append(f"Price ${current_price:,.2f} < SMA ${sma:,.2f}")

    # --- Indicator 2: RSI Momentum ---
    if rsi is not None:
        if rsi < rsi_oversold_threshold:
            buy_score += 1
            reasons.append(f"RSI {rsi:.1f} < {rsi_oversold_threshold} (oversold)")
        elif rsi > rsi_overbought_threshold:
            sell_score += 1
            reasons.append(f"RSI {rsi:.1f} > {rsi_overbought_threshold} (overbought)")

    # --- Indicator 3: Volume Spike ---
    current_volume = volume_data.get('current_volume')
    avg_volume = volume_data.get('avg_volume')
    price_change = volume_data.get('price_change_percent', 0)

    if current_volume is not None and avg_volume is not None and avg_volume > 0:
        if current_volume > avg_volume * volume_spike_multiplier:
            if price_change > 0:
                buy_score += 1
                reasons.append(f"Volume spike ({current_volume:,.0f} > {avg_volume * volume_spike_multiplier:,.0f}) with price up")
            elif price_change < 0:
                sell_score += 1
                reasons.append(f"Volume spike ({current_volume:,.0f} > {avg_volume * volume_spike_multiplier:,.0f}) with price down")

    # --- Indicator 4: Fundamentals ---
    pe_ratio = fundamental_data.get('pe_ratio')
    earnings_growth = fundamental_data.get('earnings_growth')

    if pe_ratio is not None:
        # Sell: P/E > sell threshold OR earnings growth very negative
        if pe_ratio > pe_ratio_sell_threshold:
            sell_score += 1
            reasons.append(f"P/E {pe_ratio:.1f} > {pe_ratio_sell_threshold} (overvalued)")
        elif earnings_growth is not None and earnings_growth < earnings_growth_sell_threshold:
            sell_score += 1
            reasons.append(f"Earnings growth {earnings_growth:.1f}% < {earnings_growth_sell_threshold}%")
        # Buy: P/E < buy threshold AND earnings growth positive
        elif pe_ratio < pe_ratio_buy_threshold and earnings_growth is not None and earnings_growth > 0:
            buy_score += 1
            reasons.append(f"P/E {pe_ratio:.1f} < {pe_ratio_buy_threshold} with positive earnings growth {earnings_growth:.1f}%")

    # --- Indicator 4b: Revenue Growth ---
    revenue_growth = fundamental_data.get('revenue_growth')
    if revenue_growth is not None:
        if revenue_growth > 0.10:
            buy_score += 1
            reasons.append(f"Revenue growth {revenue_growth * 100:.1f}% (strong)")
        elif revenue_growth < -0.05:
            sell_score += 1
            reasons.append(f"Revenue growth {revenue_growth * 100:.1f}% (declining)")

    # --- Indicator 4c: Beta (Risk-Adjusted) ---
    beta = fundamental_data.get('beta')
    if beta is not None:
        # High beta in an overbought market amplifies sell signal
        if beta > 1.5 and rsi is not None and rsi > rsi_overbought_threshold:
            sell_score += 1
            reasons.append(f"High beta {beta:.2f} with overbought RSI (volatile downside risk)")
        # Low beta with good fundamentals is defensive buy
        elif beta < 0.8 and pe_ratio is not None and pe_ratio < pe_ratio_buy_threshold:
            buy_score += 1
            reasons.append(f"Low beta {beta:.2f} with reasonable P/E (defensive value)")

    # --- Indicator 5: News Sentiment (Gemini preferred, VADER fallback) ---
    if news_sentiment_data:
        gemini = news_sentiment_data.get('gemini_assessment')
        min_conf = news_sentiment_data.get('min_gemini_confidence', 0.6)
        used_gemini = False

        if gemini and gemini.get('confidence', 0) >= min_conf:
            direction = gemini.get('direction', 'neutral')
            confidence = gemini.get('confidence', 0)
            if direction == 'bullish':
                buy_score += 1
                reasons.append(f"News: Gemini bullish ({confidence:.2f})")
                used_gemini = True
            elif direction == 'bearish':
                sell_score += 1
                reasons.append(f"News: Gemini bearish ({confidence:.2f})")
                used_gemini = True

        if not used_gemini:
            avg_sentiment = news_sentiment_data.get('avg_sentiment_score', 0)
            buy_threshold = news_sentiment_data.get('sentiment_buy_threshold', 0.15)
            sell_threshold = news_sentiment_data.get('sentiment_sell_threshold', -0.15)
            if avg_sentiment > buy_threshold:
                buy_score += 1
                reasons.append(f"News: VADER bullish ({avg_sentiment:.3f})")
            elif avg_sentiment < sell_threshold:
                sell_score += 1
                reasons.append(f"News: VADER bearish ({avg_sentiment:.3f})")

    # --- Indicator 6: MACD Momentum ---
    if historical_prices and len(historical_prices) >= 26:
        macd = calculate_macd(historical_prices)
        if macd:
            histogram = macd['histogram']
            if histogram > 0:
                buy_score += 1
                reasons.append(f"MACD bullish (hist {histogram:.4f})")
            elif histogram < 0:
                sell_score += 1
                reasons.append(f"MACD bearish (hist {histogram:.4f})")

    # --- Indicator 7: Bollinger Position ---
    if historical_prices and len(historical_prices) >= 20:
        bb = calculate_bollinger_bands(historical_prices)
        if bb and current_price is not None:
            if current_price < bb['lower_band']:
                buy_score += 1
                reasons.append(f"BB oversold (price < {bb['lower_band']:,.2f})")
            elif current_price > bb['upper_band']:
                sell_score += 1
                reasons.append(f"BB overbought (price > {bb['upper_band']:,.2f})")

    # --- Signal Generation ---
    reason_str = "; ".join(reasons) if reasons else "No indicators triggered"
    reason_str += f". Buy Score: {buy_score}, Sell Score: {sell_score}."

    if buy_score >= signal_threshold and buy_score > sell_score:
        log.info(f"[{symbol}] Stock BUY signal. {reason_str}")
        return {"signal": "BUY", "symbol": symbol, "reason": reason_str,
                "current_price": current_price}

    if sell_score >= signal_threshold and sell_score > buy_score:
        log.info(f"[{symbol}] Stock SELL signal. {reason_str}")
        return {"signal": "SELL", "symbol": symbol, "reason": reason_str,
                "current_price": current_price}

    log.info(f"[{symbol}] Stock HOLD. {reason_str}")
    return {"signal": "HOLD", "symbol": symbol, "reason": reason_str,
            "current_price": current_price}


def _generate_stock_sentiment_signal(symbol, market_data, fundamental_data=None,
                                      news_sentiment_data=None, sentiment_config=None):
    """
    Sentiment-first signal for stocks: Gemini is the primary trigger, with SMA trend
    filter, RSI sanity check, and P/E veto as gatekeepers.

    Flow:
        1. Gemini direction + confidence >= threshold (primary trigger)
           - Fallback: VADER score >= threshold
        2. SMA trend must agree (don't trade against the trend)
        3. RSI must not veto (don't buy overbought, don't sell oversold)
        4. P/E must not veto BUY (don't buy overvalued stocks)
    """
    if sentiment_config is None:
        sentiment_config = {}
    if fundamental_data is None:
        fundamental_data = {}

    current_price = market_data.get('current_price')
    sma = market_data.get('sma')
    rsi = market_data.get('rsi')

    log.debug(f"[{symbol}] Stock Sentiment Signal Check: Price={current_price}, SMA={sma}, RSI={rsi}")

    if current_price is None:
        return {"signal": "HOLD", "symbol": symbol, "reason": "Missing current price data.",
                "current_price": 0}

    if sma is None or rsi is None:
        return {"signal": "HOLD", "symbol": symbol, "reason": "Missing market data (SMA or RSI).",
                "current_price": current_price}

    min_gemini_conf = sentiment_config.get('min_gemini_confidence', 0.7)
    min_vader_score = sentiment_config.get('min_vader_score', 0.3)
    rsi_buy_veto = sentiment_config.get('rsi_buy_veto_threshold', 75)
    rsi_sell_veto = sentiment_config.get('rsi_sell_veto_threshold', 25)
    pe_buy_veto = sentiment_config.get('pe_buy_veto_threshold', 40)

    # --- Step 1: Sentiment trigger (Gemini primary, VADER fallback) ---
    direction = None
    sentiment_reason = ""

    if news_sentiment_data:
        gemini = news_sentiment_data.get('gemini_assessment')

        if gemini:
            g_direction = gemini.get('direction', 'neutral')
            g_confidence = gemini.get('confidence', 0)
            g_reasoning = gemini.get('reasoning', '')

            # Freshness modulation: stale catalysts get reduced confidence
            freshness = gemini.get('catalyst_freshness', 'none')
            freshness_mult = {'breaking': 1.0, 'recent': 0.8,
                              'stale': 0.3, 'none': 0.5}.get(freshness, 0.5)
            g_confidence *= freshness_mult

            if g_confidence >= min_gemini_conf and g_direction in ('bullish', 'bearish'):
                direction = g_direction
                sentiment_reason = (f"Gemini {g_direction} ({g_confidence:.2f}, "
                                    f"freshness={freshness}): {g_reasoning}")

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
        log.debug(f"[{symbol}] Stock HOLD: {reason}")
        return {"signal": "HOLD", "symbol": symbol, "reason": reason,
                "current_price": current_price}

    # --- Step 2: SMA trend filter (don't trade against the trend) ---
    if direction == 'bullish' and current_price < sma:
        reason = f"Sentiment bullish but price ${current_price:,.2f} < SMA ${sma:,.2f} (downtrend). {sentiment_reason}."
        log.debug(f"[{symbol}] Stock HOLD: {reason}")
        return {"signal": "HOLD", "symbol": symbol, "reason": reason,
                "current_price": current_price}

    if direction == 'bearish' and current_price > sma:
        reason = f"Sentiment bearish but price ${current_price:,.2f} > SMA ${sma:,.2f} (uptrend). {sentiment_reason}."
        log.debug(f"[{symbol}] Stock HOLD: {reason}")
        return {"signal": "HOLD", "symbol": symbol, "reason": reason,
                "current_price": current_price}

    # --- Step 3: RSI veto (don't buy overbought, don't sell oversold) ---
    if direction == 'bullish' and rsi > rsi_buy_veto:
        reason = f"Sentiment bullish but RSI {rsi:.2f} > {rsi_buy_veto} (overbought veto). {sentiment_reason}."
        log.debug(f"[{symbol}] Stock HOLD: {reason}")
        return {"signal": "HOLD", "symbol": symbol, "reason": reason,
                "current_price": current_price}

    if direction == 'bearish' and rsi < rsi_sell_veto:
        reason = f"Sentiment bearish but RSI {rsi:.2f} < {rsi_sell_veto} (oversold veto). {sentiment_reason}."
        log.debug(f"[{symbol}] Stock HOLD: {reason}")
        return {"signal": "HOLD", "symbol": symbol, "reason": reason,
                "current_price": current_price}

    # --- Step 4: P/E veto for BUY (don't buy overvalued stocks) ---
    pe_ratio = fundamental_data.get('pe_ratio')
    if direction == 'bullish' and pe_ratio is not None and pe_ratio > pe_buy_veto:
        reason = f"Sentiment bullish but P/E {pe_ratio:.1f} > {pe_buy_veto} (overvalued veto). {sentiment_reason}."
        log.debug(f"[{symbol}] Stock HOLD: {reason}")
        return {"signal": "HOLD", "symbol": symbol, "reason": reason,
                "current_price": current_price}

    # --- All gates passed: generate signal ---
    signal_type = "BUY" if direction == 'bullish' else "SELL"
    reason = f"{sentiment_reason}. Price: ${current_price:,.2f}, SMA: ${sma:,.2f}, RSI: {rsi:.2f}."
    log.info(f"[{symbol}] Stock {signal_type} signal (sentiment mode). {reason}")
    return {"signal": signal_type, "symbol": symbol, "reason": reason,
            "current_price": current_price}
