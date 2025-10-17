#!/usr/bin/env python3
# --- Main Application File ---
# This script orchestrates the entire bot's workflow.
# Force redeploy 2025-10-16

import time
import pandas as pd
import http.server
import socketserver
import threading
import os
import asyncio
from src.collectors.binance_data import get_current_price
from src.collectors.whale_alert import get_whale_transactions, get_stablecoin_flows
from src.analysis.signal_engine import generate_signal
from src.analysis.technical_indicators import calculate_rsi, calculate_transaction_velocity
from src.notify.telegram_bot import send_telegram_alert, start_bot
from src.database import initialize_database, get_historical_prices, get_transaction_timestamps_since, get_table_counts
from src.logger import log
from src.config import app_config

def run_bot_cycle():
    """
    Executes one full cycle of the bot's logic.
    """
    log.info("--- Starting new bot cycle ---")
    settings = app_config.get('settings', {})
    
    # Load all settings
    watch_list = settings.get('watch_list', ['BTCUSDT'])
    min_whale_value = settings.get('min_whale_transaction_usd', 1000000)
    high_interest_wallets = settings.get('high_interest_wallets', [])
    stablecoins_to_monitor = settings.get('stablecoins_to_monitor', [])
    baseline_hours = settings.get('transaction_velocity_baseline_hours', 24)
    sma_period = 20
    rsi_period = 14

    # 1. Collect data
    log.info("Fetching data from all sources...")
    whale_transactions = get_whale_transactions(min_value_usd=min_whale_value)
    stablecoin_data = get_stablecoin_flows(whale_transactions, stablecoins_to_monitor)
    
    # Process each symbol in the watch list
    for symbol in watch_list:
        log.info(f"--- Processing symbol: {symbol} ---")
        price_data = get_current_price(symbol)
        
        if not price_data or not price_data.get('price'):
            log.warning(f"Could not fetch current price for {symbol}. Skipping analysis.")
            continue

        current_price = float(price_data.get('price'))
        
        # 2. Analyze data for a signal
        log.info(f"Analyzing data for {symbol}...")
        
        # Fetch historical data for technical analysis
        historical_prices = get_historical_prices(symbol, limit=rsi_period + 1)
        historical_timestamps = get_transaction_timestamps_since(symbol.lower(), hours_ago=baseline_hours)
        
        # Calculate technical indicators and velocity
        market_price_data = {
            'current_price': current_price,
            'sma': None,
            'rsi': None
        }
        if len(historical_prices) >= sma_period:
            price_series = pd.Series(historical_prices)
            market_price_data['sma'] = price_series.rolling(window=sma_period).mean().iloc[-1]
        market_price_data['rsi'] = calculate_rsi(historical_prices, period=rsi_period)
        
        transaction_velocity = calculate_transaction_velocity(historical_timestamps)
        
        # 3. Generate a signal
        log.info(f"Generating signal for {symbol}...")
        signal = generate_signal(
            symbol,
            market_price_data,
            whale_transactions,
            stablecoin_data,
            transaction_velocity,
            high_interest_wallets
        )

# Keep the main thread alive to allow background threads to run.
while True:
    time.sleep(1)
# Keep the main thread alive to allow background threads to run.
while True:
    time.sleep(1)