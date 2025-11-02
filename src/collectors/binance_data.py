import requests
import json
from src.database import get_db_connection
from src.logger import log

# Binance API base URL
BINANCE_API_URL = "https://api.binance.us/api/v3"

def save_price_data(price_data: dict):
    """Saves price data to the database."""
    if not price_data or 'symbol' not in price_data or 'price' not in price_data:
        return

    conn = get_db_connection()
    cursor = conn.cursor()
    
    query = 'INSERT INTO market_prices (symbol, price) VALUES (%s, %s)' if IS_POSTGRES else \
            'INSERT INTO market_prices (symbol, price) VALUES (?, ?)'
    
    cursor.execute(query, (price_data['symbol'], price_data['price']))
    
    conn.commit()
    cursor.close()
    conn.close()
    log.info(f"Saved price for {price_data['symbol']} to the database.")

def get_current_price(symbol: str):
    """
    Fetches the latest price for a specific symbol from the Binance API and saves it.
    """
    endpoint = f"{BINANCE_API_URL}/ticker/price"
    params = {'symbol': symbol}

    try:
        response = requests.get(endpoint, params=params)
        response.raise_for_status()

        price_data = response.json()
        log.info(f"Successfully fetched price for {symbol}: {price_data.get('price')}")
        save_price_data(price_data) # Save the data
        return price_data

    except requests.exceptions.HTTPError as http_err:
        if response.status_code == 400:
            log.error(f"Invalid symbol '{symbol}'.")
        else:
            log.error(f"HTTP error occurred: {http_err}")
        return None
    except requests.exceptions.RequestException as e:
        log.error(f"Error fetching price data from Binance: {e}")
        return None
    except json.JSONDecodeError:
        log.error("Could not decode JSON response from the Binance API.")
        return None

if __name__ == '__main__':
    log.info("--- Testing Binance Data Collector (with DB saving) ---")
    get_current_price("BTCUSDT")
    get_current_price("ETHUSDT")
    log.info("--- Test Complete ---")
