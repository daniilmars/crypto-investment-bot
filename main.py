# --- Main Application File ---
# This script orchestrates the entire bot's workflow.

import time
from src.collectors.fear_and_greed import get_fear_and_greed_index
from src.collectors.binance_data import get_current_price
from src.collectors.whale_alert import get_whale_transactions
from src.analysis.signal_engine import generate_comprehensive_signal
from src.notify.telegram_bot import send_telegram_alert, load_config

def run_bot_cycle():
    """
    Executes one full cycle of the bot's logic:
    1. Fetches data.
    2. Analyzes it for a signal.
    3. Sends a notification if the signal is not "HOLD".
    """
    print("--- Starting new bot cycle ---")
    config = load_config()
    watch_list = config.get('settings', {}).get('watch_list', ['BTCUSDT'])
    min_whale_value = config.get('settings', {}).get('min_whale_transaction_usd', 1000000)

    # 1. Collect data
    print("Fetching data from all sources...")
    fear_and_greed_data = get_fear_and_greed_index(limit=1)
    whale_transactions = get_whale_transactions(min_value_usd=min_whale_value)
    
    market_prices = {}
    for symbol in watch_list:
        price_data = get_current_price(symbol)
        if price_data:
            market_prices[symbol] = price_data.get('price')

    if not fear_and_greed_data:
        print("Could not fetch F&G data. Skipping this cycle.")
        return

    # 2. Analyze data for a signal
    print("Analyzing data for a signal...")
    signal = generate_comprehensive_signal(fear_and_greed_data, whale_transactions, market_prices)

    if not signal:
        print("Signal engine did not produce a valid signal. Skipping this cycle.")
        return
        
    print(f"Signal generated: {signal['signal']} - Reason: {signal['reason']}")

    # 3. Send notification if the signal is significant
    if signal['signal'] in ["BUY", "SELL"]:
        print("Significant signal detected. Sending notification...")
        send_telegram_alert(signal)
    else:
        print("Signal is 'HOLD'. No notification will be sent.")
    
    print("--- Bot cycle finished ---")


if __name__ == "__main__":
    # Load configuration to get the run interval
    config = load_config()
    run_interval_minutes = 15 # Default value
    if config and 'settings' in config and 'run_interval_minutes' in config['settings']:
        run_interval_minutes = config['settings']['run_interval_minutes']
        print(f"Bot will run every {run_interval_minutes} minutes based on config.")
    else:
        print(f"Using default run interval of {run_interval_minutes} minutes.")

    # --- Main Application Loop ---
    # The bot will run indefinitely and execute a cycle at the specified interval.
    while True:
        run_bot_cycle()
        sleep_duration_seconds = run_interval_minutes * 60
        print(f"\nSleeping for {run_interval_minutes} minutes...")
        time.sleep(sleep_duration_seconds)
