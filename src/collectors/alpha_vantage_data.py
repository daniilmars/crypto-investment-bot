import re
import requests
import json
import time
from src.config import app_config
from src.collectors.binance_data import save_price_data
from src.logger import log

# Stock symbols: 1-10 alphanumeric chars, may include dots/hyphens (e.g. BRK.B)
_STOCK_SYMBOL_RE = re.compile(r'^[A-Za-z0-9.\-]{1,10}$')


def _validate_stock_symbol(symbol: str) -> bool:
    """Validates that a stock symbol matches expected format."""
    if not symbol or not _STOCK_SYMBOL_RE.match(symbol):
        log.error(f"Invalid stock symbol format: {symbol!r}")
        return False
    return True

ALPHA_VANTAGE_BASE_URL = "https://www.alphavantage.co/query"
MAX_RETRIES = 3
RETRY_BACKOFF_BASE = 2
MIN_REQUEST_INTERVAL = 12  # seconds between calls (5/min limit)

_last_request_time = 0
_company_overview_cache = {}  # {symbol: {'data': {...}, 'timestamp': float}}
CACHE_TTL = 86400  # 24 hours


def _get_api_key():
    """Reads the Alpha Vantage API key from config."""
    return app_config.get('api_keys', {}).get('alpha_vantage')


def _rate_limited_request(params):
    """
    Makes a rate-limited request to the Alpha Vantage API.
    Enforces 12s between calls and retries 3x with backoff.
    """
    global _last_request_time

    api_key = _get_api_key()
    if not api_key or api_key == "YOUR_ALPHA_VANTAGE_API_KEY":
        log.error("Alpha Vantage API key is not configured.")
        return None

    params['apikey'] = api_key

    for attempt in range(1, MAX_RETRIES + 1):
        # Enforce rate limit
        elapsed = time.time() - _last_request_time
        if elapsed < MIN_REQUEST_INTERVAL:
            time.sleep(MIN_REQUEST_INTERVAL - elapsed)

        try:
            _last_request_time = time.time()
            response = requests.get(ALPHA_VANTAGE_BASE_URL, params=params, timeout=30)
            response.raise_for_status()
            data = response.json()

            # Check for rate limit response
            if "Note" in data:
                log.warning(f"Alpha Vantage rate limit hit: {data['Note']}")
                if attempt < MAX_RETRIES:
                    backoff = RETRY_BACKOFF_BASE ** attempt
                    log.info(f"Retrying in {backoff}s...")
                    time.sleep(backoff)
                    continue
                return None

            # Check for error response
            if "Error Message" in data:
                log.error(f"Alpha Vantage error: {data['Error Message']}")
                return None

            return data

        except requests.exceptions.HTTPError as http_err:
            log.error(f"HTTP error from Alpha Vantage (attempt {attempt}/{MAX_RETRIES}): {http_err}")
        except requests.exceptions.RequestException as e:
            log.error(f"Error fetching from Alpha Vantage (attempt {attempt}/{MAX_RETRIES}): {e}")
        except json.JSONDecodeError:
            log.error(f"Could not decode JSON from Alpha Vantage (attempt {attempt}/{MAX_RETRIES}).")

        if attempt < MAX_RETRIES:
            backoff = RETRY_BACKOFF_BASE ** attempt
            log.info(f"Retrying in {backoff}s...")
            time.sleep(backoff)

    log.error(f"Failed to fetch from Alpha Vantage after {MAX_RETRIES} attempts.")
    return None


def get_stock_price(symbol):
    """
    Fetches the latest stock price via GLOBAL_QUOTE and saves it to the database.

    Returns:
        dict with 'symbol', 'price', 'volume', 'change_percent' or None on failure.
    """
    if not _validate_stock_symbol(symbol):
        return None
    data = _rate_limited_request({
        'function': 'GLOBAL_QUOTE',
        'symbol': symbol
    })

    if not data or 'Global Quote' not in data:
        log.warning(f"No quote data returned for {symbol}.")
        return None

    quote = data['Global Quote']
    try:
        price = float(quote.get('05. price', 0))
        volume = float(quote.get('06. volume', 0))
        change_percent_str = quote.get('10. change percent', '0%')
        change_percent = float(change_percent_str.replace('%', ''))
    except (ValueError, TypeError) as e:
        log.error(f"Error parsing quote data for {symbol}: {e}")
        return None

    if price <= 0:
        log.warning(f"Invalid price for {symbol}: {price}")
        return None

    # Save to database using the shared save_price_data function
    save_price_data({'symbol': symbol, 'price': price})
    log.info(f"Successfully fetched stock price for {symbol}: ${price:.2f}")

    return {
        'symbol': symbol,
        'price': price,
        'volume': volume,
        'change_percent': change_percent
    }


def get_daily_prices(symbol):
    """
    Fetches daily time series data for local SMA/RSI calculation.

    Returns:
        dict with 'prices' (list of floats, oldest→newest) and 'volumes' (list of floats)
        or None on failure.
    """
    if not _validate_stock_symbol(symbol):
        return None
    data = _rate_limited_request({
        'function': 'TIME_SERIES_DAILY',
        'symbol': symbol,
        'outputsize': 'compact'  # last 100 data points
    })

    if not data or 'Time Series (Daily)' not in data:
        log.warning(f"No daily time series data returned for {symbol}.")
        return None

    time_series = data['Time Series (Daily)']
    # Sort by date ascending (oldest first)
    sorted_dates = sorted(time_series.keys())

    prices = []
    volumes = []
    for date in sorted_dates:
        try:
            prices.append(float(time_series[date]['4. close']))
            volumes.append(float(time_series[date]['5. volume']))
        except (ValueError, KeyError) as e:
            log.warning(f"Skipping data point for {symbol} on {date}: {e}")
            continue

    log.info(f"Fetched {len(prices)} daily prices for {symbol}.")
    return {'prices': prices, 'volumes': volumes}


def get_company_overview(symbol):
    """
    Fetches company fundamental data. Cached for 24 hours.

    Returns:
        dict with 'pe_ratio', 'earnings_growth', 'revenue_growth', 'beta' or None.
    """
    if not _validate_stock_symbol(symbol):
        return None
    # Check cache
    cached = _company_overview_cache.get(symbol)
    if cached and (time.time() - cached['timestamp']) < CACHE_TTL:
        log.info(f"Using cached company overview for {symbol}.")
        return cached['data']

    data = _rate_limited_request({
        'function': 'OVERVIEW',
        'symbol': symbol
    })

    if not data or 'Symbol' not in data:
        log.warning(f"No company overview data returned for {symbol}.")
        return None

    def _safe_float(value):
        """Safely convert a value to float, returning None for missing/invalid data."""
        if value is None or value == 'None' or value == '-':
            return None
        try:
            return float(value)
        except (ValueError, TypeError):
            return None

    result = {
        'pe_ratio': _safe_float(data.get('PERatio')),
        'earnings_growth': _safe_float(data.get('QuarterlyEarningsGrowthYOY')),
        'revenue_growth': _safe_float(data.get('QuarterlyRevenueGrowthYOY')),
        'beta': _safe_float(data.get('Beta'))
    }

    # Cache the result
    _company_overview_cache[symbol] = {
        'data': result,
        'timestamp': time.time()
    }

    log.info(f"Fetched company overview for {symbol}: P/E={result['pe_ratio']}, "
             f"Earnings Growth={result['earnings_growth']}")
    return result
