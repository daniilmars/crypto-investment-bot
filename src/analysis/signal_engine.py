import pandas as pd
from src.logger import log

def generate_signal(symbol, whale_transactions, market_data, high_interest_wallets=None, stablecoin_data=None, stablecoin_threshold=100000000, velocity_data=None, velocity_threshold_multiplier=5.0, rsi_overbought_threshold=70, rsi_oversold_threshold=30):
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

    # --- Signal Generation ---
    reason = f"Price: ${current_price:,.2f}, SMA: ${sma:,.2f}, RSI: {rsi:.2f}, Whale Net Flow: ${net_flow:,.2f}. Buy Score: {buy_score}, Sell Score: {sell_score}."

    if buy_score >= 2:
        log.info(f"[{symbol}] BUY signal generated. {reason}")
        return {"signal": "BUY", "symbol": symbol, "reason": reason}
    
    if sell_score >= 2:
        log.info(f"[{symbol}] SELL signal generated. {reason}")
        return {"signal": "SELL", "symbol": symbol, "reason": reason}

    log.debug(f"[{symbol}] HOLD: No strong signal detected. {reason}")
    return {"signal": "HOLD", "symbol": symbol, "reason": "No strong signal detected. " + reason}