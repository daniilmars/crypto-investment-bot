import re
import requests
import json
import time
import psycopg2
from src.database import get_db_connection, release_db_connection
from src.logger import log

MAX_RETRIES = 3
RETRY_BACKOFF_BASE = 2

# Symbols must be alphanumeric, 1-20 characters (covers BTC, BTCUSDT, etc.)
_SYMBOL_RE = re.compile(r'^[A-Za-z0-9]{1,20}$')


def _validate_symbol(symbol: str) -> bool:
    """Validates that a symbol is alphanumeric and within expected length."""
    if not symbol or not _SYMBOL_RE.match(symbol):
        log.error(f"Invalid symbol format: {symbol!r}")
        return False
    return True

# Binance API base URL
BINANCE_API_URL = "https://api.binance.us/api/v3"

def save_price_data(price_data: dict):
    """Saves price data to the database."""
    if not price_data or 'symbol' not in price_data or 'price' not in price_data:
        return

    conn = None
    cursor = None
    try:
        conn = get_db_connection()
        is_postgres_conn = isinstance(conn, psycopg2.extensions.connection)
        cursor = conn.cursor()

        query = 'INSERT INTO market_prices (symbol, price) VALUES (%s, %s)' if is_postgres_conn else \
                'INSERT INTO market_prices (symbol, price) VALUES (?, ?)'

        cursor.execute(query, (price_data['symbol'], price_data['price']))
        conn.commit()
        log.info(f"Saved price for {price_data['symbol']} to the database.")
    except Exception as e:
        log.error(f"Error saving price data: {e}", exc_info=True)
    finally:
        if cursor:
            cursor.close()
        release_db_connection(conn)

def get_current_price(symbol: str):
    """
    Fetches the latest price for a specific symbol from the Binance API and saves it.
    Retries with exponential backoff on transient failures.
    """
    if not _validate_symbol(symbol):
        return None
    endpoint = f"{BINANCE_API_URL}/ticker/price"
    params = {'symbol': symbol}

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            response = requests.get(endpoint, params=params, timeout=30)
            response.raise_for_status()

            price_data = response.json()
            log.info(f"Successfully fetched price for {symbol}: {price_data.get('price')}")
            save_price_data(price_data)
            return price_data

        except requests.exceptions.HTTPError as http_err:
            if hasattr(http_err, 'response') and http_err.response is not None and http_err.response.status_code == 400:
                log.error(f"Invalid symbol '{symbol}'.")
                return None  # Don't retry client errors
            log.error(f"HTTP error occurred (attempt {attempt}/{MAX_RETRIES}): {http_err}")
        except requests.exceptions.RequestException as e:
            log.error(f"Error fetching price data from Binance (attempt {attempt}/{MAX_RETRIES}): {e}")
        except json.JSONDecodeError:
            log.error(f"Could not decode JSON response from Binance (attempt {attempt}/{MAX_RETRIES}).")

        if attempt < MAX_RETRIES:
            backoff = RETRY_BACKOFF_BASE ** attempt
            log.info(f"Retrying in {backoff}s...")
            time.sleep(backoff)

    log.error(f"Failed to fetch price for {symbol} after {MAX_RETRIES} attempts.")
    return None

if __name__ == '__main__':
    log.info("--- Testing Binance Data Collector (with DB saving) ---")
    get_current_price("BTCUSDT")
    get_current_price("ETHUSDT")
    log.info("--- Test Complete ---")
