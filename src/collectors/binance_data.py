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

def get_24hr_stats(symbol: str):
    """
    Fetches 24-hour rolling statistics for a symbol from Binance.

    Returns:
        dict with 'volume', 'quote_volume', 'price_change_percent', 'high', 'low',
        'weighted_avg_price', 'trade_count' or None on failure.
    """
    if not _validate_symbol(symbol):
        return None
    endpoint = f"{BINANCE_API_URL}/ticker/24hr"
    params = {'symbol': symbol}

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            response = requests.get(endpoint, params=params, timeout=30)
            response.raise_for_status()
            data = response.json()

            result = {
                'symbol': symbol,
                'volume': float(data.get('volume', 0)),
                'quote_volume': float(data.get('quoteVolume', 0)),
                'price_change_percent': float(data.get('priceChangePercent', 0)),
                'high': float(data.get('highPrice', 0)),
                'low': float(data.get('lowPrice', 0)),
                'weighted_avg_price': float(data.get('weightedAvgPrice', 0)),
                'trade_count': int(data.get('count', 0)),
            }
            log.info(f"Fetched 24hr stats for {symbol}: vol={result['volume']:.2f}, "
                     f"change={result['price_change_percent']:.2f}%")
            return result

        except requests.exceptions.HTTPError as http_err:
            if hasattr(http_err, 'response') and http_err.response is not None and http_err.response.status_code == 400:
                log.error(f"Invalid symbol '{symbol}' for 24hr stats.")
                return None
            log.error(f"HTTP error fetching 24hr stats (attempt {attempt}/{MAX_RETRIES}): {http_err}")
        except requests.exceptions.RequestException as e:
            log.error(f"Error fetching 24hr stats from Binance (attempt {attempt}/{MAX_RETRIES}): {e}")
        except (json.JSONDecodeError, ValueError, KeyError) as e:
            log.error(f"Error parsing 24hr stats response (attempt {attempt}/{MAX_RETRIES}): {e}")

        if attempt < MAX_RETRIES:
            backoff = RETRY_BACKOFF_BASE ** attempt
            log.info(f"Retrying in {backoff}s...")
            time.sleep(backoff)

    log.error(f"Failed to fetch 24hr stats for {symbol} after {MAX_RETRIES} attempts.")
    return None


def get_klines(symbol: str, interval: str = '1h', limit: int = 100):
    """
    Fetches candlestick (klines) OHLC data from Binance.

    Args:
        symbol: Trading pair (e.g. 'BTCUSDT')
        interval: Candle interval ('1m','5m','15m','1h','4h','1d', etc.)
        limit: Number of candles to fetch (max 1000)

    Returns:
        list of dicts with 'open', 'high', 'low', 'close', 'volume', 'timestamp'
        or None on failure.
    """
    if not _validate_symbol(symbol):
        return None
    if limit > 1000:
        limit = 1000

    endpoint = f"{BINANCE_API_URL}/klines"
    params = {'symbol': symbol, 'interval': interval, 'limit': limit}

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            response = requests.get(endpoint, params=params, timeout=30)
            response.raise_for_status()
            raw_klines = response.json()

            klines = []
            for k in raw_klines:
                klines.append({
                    'timestamp': int(k[0]),
                    'open': float(k[1]),
                    'high': float(k[2]),
                    'low': float(k[3]),
                    'close': float(k[4]),
                    'volume': float(k[5]),
                })
            log.info(f"Fetched {len(klines)} klines for {symbol} ({interval}).")
            return klines

        except requests.exceptions.HTTPError as http_err:
            if hasattr(http_err, 'response') and http_err.response is not None and http_err.response.status_code == 400:
                log.error(f"Invalid request for klines: {symbol}/{interval}.")
                return None
            log.error(f"HTTP error fetching klines (attempt {attempt}/{MAX_RETRIES}): {http_err}")
        except requests.exceptions.RequestException as e:
            log.error(f"Error fetching klines from Binance (attempt {attempt}/{MAX_RETRIES}): {e}")
        except (json.JSONDecodeError, ValueError, IndexError) as e:
            log.error(f"Error parsing klines response (attempt {attempt}/{MAX_RETRIES}): {e}")

        if attempt < MAX_RETRIES:
            backoff = RETRY_BACKOFF_BASE ** attempt
            log.info(f"Retrying in {backoff}s...")
            time.sleep(backoff)

    log.error(f"Failed to fetch klines for {symbol} after {MAX_RETRIES} attempts.")
    return None


