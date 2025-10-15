def generate_signal(whale_transactions, market_data, high_interest_wallets=None, stablecoin_data=None, stablecoin_threshold=100000000, velocity_data=None, velocity_threshold_multiplier=5.0):
    """
    Generates a trading signal based on on-chain data and technical indicators.
    Prioritizes anomalies and high-priority events.
    """
    if high_interest_wallets is None: high_interest_wallets = []
    if stablecoin_data is None: stablecoin_data = {}
    if velocity_data is None: velocity_data = {}

    # --- 1. Check for Transaction Velocity Anomaly ---
    if velocity_data.get('is_anomaly'):
        current = velocity_data.get('current_count')
        baseline = velocity_data.get('baseline_avg')
        if current > (baseline * velocity_threshold_multiplier):
            reason = f"Transaction velocity anomaly detected: {current} txns in last hour vs. baseline of {baseline:.1f}."
            return {"signal": "VOLATILITY_WARNING", "reason": reason}

    # --- 2. Check for High-Priority Stablecoin Inflow ---
    inflow = stablecoin_data.get('stablecoin_inflow_usd', 0)
    if inflow > stablecoin_threshold:
        reason = f"Massive stablecoin inflow of ${inflow:,.2f} detected to exchanges."
        return {"signal": "BUY", "reason": "High-priority signal: " + reason}

    # --- 3. Check for High-Priority Signals from Watched Wallets ---
    if whale_transactions:
        for tx in whale_transactions:
            from_owner = tx.get('from', {}).get('owner', 'unknown')
            to_owner = tx.get('to', {}).get('owner', 'unknown')
            to_owner_type = tx.get('to', {}).get('owner_type', 'unknown')
            from_owner_type = tx.get('from', {}).get('owner_type', 'unknown')
            amount_usd = tx.get('amount_usd', 0)

            if from_owner in high_interest_wallets and to_owner_type == 'exchange':
                reason = f"High-interest wallet '{from_owner}' sent ${amount_usd:,.2f} to {to_owner}."
                return {"signal": "SELL", "reason": "High-priority signal: " + reason}

            if to_owner in high_interest_wallets and from_owner_type == 'exchange':
                reason = f"High-interest wallet '{to_owner}' received ${amount_usd:,.2f} from {from_owner}."
                return {"signal": "BUY", "reason": "High-priority signal: " + reason}

    # --- 4. Standard Technical and On-Chain Analysis ---
    current_price = market_data.get('current_price')
    sma = market_data.get('sma')
    rsi = market_data.get('rsi')

    if current_price is None or sma is None or rsi is None:
        return {"signal": "HOLD", "reason": "Missing market data (price, SMA, or RSI)."}

    is_uptrend = current_price > sma
    is_downtrend = current_price < sma
    is_oversold = rsi < 30
    is_overbought = rsi > 70

    net_flow = 0
    if whale_transactions:
        exchange_inflow = sum(tx['amount_usd'] for tx in whale_transactions if "exchange" in tx.get('to', {}).get('owner_type', ''))
        exchange_outflow = sum(tx['amount_usd'] for tx in whale_transactions if "exchange" in tx.get('from', {}).get('owner_type', ''))
        net_flow = exchange_inflow - exchange_outflow
    
    whale_confirms_buy = net_flow < 0
    whale_confirms_sell = net_flow > 0

    reason = f"Price: ${current_price:,.2f}, SMA: ${sma:,.2f}, RSI: {rsi:.2f}, Whale Net Flow: ${net_flow:,.2f}."

    if is_uptrend and is_oversold and whale_confirms_buy:
        return {"signal": "BUY", "reason": "Uptrend, oversold, and whale activity confirms BUY. " + reason}
    
    if is_downtrend and is_overbought and whale_confirms_sell:
        return {"signal": "SELL", "reason": "Downtrend, overbought, and whale activity confirms SELL. " + reason}

    return {"signal": "HOLD", "reason": "No strong signal detected. " + reason}
