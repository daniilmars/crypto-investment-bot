from src.logger import log


def generate_stock_signal(symbol, market_data, volume_data=None, fundamental_data=None,
                          rsi_overbought_threshold=70, rsi_oversold_threshold=30,
                          pe_ratio_buy_threshold=25, pe_ratio_sell_threshold=40,
                          earnings_growth_sell_threshold=-10,
                          volume_spike_multiplier=1.5,
                          news_sentiment_data=None):
    """
    Generates a BUY/SELL/HOLD signal for a stock using a 4-indicator scoring system.
    Requires 2+ indicators to agree for a BUY or SELL signal.

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

    # --- Indicator 5: News Sentiment ---
    if news_sentiment_data:
        claude_assessment = news_sentiment_data.get('claude_assessment')
        min_confidence = news_sentiment_data.get('min_claude_confidence', 0.6)

        if claude_assessment and claude_assessment.get('confidence', 0) >= min_confidence:
            direction = claude_assessment.get('direction', 'neutral')
            if direction == 'bullish':
                buy_score += 1
                reasons.append(f"News: Claude bullish ({claude_assessment.get('confidence', 0):.1f})")
            elif direction == 'bearish':
                sell_score += 1
                reasons.append(f"News: Claude bearish ({claude_assessment.get('confidence', 0):.1f})")
        else:
            avg_sentiment = news_sentiment_data.get('avg_sentiment_score', 0)
            buy_threshold = news_sentiment_data.get('sentiment_buy_threshold', 0.15)
            sell_threshold = news_sentiment_data.get('sentiment_sell_threshold', -0.15)
            if avg_sentiment > buy_threshold:
                buy_score += 1
                reasons.append(f"News: VADER bullish ({avg_sentiment:.3f})")
            elif avg_sentiment < sell_threshold:
                sell_score += 1
                reasons.append(f"News: VADER bearish ({avg_sentiment:.3f})")

    # --- Signal Generation ---
    reason_str = "; ".join(reasons) if reasons else "No indicators triggered"
    reason_str += f". Buy Score: {buy_score}, Sell Score: {sell_score}."

    if buy_score >= 2:
        log.info(f"[{symbol}] Stock BUY signal. {reason_str}")
        return {"signal": "BUY", "symbol": symbol, "reason": reason_str,
                "current_price": current_price}

    if sell_score >= 2:
        log.info(f"[{symbol}] Stock SELL signal. {reason_str}")
        return {"signal": "SELL", "symbol": symbol, "reason": reason_str,
                "current_price": current_price}

    log.info(f"[{symbol}] Stock HOLD. {reason_str}")
    return {"signal": "HOLD", "symbol": symbol, "reason": reason_str,
            "current_price": current_price}