def get_order_book_depth(symbol: str, limit: int = 20):
    """
    Fetches order book depth from Binance.

    Args:
        symbol: Trading pair (e.g. 'BTCUSDT')
        limit: Depth levels (5, 10, 20, 50, 100, 500, 1000)

    Returns:
        dict with 'bid_volume', 'ask_volume', 'bid_ask_ratio', 'top_bid', 'top_ask',
        'spread_pct' or None on failure.
    """
    if not _validate_symbol(symbol):
        return None

    endpoint = f"{BINANCE_API_URL}/depth"
    params = {'symbol': symbol, 'limit': limit}

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            response = requests.get(endpoint, params=params, timeout=30)
            response.raise_for_status()
            data = response.json()

            bids = data.get('bids', [])
            asks = data.get('asks', [])

            if not bids or not asks:
                log.warning(f"Empty order book for {symbol}.")
                return None

            # Calculate total volume on each side (price * quantity)
            bid_volume = sum(float(b[0]) * float(b[1]) for b in bids)
            ask_volume = sum(float(a[0]) * float(a[1]) for a in asks)

            top_bid = float(bids[0][0])
            top_ask = float(asks[0][0])
            spread_pct = ((top_ask - top_bid) / top_ask) * 100 if top_ask > 0 else 0

            bid_ask_ratio = bid_volume / ask_volume if ask_volume > 0 else 0

            result = {
                'symbol': symbol,
                'bid_volume': bid_volume,
                'ask_volume': ask_volume,
                'bid_ask_ratio': bid_ask_ratio,
                'top_bid': top_bid,
                'top_ask': top_ask,
                'spread_pct': spread_pct,
            }
            log.info(f"Order book for {symbol}: bid/ask ratio={bid_ask_ratio:.3f}, "
                     f"spread={spread_pct:.4f}%")
            return result

        except requests.exceptions.HTTPError as http_err:
            if hasattr(http_err, 'response') and http_err.response is not None and http_err.response.status_code == 400:
                log.error(f"Invalid symbol '{symbol}' for order book.")
                return None
            log.error(f"HTTP error fetching order book (attempt {attempt}/{MAX_RETRIES}): {http_err}")
        except requests.exceptions.RequestException as e:
            log.error(f"Error fetching order book from Binance (attempt {attempt}/{MAX_RETRIES}): {e}")
        except (json.JSONDecodeError, ValueError, IndexError) as e:
            log.error(f"Error parsing order book response (attempt {attempt}/{MAX_RETRIES}): {e}")

        if attempt < MAX_RETRIES:
            backoff = RETRY_BACKOFF_BASE ** attempt
            log.info(f"Retrying in {backoff}s...")
            time.sleep(backoff)

    log.error(f"Failed to fetch order book for {symbol} after {MAX_RETRIES} attempts.")
    return None


if __name__ == '__main__':
    log.info("--- Testing Binance Data Collector (with DB saving) ---")
    get_current_price("BTCUSDT")
    get_current_price("ETHUSDT")
    log.info("--- Testing 24hr Stats ---")
    get_24hr_stats("BTCUSDT")
    log.info("--- Testing Klines ---")
    get_klines("BTCUSDT", interval='1h', limit=10)
    log.info("--- Testing Order Book ---")
    get_order_book_depth("BTCUSDT")
    log.info("--- Test Complete ---")
