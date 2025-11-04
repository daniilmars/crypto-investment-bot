#!/usr/bin/env python3
# --- Main Application File ---
# This script orchestrates the entire bot's workflow.
# Force redeploy 2025-11-02_v2
# Force redeploy 2025-11-02
# Force redeploy 2025-10-16

import asyncio
import os
import time

import pandas as pd
import uvicorn
from fastapi import FastAPI, Request
from telegram import Update

from src.analysis.signal_engine import generate_signal
from src.analysis.technical_indicators import (calculate_rsi,
                                               calculate_transaction_velocity)
from src.collectors.binance_data import get_current_price
from src.collectors.whale_alert import (get_stablecoin_flows,
                                        get_whale_transactions)
from src.config import app_config
from src.database import (get_historical_prices,
                          get_transaction_timestamps_since, initialize_database,
                          save_signal)
from src.execution.binance_trader import (get_account_balance,
                                          get_open_positions, place_order)
from src.logger import log
from src.notify.telegram_bot import send_telegram_alert, start_bot
from src.state import bot_is_running

# Initialize the database at the start of the application
initialize_database()

# --- FastAPI App Initialization ---
app = FastAPI()
application = None


async def run_bot_cycle():
    """
    Executes one full cycle of the bot's logic.
    """
    log.info("--- Starting new bot cycle ---")
    settings = app_config.get('settings', {})

    # Load all settings
    watch_list = settings.get('watch_list', ['BTC'])  # Default to BTC if not configured
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
                        place_order(symbol, "SELL", position['quantity'], current_price,
                                    existing_order_id=position['order_id'])
                        await send_telegram_alert({"signal": "SELL", "symbol": symbol, "current_price": current_price,
                                                   "reason": f"Stop-loss hit ({stop_loss_percentage * 100:.2f}% loss)."})
                        continue

                    # Check for Take Profit
                    if pnl_percentage >= take_profit_percentage:
                        log.info(f"[PAPER TRADE] Take-profit hit for {symbol}. Closing position.")
                        place_order(symbol, "SELL", position['quantity'], current_price,
                                    existing_order_id=position['order_id'])
                        await send_telegram_alert({"signal": "SELL", "symbol": symbol, "current_price": current_price,
                                                   "reason": f"Take-profit hit ({take_profit_percentage * 100:.2f}% gain)."})
                        continue

        # --- Pause Check ---
        if not bot_is_running.is_set():
            log.info("Bot is paused. Skipping new signal generation and trading.")
            continue

        # 2. Analyze data for a signal
        log.info(f"Analyzing data for {symbol}...")

        historical_prices = get_historical_prices(symbol, limit=max(sma_period, rsi_period) + 1)
        historical_timestamps = get_transaction_timestamps_since(symbol.lower(), hours_ago=baseline_hours)

        market_price_data = {'current_price': current_price, 'sma': None, 'rsi': None}
        if len(historical_prices) >= sma_period:
            price_series = pd.Series(historical_prices)
            market_price_data['sma'] = price_series.rolling(window=sma_period).mean().iloc[-1]
        market_price_data['rsi'] = calculate_rsi(historical_prices, period=rsi_period)
        log.info(f"Technical Indicators for {symbol}: SMA={market_price_data['sma']}, RSI={market_price_data['rsi']}")

        transaction_velocity = calculate_transaction_velocity(symbol, whale_transactions, historical_timestamps,
                                                              baseline_hours)
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
                    log.info(
                        f"Skipping BUY for {symbol}: Max concurrent positions ({max_concurrent_positions}) reached.")
                else:
                    capital_to_risk = current_balance * trade_risk_percentage
                    quantity_to_buy = capital_to_risk / current_price
                    if quantity_to_buy * current_price > current_balance:
                        log.warning(f"Skipping BUY for {symbol}: Insufficient balance.")
                    else:
                        log.info(f"Executing paper trade: BUY {quantity_to_buy:.4f} {symbol}.")
                        place_order(symbol, "BUY", quantity_to_buy, current_price)
                        await send_telegram_alert(signal)

            elif signal['signal'] == "SELL":
                position_to_close = next(
                    (p for p in open_positions if p['symbol'] == symbol and p['status'] == 'OPEN'), None)
                if position_to_close:
                    log.info(f"Executing paper trade: SELL {position_to_close['quantity']:.4f} {symbol}.")
                    place_order(symbol, "SELL", position_to_close['quantity'], current_price,
                                existing_order_id=position_to_close['order_id'])
                    await send_telegram_alert(signal)
                else:
                    log.info(f"Skipping SELL for {symbol}: No open position found.")
            else:  # HOLD
                log.info(f"Signal is HOLD for {symbol}. No trade action taken.")


