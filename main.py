#!/usr/bin/env python3
# --- Main Application File ---
# This script orchestrates the entire bot's workflow.
# Force redeploy 2025-11-02_v2
# Force redeploy 2025-11-02
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
from src.database import initialize_database, get_historical_prices, get_transaction_timestamps_since, get_table_counts, save_signal
from src.logger import log
from src.config import app_config
from src.execution.binance_trader import place_order, get_open_positions, get_account_balance
from src.state import bot_is_running

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
    sma_period = settings.get('sma_period', 20)
    rsi_period = settings.get('rsi_period', 14)
    rsi_overbought_threshold = settings.get('rsi_overbought_threshold', 70)
    rsi_oversold_threshold = settings.get('rsi_oversold_threshold', 30)

    # Paper trading and risk management settings
    paper_trading = settings.get('paper_trading', True)
    paper_trading_initial_capital = settings.get('paper_trading_initial_capital', 10000.0)
    trade_risk_percentage = settings.get('trade_risk_percentage', 0.01)
    stop_loss_percentage = settings.get('stop_loss_percentage', 0.02)
    take_profit_percentage = settings.get('take_profit_percentage', 0.05)
    max_concurrent_positions = settings.get('max_concurrent_positions', 3)

    # 1. Collect data
    log.info("Fetching data from all sources...")
    whale_transactions = get_whale_transactions(min_value_usd=min_whale_value)
    stablecoin_data = get_stablecoin_flows(whale_transactions, stablecoins_to_monitor)
    
    # Process each symbol in the watch list
    for symbol in watch_list:
        log.info(f"--- Processing symbol: {symbol} ---")
        
        # Ensure the symbol format is correct for the Binance API (e.g., BTCUSDT)
        api_symbol = symbol if "USDT" in symbol else f"{symbol}USDT"
        price_data = get_current_price(api_symbol)
        
        if not price_data or not price_data.get('price'):
            log.warning(f"Could not fetch current price for {api_symbol}. Skipping analysis.")
            continue

        current_price = float(price_data.get('price'))
        log.info(f"Current price for {symbol}: ${current_price:,.2f}")
        
        # --- Position Monitoring (runs regardless of paused state) ---
        if paper_trading:
            open_positions = get_open_positions()
            for position in open_positions:
                if position['symbol'] == symbol and position['status'] == 'OPEN':
                    pnl_percentage = (current_price - position['entry_price']) / position['entry_price']

                    # Check for Stop Loss
                    if pnl_percentage <= -stop_loss_percentage:
                        log.info(f"[PAPER TRADE] Stop-loss hit for {symbol}. Closing position.")
                        place_order(symbol, "SELL", position['quantity'], current_price)
                        send_telegram_alert({"signal": "SELL", "symbol": symbol, "current_price": current_price, "reason": f"Stop-loss hit ({stop_loss_percentage*100:.2f}% loss)."})
                        continue

                    # Check for Take Profit
                    if pnl_percentage >= take_profit_percentage:
                        log.info(f"[PAPER TRADE] Take-profit hit for {symbol}. Closing position.")
                        place_order(symbol, "SELL", position['quantity'], current_price)
                        send_telegram_alert({"signal": "SELL", "symbol": symbol, "current_price": current_price, "reason": f"Take-profit hit ({take_profit_percentage*100:.2f}% gain)."})
                        continue
        
        # --- Pause Check ---
        if not bot_is_running.is_set():
            log.info("Bot is paused. Skipping new signal generation and trading.")
            continue

        # 2. Analyze data for a signal
        log.info(f"Analyzing data for {symbol}...")
        
        historical_prices = get_historical_prices(symbol, limit=max(sma_period, rsi_period) + 1)
        historical_timestamps = get_transaction_timestamps_since(symbol.lower(), hours_ago=baseline_hours)
        
        market_price_data = { 'current_price': current_price, 'sma': None, 'rsi': None }
        if len(historical_prices) >= sma_period:
            price_series = pd.Series(historical_prices)
            market_price_data['sma'] = price_series.rolling(window=sma_period).mean().iloc[-1]
        market_price_data['rsi'] = calculate_rsi(historical_prices, period=rsi_period)
        log.info(f"Technical Indicators for {symbol}: SMA={market_price_data['sma']}, RSI={market_price_data['rsi']}")
        
        transaction_velocity = calculate_transaction_velocity(symbol, whale_transactions, historical_timestamps, baseline_hours)
        log.info(f"Transaction Velocity for {symbol}: {transaction_velocity}")
        
        # 3. Generate a signal
        log.info(f"Generating signal for {symbol}...")
        signal = generate_signal(
            symbol=symbol,
            whale_transactions=whale_transactions,
            market_data=market_price_data,
            high_interest_wallets=high_interest_wallets,
            stablecoin_data=stablecoin_data,
            velocity_data=transaction_velocity,
            rsi_overbought_threshold=rsi_overbought_threshold,
            rsi_oversold_threshold=rsi_oversold_threshold
        )
        log.info(f"Generated Signal for {symbol}: {signal}")
        save_signal(signal)

        # --- 4. Paper Trading Logic ---
        if paper_trading:
            log.info(f"Processing signal for paper trading...")
            open_positions = get_open_positions()
            current_balance = get_account_balance().get('total_usd', paper_trading_initial_capital)
            
            if signal['signal'] == "BUY":
                if any(p['symbol'] == symbol and p['status'] == 'OPEN' for p in open_positions):
                    log.info(f"Skipping BUY for {symbol}: Position already open.")
                elif len(open_positions) >= max_concurrent_positions:
                    log.info(f"Skipping BUY for {symbol}: Max concurrent positions ({max_concurrent_positions}) reached.")
                else:
                    capital_to_risk = current_balance * trade_risk_percentage
                    quantity_to_buy = capital_to_risk / current_price
                    if quantity_to_buy * current_price > current_balance:
                        log.warning(f"Skipping BUY for {symbol}: Insufficient balance.")
                    else:
                        log.info(f"Executing paper trade: BUY {quantity_to_buy:.4f} {symbol}.")
                        place_order(symbol, "BUY", quantity_to_buy, current_price)
                        send_telegram_alert(signal)

            elif signal['signal'] == "SELL":
                position_to_close = next((p for p in open_positions if p['symbol'] == symbol and p['status'] == 'OPEN'), None)
                if position_to_close:
                    log.info(f"Executing paper trade: SELL {position_to_close['quantity']:.4f} {symbol}.")
                    place_order(symbol, "SELL", position_to_close['quantity'], current_price)
                    send_telegram_alert(signal)
                else:
                    log.info(f"Skipping SELL for {symbol}: No open position found.")
            else: # HOLD
                log.info(f"Signal is HOLD for {symbol}. No trade action taken.")

