import sys
import os
from unittest.mock import patch

# Explicitly add the project root to the Python path
project_root = "/Users/daniilmarszallek/Documents/Crypto Bot/"
sys.path.insert(0, project_root)

from src.collectors.binance_data import get_current_price
from src.logger import log

# Mock database functions to prevent actual DB interaction during this test
@patch('src.database.initialize_database')
@patch('src.collectors.binance_data.save_price_data')
def run_test(mock_save_price_data, mock_initialize_database):
    log.info("--- Starting local test for Binance symbols ---")

    watch_list = [
        "BTC", "ETH", "SOL", "XRP", "ADA", "AVAX", "DOGE", "MATIC", "BNB", "TRX"
    ]

    for symbol in watch_list:
        binance_symbol = f"{symbol}USDT"
        log.info(f"Attempting to fetch price for {binance_symbol}...")
        price_data = get_current_price(binance_symbol)
        if price_data and price_data.get('price'):
            log.info(f"Successfully fetched price for {binance_symbol}: {price_data['price']}")
        else:
            log.error(f"Failed to fetch price for {binance_symbol}. Check if the symbol is valid on Binance.")
    
    log.info("--- Local test for Binance symbols complete ---")

if __name__ == "__main__":
    run_test()