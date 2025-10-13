# --- Main Application File ---
# This script orchestrates the entire bot's workflow.

import time
from src.collectors.fear_and_greed import get_fear_and_greed_index
from src.collectors.binance_data import get_current_price
from src.collectors.whale_alert import get_whale_transactions
from src.analysis.signal_engine import generate_comprehensive_signal
from src.collectors.whale_alert import get_whale_transactions
from src.analysis.signal_engine import generate_comprehensive_signal
from src.notify.telegram_bot import send_telegram_alert, load_config
from src.database import initialize_database
from src.logger import log

def run_bot_cycle():
    """
    Executes one full cycle of the bot's logic.
    """
    log.info("--- Starting new bot cycle ---")
    config = load_config()
    watch_list = config.get('settings', {}).get('watch_list', ['BTCUSDT'])
    min_whale_value = config.get('settings', {}).get('min_whale_transaction_usd', 1000000)

    # 1. Collect data
    log.info("Fetching data from all sources...")
    fear_and_greed_data = get_fear_and_greed_index(limit=1)
    whale_transactions = get_whale_transactions(min_value_usd=min_whale_value)
    
    market_prices = {}
    # TODO: In the future, we should fetch historical prices for a proper moving average
    # For now, the live price is used for alerting, and the backtester handles SMA.
    for symbol in watch_list:
        price_data = get_current_price(symbol)
        if price_data:
            market_prices[symbol] = price_data.get('price')

    if not fear_and_greed_data:
        log.warning("Could not fetch F&G data. Skipping this cycle.")
        return

    # 2. Analyze data for a signal
    log.info("Analyzing data for a signal...")
    # Note: The live run doesn't have historical data for a moving average.
    # We'll pass an empty dict for market_prices to the signal engine for now.
    # The backtester is where the SMA logic is fully utilized.
    signal = generate_comprehensive_signal(fear_and_greed_data, whale_transactions, {})

    if not signal:
        log.warning("Signal engine did not produce a valid signal. Skipping this cycle.")
        return
        
    log.info(f"Signal generated: {signal['signal']} - Reason: {signal['reason']}")

    # 3. Send notification if the signal is significant
    if signal['signal'] in ["BUY", "SELL"]:
        log.info("Significant signal detected. Sending notification...")
        send_telegram_alert(signal)
    else:
        log.info("Signal is 'HOLD'. No notification will be sent.")
    
    log.info("--- Bot cycle finished ---")


if __name__ == "__main__":
    # Initialize the database first
    initialize_database()

    # Load configuration to get the run interval
    config = load_config()
    run_interval_minutes = 15 # Default value
    if config and 'settings' in config and 'run_interval_minutes' in config['settings']:
        run_interval_minutes = config['settings']['run_interval_minutes']
        log.info(f"Bot will run every {run_interval_minutes} minutes based on config.")
    else:
        log.info(f"Using default run interval of {run_interval_minutes} minutes.")

    # --- Main Application Loop ---
    while True:
        run_bot_cycle()
        sleep_duration_seconds = run_interval_minutes * 60
        log.info(f"Sleeping for {run_interval_minutes} minutes...")
        time.sleep(sleep_duration_seconds)