async def bot_loop():
    """
    The main indefinite loop for the bot.
    """
    run_interval_minutes = app_config.get('settings', {}).get('run_interval_minutes', 15)
    while True:
        try:
            await run_bot_cycle()
        except Exception as e:
            log.error(f"Error in bot_loop cycle: {e}", exc_info=True)
        log.info(f"Cycle complete. Waiting for {run_interval_minutes} minutes...")
        await asyncio.sleep(run_interval_minutes * 60)


async def run_single_status_update():
    """Fetches and sends a single status update."""
    status_config = app_config.get('settings', {}).get('regular_status_update', {})
    interval_hours = status_config.get('interval_hours', 1)

    from src.database import get_trade_summary
    from src.notify.telegram_bot import send_performance_report

    try:
        log.info("Fetching trade summary for status update...")
        summary = get_trade_summary(hours_ago=interval_hours)
        if application:
            await send_performance_report(summary, interval_hours)
    except Exception as e:
        log.error(f"Error in run_single_status_update: {e}")


async def status_update_loop():
    """
    A separate loop to send periodic status updates.
    """
    status_config = app_config.get('settings', {}).get('regular_status_update', {})
    if not status_config.get('enabled'):
        log.info("Regular status updates are disabled.")
        return

    interval_hours = status_config.get('interval_hours', 1)
    log.info(f"Starting regular status update loop. Interval: {interval_hours} hours.")

    while True:
        try:
            await run_single_status_update()
        except Exception as e:
            log.error(f"Error in status_update_loop: {e}", exc_info=True)
        await asyncio.sleep(interval_hours * 3600)


@app.on_event("startup")
async def startup_event():
    """
    On startup, initialize the Telegram bot, set the webhook,
    and start the background tasks.
    """
    global application
    log.info("Starting application...")

    # Initialize the Telegram application
    application = await start_bot()

    # Set the webhook. The URL must be passed as an environment variable.
    # For Google Cloud Run, this is often provided as `GOOGLE_CLOUD_RUN_SERVICE_URL`.
    service_url = os.environ.get("SERVICE_URL")
    if not service_url:
        log.warning("SERVICE_URL environment variable not set. Webhook will not be set.")
    else:
        webhook_url = f"{service_url}/webhook"
        log.info(f"Setting webhook to: {webhook_url}")
        await application.bot.set_webhook(url=webhook_url)

    # Start background tasks
    asyncio.create_task(bot_loop())
    asyncio.create_task(status_update_loop())
    log.info("Startup complete. Background tasks running.")


@app.on_event("shutdown")
async def shutdown_event_handler():
    """
    On shutdown, gracefully delete the webhook.
    """
    log.info("Shutting down application...")
    if application:
        log.info("Deleting webhook...")
        await application.bot.delete_webhook()
    log.info("Shutdown complete.")


@app.get("/health", status_code=200)
async def health_check():
    """
    Health check endpoint for Cloud Run.
    """
    return {"status": "ok"}


@app.post("/webhook")
async def handle_webhook(request: Request):
    """
    Handles incoming updates from the Telegram API webhook.
    """
    if not application:
        log.error("Webhook received but application not initialized.")
        return {"status": "error", "message": "Bot not initialized"}, 500

    try:
        data = await request.json()
        update = Update.de_json(data, application.bot)
        await application.process_update(update)
        return {"status": "ok"}
    except Exception as e:
        log.error(f"Error processing webhook: {e}", exc_info=True)
        return {"status": "error"}, 500


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    log.info(f"Starting Uvicorn server on port {port}...")
    uvicorn.run(app, host="0.0.0.0", port=port)

