from src.logger import log
from src.analysis.technical_indicators import calculate_macd, calculate_bollinger_bands
from src.analysis.trend_alignment import compute_trend_alignment

def generate_signal(symbol, market_data, news_sentiment_data=None,
                    signal_mode="scoring", sentiment_config=None,
                    rsi_overbought_threshold=70, rsi_oversold_threshold=30,
                    historical_prices=None, volume_data=None,
                    order_book_data=None, signal_threshold=3):
    """
    Generates a trading signal.

    Args:
        signal_mode: "scoring" (legacy 7-indicator system) or "sentiment" (Gemini-first).
        sentiment_config: dict with min_gemini_confidence,
                          rsi_buy_veto_threshold, rsi_sell_veto_threshold.
    """
    if signal_mode == "sentiment":
        # Check if Gemini data is actually available; if not, fall back
        # to scoring mode so we don't silently HOLD everything when
        # Vertex AI is down.
        has_gemini = (news_sentiment_data
                      and news_sentiment_data.get('gemini_assessment')
                      and news_sentiment_data['gemini_assessment'].get('confidence') is not None)
        if not has_gemini:
            log.info(f"[{symbol}] No Gemini assessment available — "
                     f"falling back to scoring mode.")
            return _generate_scoring_signal(
                symbol=symbol,
                market_data=market_data,
                rsi_overbought_threshold=rsi_overbought_threshold,
                rsi_oversold_threshold=rsi_oversold_threshold,
                historical_prices=historical_prices,
                news_sentiment_data=news_sentiment_data,
                volume_data=volume_data,
                order_book_data=order_book_data,
                signal_threshold=signal_threshold,
            )
        return _generate_sentiment_signal(
            symbol=symbol,
            market_data=market_data,
            news_sentiment_data=news_sentiment_data,
            sentiment_config=sentiment_config or {},
            historical_prices=historical_prices,
        )
    else:
        return _generate_scoring_signal(
            symbol=symbol,
            market_data=market_data,
            rsi_overbought_threshold=rsi_overbought_threshold,
            rsi_oversold_threshold=rsi_oversold_threshold,
            news_sentiment_data=news_sentiment_data,
            historical_prices=historical_prices,
            volume_data=volume_data,
            order_book_data=order_book_data,
            signal_threshold=signal_threshold,
        )


def _generate_scoring_signal(symbol, market_data,
                              rsi_overbought_threshold=70, rsi_oversold_threshold=30,
                              news_sentiment_data=None, historical_prices=None,
                              volume_data=None, order_book_data=None, signal_threshold=3):
    """Legacy 7-indicator scoring system."""
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

    # Indicator 3: News Sentiment (Gemini only)
    news_reason = ""
    if news_sentiment_data:
        gemini = news_sentiment_data.get('gemini_assessment')
        min_conf = news_sentiment_data.get('min_gemini_confidence', 0.6)

        if gemini and gemini.get('confidence', 0) >= min_conf:
            direction = gemini.get('direction', 'neutral')
            confidence = gemini.get('confidence', 0)
            if direction == 'bullish':
                buy_score += 1
                news_reason = f", News: Gemini bullish ({confidence:.2f})"
            elif direction == 'bearish':
                sell_score += 1
                news_reason = f", News: Gemini bearish ({confidence:.2f})"

    # Indicator 4: MACD Momentum
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

    # Indicator 5: Bollinger Position
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

    # Indicator 6: Volume (24hr stats from Binance)
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

    # Indicator 7: Order Book Depth Imbalance
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
    reason = f"Price: ${current_price:,.2f}, SMA: ${sma:,.2f}, RSI: {rsi:.2f}{news_reason}{macd_reason}{bollinger_reason}{volume_reason}{orderbook_reason}. Buy Score: {buy_score}, Sell Score: {sell_score}."

    if buy_score >= signal_threshold and buy_score > sell_score:
        # Remap strength: threshold → 0.5, max(7) → 1.0 (compatible with quality gate)
        strength = 0.5 + 0.5 * (buy_score - signal_threshold) / (7 - signal_threshold)
        log.info(f"[{symbol}] BUY signal generated. {reason}")
        return {"signal": "BUY", "symbol": symbol, "reason": reason, "current_price": current_price,
                "signal_strength": strength}

    if sell_score >= signal_threshold and sell_score > buy_score:
        strength = 0.5 + 0.5 * (sell_score - signal_threshold) / (7 - signal_threshold)
        log.info(f"[{symbol}] SELL signal generated. {reason}")
        return {"signal": "SELL", "symbol": symbol, "reason": reason, "current_price": current_price,
                "signal_strength": strength}

    log.debug(f"[{symbol}] HOLD: No strong signal detected. {reason}")
    return {"signal": "HOLD", "symbol": symbol, "reason": "No strong signal detected. " + reason}


