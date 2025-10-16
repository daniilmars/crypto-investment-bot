#!/usr/bin/env python3
# --- Main Application File ---
# This script orchestrates the entire bot's workflow.

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
from src.database import initialize_database, get_historical_prices, get_transaction_timestamps_since
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
        
        velocity_data = calculate_transaction_velocity(symbol.lower(), whale_transactions, historical_timestamps, baseline_hours)

        signal = generate_signal(
            whale_transactions,
            market_price_data,
            high_interest_wallets,
            stablecoin_data,
            settings.get('stablecoin_inflow_threshold_usd', 100000000),
            velocity_data,
            settings.get('transaction_velocity_threshold_multiplier', 5.0)
        )

        if not signal:
            log.warning(f"Signal engine did not produce a valid signal for {symbol}.")
            continue
            
        log.info(f"Signal for {symbol}: {signal['signal']} - Reason: {signal['reason']}")

        # 3. Send notification if the signal is significant
        if signal['signal'] in ["BUY", "SELL", "VOLATILITY_WARNING"]:
            log.info(f"Significant signal detected for {symbol}. Sending notification...")
            signal['symbol'] = symbol
            signal['current_price'] = current_price
            asyncio.run(send_telegram_alert(signal))
        else:
            log.info(f"Signal for {symbol} is 'HOLD'. No notification will be sent.")
    
    log.info("--- Bot cycle finished ---")

def start_health_check_server():
    """Starts a simple HTTP server for health checks."""
    port = int(os.environ.get("PORT", 8080))
    handler = http.server.SimpleHTTPRequestHandler
    with socketserver.TCPServer(("", port), handler) as httpd:
        log.info(f"Health check server started on port {port}")
        httpd.serve_forever()

def bot_loop():
    """The main, long-running loop for the bot's analysis cycle."""
    # Load configuration to get the run interval
    settings = app_config.get('settings', {})
    run_interval_minutes = settings.get('run_interval_minutes', 15)
    log.info(f"Bot will run every {run_interval_minutes} minutes.")

    while True:
        try:
            run_bot_cycle()
        except Exception as e:
            log.error(f"An error occurred in the main bot cycle: {e}", exc_info=True)
        
        sleep_duration_seconds = run_interval_minutes * 60
        log.info(f"Sleeping for {run_interval_minutes} minutes...")
        time.sleep(sleep_duration_seconds)

if __name__ == "__main__":
    # --- Argument Parser for Special Modes ---
    import argparse
    parser = argparse.ArgumentParser(description="Crypto Investment Alert Bot")
    parser.add_argument(
        '--test-notify',
        action='store_true',
        help='Send a test notification to the configured Telegram chat and exit.'
    )
    args = parser.parse_args()

    if args.test_notify:
        log.info("--- Running Telegram Notification Test ---")
        test_signal = {
            'signal': 'TEST',
            'reason': 'This is a test message to confirm the deployment is working correctly.',
            'symbol': 'BOT',
            'current_price': 'N/A'
        }
        asyncio.run(send_telegram_alert(test_signal))
        log.info("Test message sent. Exiting.")
        exit()

    # --- Main Application ---
    # Initialize the database first
    initialize_database()

    # Start the main bot cycle in a separate thread
    main_bot_thread = threading.Thread(target=bot_loop)
    main_bot_thread.daemon = True
    main_bot_thread.start()

    # Start Telegram bot in a separate thread
    telegram_bot_thread = threading.Thread(target=start_bot)
    telegram_bot_thread.daemon = True
    telegram_bot_thread.start()

    # Start the health check server in the main thread
    # This is crucial for Cloud Run to keep the instance alive.
    start_health_check_server()
