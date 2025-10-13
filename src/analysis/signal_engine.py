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
    Generates a more advanced signal by combining multiple data sources.

    Current Logic:
    - Base signal on Fear & Greed Index.
    - If F&G is "Extreme Fear" (BUY), check if whales are moving assets OFF exchanges (bullish).
    - If F&G is "Extreme Greed" (SELL), check if whales are moving assets ONTO exchanges (bearish).
    - This provides a secondary confirmation for our primary signal.
    """
    # 1. Get base signal from Fear & Greed
    base_signal = generate_signal_from_fear_and_greed(fear_and_greed)
    if not base_signal:
        return {"signal": "HOLD", "reason": "Could not determine F&G signal."}

    final_signal = base_signal
    final_signal['source'] = "Fear & Greed Index"
    final_signal['details'] = fear_and_greed[0]

    # 2. Analyze whale transactions for confirmation
    if whale_transactions is not None and len(whale_transactions) > 0:
        exchange_inflow = 0
        exchange_outflow = 0
        
        for tx in whale_transactions:
            # Simple logic: if 'to' is an exchange, it's inflow. If 'from' is, it's outflow.
            if "exchange" in tx['to']['owner_type'].lower():
                exchange_inflow += tx['amount_usd']
            if "exchange" in tx['from']['owner_type'].lower():
                exchange_outflow += tx['amount_usd']
        
        net_flow = exchange_inflow - exchange_outflow
        
        # Add whale data to the signal reason
        final_signal['reason'] += f" | Whale net flow: ${net_flow:,.2f}."

        # Confirmation Logic
        if base_signal['signal'] == 'BUY' and net_flow < 0: # Outflow confirms BUY
            final_signal['reason'] += " (Confirmation: Whales are moving assets off exchanges)."
        elif base_signal['signal'] == 'SELL' and net_flow > 0: # Inflow confirms SELL
            final_signal['reason'] += " (Confirmation: Whales are moving assets onto exchanges)."
        elif base_signal['signal'] != 'HOLD':
            # If whale activity contradicts the F&G signal, we might downgrade to HOLD
            final_signal['signal'] = 'HOLD'
            final_signal['reason'] += " (Contradiction: Whale activity does not support F&G signal)."

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