def bot_loop():
    """
    The main indefinite loop for the bot.
    """
    run_interval_minutes = app_config.get('settings', {}).get('run_interval_minutes', 15)
    while not shutdown_event.is_set():
        run_bot_cycle()
        log.info(f"Cycle complete. Waiting for {run_interval_minutes} minutes...")
        shutdown_event.wait(run_interval_minutes * 60)

def status_update_loop():
    """
    A separate loop to send periodic status updates.
    """
    status_config = app_config.get('settings', {}).get('regular_status_update', {})
    if not status_config.get('enabled'):
        log.info("Regular status updates are disabled.")
        return

    interval_hours = status_config.get('interval_hours', 1)
    log.info(f"Starting regular status update loop. Interval: {interval_hours} hours.")
    
    from src.database import get_trade_summary
    from src.notify.telegram_bot import send_performance_report

    while not shutdown_event.is_set():
        try:
            log.info("Fetching trade summary for periodic status update...")
            summary = get_trade_summary(hours_ago=interval_hours)
            # We need to run the async function in a thread-safe way on the main event loop
            if telegram_app:
                future = asyncio.run_coroutine_threadsafe(send_performance_report(summary, interval_hours), telegram_app.loop)
                future.result() # Wait for the coroutine to finish
        except Exception as e:
            log.error(f"Error in status_update_loop: {e}")
        
        # Wait for the interval, but check for shutdown event periodically
        shutdown_event.wait(interval_hours * 3600)

def start_health_check_server():
    """
    Starts a simple HTTP server in a thread to respond to Cloud Run health checks.
    """
    port = int(os.environ.get("PORT", 8080))
    
    class HealthCheckHandler(http.server.SimpleHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-type", "text/plain")
            self.end_headers()
            self.wfile.write(b"OK")

    with socketserver.TCPServer(("", port), HealthCheckHandler) as httpd:
        log.info(f"Health check server started on port {port}")
        httpd.serve_forever()

def telegram_main():
    """
    Initializes and runs the Telegram bot's asyncio event loop.
    """
    global telegram_app
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    
    telegram_app = loop.run_until_complete(start_bot())
    
    if telegram_app:
        # Keep the event loop running to handle Telegram updates
        loop.run_forever()

if __name__ == "__main__":
    # --- Global Application State ---
    telegram_app = None
    shutdown_event = threading.Event()
    
    # --- Graceful Shutdown Logic ---
    def shutdown(signum, frame):
        """Gracefully shuts down all application threads."""
        if not shutdown_event.is_set():
            log.info(f"Shutdown signal {signum} received. Initiating graceful shutdown...")
            shutdown_event.set() # Signal all loops to exit

            # Stop the Telegram bot
            if telegram_app:
                # Get the running event loop and stop it
                loop = asyncio.get_event_loop()
                loop.call_soon_threadsafe(loop.stop)
                # Running async stop function from a sync context
                asyncio.run(stop_bot(telegram_app))
            
            log.info("Shutdown complete.")
    
    # Register signal handlers
    import signal
    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    # --- Main Application ---
    try:
        # Initialize the database first
        initialize_database()

        # Start the Telegram bot in a separate thread
        telegram_thread = threading.Thread(target=telegram_main)
        telegram_thread.daemon = True
        telegram_thread.start()

        # Give the Telegram bot a moment to initialize
        time.sleep(2)

        if telegram_app:
            # Start the main bot cycle in a separate thread
            main_bot_thread = threading.Thread(target=bot_loop)
            main_bot_thread.daemon = True
            main_bot_thread.start()

            # Start the periodic status update loop in a separate thread
            status_update_thread = threading.Thread(target=status_update_loop)
            status_update_thread.daemon = True
            status_update_thread.start()
        else:
            log.error("Failed to initialize Telegram bot. Application will not start.")
            shutdown(None, None)

        # Start the health check server in the main thread
        # This is crucial for Cloud Run to keep the instance alive.
        start_health_check_server()

    except (KeyboardInterrupt, SystemExit):
        log.info("Caught KeyboardInterrupt or SystemExit.")
        shutdown(None, None)
    except Exception as e:
        log.error(f"An unexpected error occurred in the main thread: {e}", exc_info=True)
        shutdown(None, None)
