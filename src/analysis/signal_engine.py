# This module will contain the logic for analyzing data and generating trading signals.

def generate_signal_from_fear_and_greed(fear_and_greed_data: list):
    """
    Analyzes the Fear & Greed Index data to generate a simple trading signal.
    (This is the original, simple signal generator).
    """
    if not fear_and_greed_data or not isinstance(fear_and_greed_data, list) or len(fear_and_greed_data) == 0:
        return None

    latest_entry = fear_and_greed_data[0]
    value = int(latest_entry.get('value', 50))
    classification = latest_entry.get('value_classification', 'Neutral')

    signal = {"signal": "HOLD", "reason": f"F&G Index is {value} ({classification})."}
    if classification == "Extreme Fear":
        signal["signal"] = "BUY"
        signal["reason"] = f"F&G Index is at {value} (Extreme Fear)."
    elif classification == "Extreme Greed":
        signal["signal"] = "SELL"
        signal["reason"] = f"F&G Index is at {value} (Extreme Greed)."
    return signal

def generate_comprehensive_signal(fear_and_greed, whale_transactions, market_prices):
    """
    Generates a more advanced signal by combining sentiment, on-chain, and market data.
    """
    # 1. Get base signal from Fear & Greed
    base_signal = generate_signal_from_fear_and_greed(fear_and_greed)
    if not base_signal:
        return {"signal": "HOLD", "reason": "Could not determine F&G signal."}

    final_signal = base_signal
    final_signal['source'] = "Fear & Greed Index"
    final_signal['details'] = fear_and_greed[0]

    # 2. Analyze whale transactions for on-chain confirmation
    if whale_transactions is not None and len(whale_transactions) > 0:
        # (Whale logic remains the same as before)
        exchange_inflow = sum(tx['amount_usd'] for tx in whale_transactions if "exchange" in tx.get('to', {}).get('owner_type', ''))
        exchange_outflow = sum(tx['amount_usd'] for tx in whale_transactions if "exchange" in tx.get('from', {}).get('owner_type', ''))
        net_flow = exchange_inflow - exchange_outflow
        final_signal['reason'] += f" | Whale net flow: ${net_flow:,.2f}."

        if base_signal['signal'] == 'BUY' and net_flow > 0: # Inflow contradicts BUY
            final_signal['signal'] = 'HOLD'
            final_signal['reason'] += " (Contradiction: Whale inflow)."
        elif base_signal['signal'] == 'SELL' and net_flow < 0: # Outflow contradicts SELL
            final_signal['signal'] = 'HOLD'
            final_signal['reason'] += " (Contradiction: Whale outflow)."

    # 3. Analyze price action for market confirmation
    if final_signal['signal'] != 'HOLD' and market_prices.get('current_price') and market_prices.get('sma_5'):
        current_price = market_prices['current_price']
        sma = market_prices['sma_5']
        final_signal['reason'] += f" | Price: ${current_price:,.2f}, SMA(5): ${sma:,.2f}."

        if final_signal['signal'] == 'BUY' and current_price > sma:
            final_signal['signal'] = 'HOLD'
            final_signal['reason'] += " (Contradiction: Price is above SMA)."
        elif final_signal['signal'] == 'SELL' and current_price < sma:
            final_signal['signal'] = 'HOLD'
            final_signal['reason'] += " (Contradiction: Price is below SMA)."

    return final_signal


if __name__ == '__main__':
    # --- Testing Comprehensive Signal Engine ---
    print("--- Testing Comprehensive Signal Engine ---")

    # Mock Data
    fng_buy = [{'value': '15', 'value_classification': 'Extreme Fear'}]
    fng_sell = [{'value': '85', 'value_classification': 'Extreme Greed'}]
    fng_hold = [{'value': '50', 'value_classification': 'Neutral'}]
    
    whales_bullish = [{'from': {'owner_type': 'exchange'}, 'to': {'owner_type': 'wallet'}, 'amount_usd': 5000000}]
    whales_bearish = [{'from': {'owner_type': 'wallet'}, 'to': {'owner_type': 'exchange'}, 'amount_usd': 5000000}]
    whales_neutral = []

    # Test Case 1: F&G Buy + Bullish Whales -> Strong BUY
    print("\nTesting F&G BUY + Bullish Whales...")
    signal1 = generate_comprehensive_signal(fng_buy, whales_bullish, {})
    print(f"Signal: {signal1['signal']}, Reason: {signal1['reason']}")

    # Test Case 2: F&G Sell + Bearish Whales -> Strong SELL
    print("\nTesting F&G SELL + Bearish Whales...")
    signal2 = generate_comprehensive_signal(fng_sell, whales_bearish, {})
    print(f"Signal: {signal2['signal']}, Reason: {signal2['reason']}")

    # Test Case 3: F&G Buy + Bearish Whales -> Downgrade to HOLD
    print("\nTesting F&G BUY + Bearish Whales (Contradiction)...")
    signal3 = generate_comprehensive_signal(fng_buy, whales_bearish, {})
    print(f"Signal: {signal3['signal']}, Reason: {signal3['reason']}")
    
    # Test Case 4: F&G Neutral + Any Whales -> HOLD
    print("\nTesting F&G Neutral...")
    signal4 = generate_comprehensive_signal(fng_hold, whales_bearish, {})
    print(f"Signal: {signal4['signal']}, Reason: {signal4['reason']}")

    print("\n--- Test Complete ---")