def _generate_sentiment_signal(symbol, market_data,
                                news_sentiment_data=None, sentiment_config=None,
                                historical_prices=None):
    """
    Sentiment-first signal: Gemini is the primary trigger, with SMA trend filter
    and RSI sanity check as gatekeepers.

    Flow:
        1. Gemini direction + confidence >= threshold (primary trigger)
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
    rsi_buy_veto = sentiment_config.get('rsi_buy_veto_threshold', 75)
    rsi_sell_veto = sentiment_config.get('rsi_sell_veto_threshold', 25)

    # Sector conviction adjustment: ±conviction_influence_pct at full conviction
    sector_conv = sentiment_config.get('sector_conviction', 0.0)
    conviction_influence = sentiment_config.get('conviction_influence_pct', 0.10)

    # --- Step 1: Sentiment trigger (Gemini only) ---
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

            # Direction-aware threshold: bullish sector lowers BUY threshold,
            # raises SELL threshold (and vice versa for bearish sectors)
            conviction_adj = sector_conv * conviction_influence
            if g_direction == 'bullish':
                effective_conf = max(0.35, min(0.85, min_gemini_conf - conviction_adj))
            elif g_direction == 'bearish':
                effective_conf = max(0.35, min(0.85, min_gemini_conf + conviction_adj))
            else:
                effective_conf = min_gemini_conf

            if g_confidence >= effective_conf and g_direction in ('bullish', 'bearish'):
                direction = g_direction
                sentiment_reason = (f"Gemini {g_direction} ({g_confidence:.2f}, "
                                    f"freshness={freshness}): {g_reasoning}")

    if direction is None:
        reason = f"No sentiment trigger. Price: ${current_price:,.2f}, SMA: ${sma:,.2f}, RSI: {rsi:.2f}."
        log.debug(f"[{symbol}] HOLD: {reason}")
        return {"signal": "HOLD", "symbol": symbol, "reason": reason}

    # --- Step 2: SMA trend filter (soft override for high-conviction events) ---
    sma_override_applied = False
    sma_cfg = sentiment_config.get('sma_override', {})

    if direction == 'bullish' and current_price < sma:
        can_override = (
            sma_cfg.get('enabled', False)
            and g_confidence >= sma_cfg.get('min_confidence', 0.75)
            and freshness == sma_cfg.get('required_freshness', 'breaking')
        )
        if can_override:
            sma_override_applied = True
            log.info(f"[{symbol}] SMA override: price ${current_price:,.2f} < SMA "
                     f"${sma:,.2f} but Gemini conf {g_confidence:.2f} "
                     f"(freshness={freshness}) exceeds threshold")
        else:
            reason = (f"Sentiment bullish but price ${current_price:,.2f} < SMA "
                      f"${sma:,.2f} (downtrend). {sentiment_reason}.")
            log.debug(f"[{symbol}] HOLD: {reason}")
            return {"signal": "HOLD", "symbol": symbol, "reason": reason}

    if direction == 'bearish' and current_price > sma:
        reason = (f"Sentiment bearish but price ${current_price:,.2f} > SMA "
                  f"${sma:,.2f} (uptrend). {sentiment_reason}.")
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

    # --- Step 4: ADX trend strength filter (optional) ---
    adx_min = sentiment_config.get('adx_min_threshold')
    if adx_min is not None and historical_prices and len(historical_prices) >= 29:
        from src.analysis.technical_indicators import calculate_adx_from_closes
        adx = calculate_adx_from_closes(historical_prices, period=14)
        if adx is not None and adx < adx_min:
            reason = (f"ADX {adx:.1f} < {adx_min} (weak trend). {sentiment_reason}. "
                      f"Price: ${current_price:,.2f}, SMA: ${sma:,.2f}, RSI: {rsi:.2f}.")
            log.debug(f"[{symbol}] HOLD: {reason}")
            return {"signal": "HOLD", "symbol": symbol, "reason": reason}

    # --- All gates passed: generate signal ---
    signal_type = "BUY" if direction == 'bullish' else "SELL"
    reason = f"{sentiment_reason}. Price: ${current_price:,.2f}, SMA: ${sma:,.2f}, RSI: {rsi:.2f}."
    log.info(f"[{symbol}] {signal_type} signal (sentiment mode). {reason}")

    # Signal strength: use effective Gemini confidence (after freshness modulation)
    strength = 0.5  # default for VADER fallback
    if news_sentiment_data:
        gemini = news_sentiment_data.get('gemini_assessment')
        if gemini:
            raw_conf = gemini.get('confidence', 0)
            freshness_val = gemini.get('catalyst_freshness', 'none')
            f_mult = {'breaking': 1.0, 'recent': 0.8, 'stale': 0.3, 'none': 0.5}.get(freshness_val, 0.5)
            strength = raw_conf * f_mult

    # Apply SMA override penalty — signal passes but at reduced strength
    if sma_override_applied:
        penalty = sma_cfg.get('penalty_pct', 0.25)
        strength *= (1.0 - penalty)
        log.info(f"[{symbol}] SMA override penalty: strength {strength:.3f} "
                 f"(reduced by {penalty:.0%})")

    # --- Step 5: Multi-timeframe trend alignment (strength modifier) ---
    tf_cfg = sentiment_config.get('trend_alignment', {})
    trend_alignment_meta = None
    if tf_cfg.get('enabled', False):
        daily_closes = market_data.get('daily_closes')
        weekly_closes = market_data.get('weekly_closes')
        monthly_closes = market_data.get('monthly_closes')
        if daily_closes:
            ta = compute_trend_alignment(
                daily_closes, direction,
                weekly_closes=weekly_closes,
                monthly_closes=monthly_closes,
                daily_period=tf_cfg.get('daily_period', 20),
                weekly_period=tf_cfg.get('weekly_period', 20),
                monthly_period=tf_cfg.get('monthly_period', 10),
            )
            min_score = tf_cfg.get('min_score', 0.0)
            if ta['timeframes_evaluated'] > 0 and ta['score'] < min_score:
                reason = (f"Trend alignment {ta['score']:.2f} < {min_score} "
                          f"({ta['agreement_count']}/{ta['timeframes_evaluated']} "
                          f"timeframes agree). {sentiment_reason}.")
                log.info(f"[{symbol}] HOLD: {reason}")
                return {"signal": "HOLD", "symbol": symbol, "reason": reason,
                        "trend_alignment": ta}
            floor = tf_cfg.get('strength_floor', 0.5)
            multiplier = floor + (1.0 - floor) * ta['score']
            strength *= multiplier
            trend_alignment_meta = ta
            log.info(f"[{symbol}] Trend alignment: {ta['agreement_count']}/"
                     f"{ta['timeframes_evaluated']} agree (score={ta['score']:.2f}), "
                     f"strength × {multiplier:.2f} → {strength:.3f}")

    result = {"signal": signal_type, "symbol": symbol, "reason": reason,
              "current_price": current_price, "signal_strength": strength}
    if sma_override_applied:
        result["sma_override"] = True
    if trend_alignment_meta is not None:
        result["trend_alignment"] = trend_alignment_meta
    return result
