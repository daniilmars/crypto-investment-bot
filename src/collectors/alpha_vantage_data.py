import re
import requests
import json
import time
from src.config import app_config
from src.collectors.binance_data import save_price_data
from src.logger import log

# yfinance fallback (free, no API key)
try:
    import yfinance as yf
    _HAS_YFINANCE = True
except ImportError:
    _HAS_YFINANCE = False

# Stock symbols: 1-15 alphanumeric chars, may include dots/hyphens (e.g. BRK.B, ICICIBANK.NS)
_STOCK_SYMBOL_RE = re.compile(r'^[A-Za-z0-9.\-]{1,15}$')


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
        log.warning(f"No quote data from Alpha Vantage for {symbol}. Trying yfinance fallback.")
        return _get_stock_price_yfinance(symbol)

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


def _get_stock_price_yfinance(symbol):
    """Fallback: fetch stock price via yfinance (no API key needed)."""
    if not _HAS_YFINANCE:
        log.warning("yfinance not installed, cannot use fallback.")
        return None
    try:
        ticker = yf.Ticker(symbol)
        hist = ticker.history(period="2d")
        if hist.empty:
            log.warning(f"yfinance returned no data for {symbol}.")
            return None
        price = float(hist['Close'].iloc[-1])
        volume = float(hist['Volume'].iloc[-1])
        if len(hist) >= 2:
            prev_close = float(hist['Close'].iloc[-2])
            change_percent = ((price - prev_close) / prev_close) * 100
        else:
            change_percent = 0.0
        save_price_data({'symbol': symbol, 'price': price})
        log.info(f"[yfinance] Fetched stock price for {symbol}: ${price:.2f}")
        return {'symbol': symbol, 'price': price, 'volume': volume, 'change_percent': change_percent}
    except Exception as e:
        log.error(f"[yfinance] Error fetching {symbol}: {e}")
        return None


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
        log.warning(f"No daily data from Alpha Vantage for {symbol}. Trying yfinance fallback.")
        return _get_daily_prices_yfinance(symbol)

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


def _get_daily_prices_yfinance(symbol):
    """Fallback: fetch daily prices via yfinance."""
    if not _HAS_YFINANCE:
        log.warning("yfinance not installed, cannot use fallback.")
        return None
    try:
        ticker = yf.Ticker(symbol)
        hist = ticker.history(period="6mo")
        if hist.empty:
            log.warning(f"[yfinance] No daily data for {symbol}.")
            return None
        prices = hist['Close'].tolist()
        volumes = hist['Volume'].tolist()
        log.info(f"[yfinance] Fetched {len(prices)} daily prices for {symbol}.")
        return {'prices': prices, 'volumes': volumes}
    except Exception as e:
        log.error(f"[yfinance] Error fetching daily data for {symbol}: {e}")
        return None


def get_batch_stock_prices(symbols):
    """Fetches current prices for multiple stocks in one yfinance batch call.

    Args:
        symbols: list of ticker strings (e.g. ['AAPL', 'SAP.DE', '7203.T'])

    Returns:
        dict {symbol: {'symbol': str, 'price': float, 'volume': float, 'change_percent': float}}
        Symbols that fail are silently skipped.
    """
    if not _HAS_YFINANCE or not symbols:
        return {}

    try:
        data = yf.download(symbols, period="2d", group_by="ticker", threads=True, progress=False)
        if data.empty:
            log.warning("[yfinance batch] No data returned for any symbols.")
            return {}

        results = {}
        for sym in symbols:
            try:
                if len(symbols) == 1:
                    sym_data = data
                else:
                    sym_data = data[sym]

                if sym_data.empty or sym_data['Close'].dropna().empty:
                    continue

                close_vals = sym_data['Close'].dropna()
                price = float(close_vals.iloc[-1])
                volume = float(sym_data['Volume'].dropna().iloc[-1]) if not sym_data['Volume'].dropna().empty else 0.0

                if len(close_vals) >= 2:
                    prev_close = float(close_vals.iloc[-2])
                    change_percent = ((price - prev_close) / prev_close) * 100 if prev_close > 0 else 0.0
                else:
                    change_percent = 0.0

                if price > 0:
                    save_price_data({'symbol': sym, 'price': price})
                    results[sym] = {
                        'symbol': sym,
                        'price': price,
                        'volume': volume,
                        'change_percent': change_percent,
                    }
            except (KeyError, IndexError, TypeError) as e:
                log.warning(f"[yfinance batch] Skipping {sym}: {e}")
                continue

        log.info(f"[yfinance batch] Fetched prices for {len(results)}/{len(symbols)} stocks.")
        return results

    except Exception as e:
        log.error(f"[yfinance batch] Error fetching batch prices: {e}")
        return {}


def get_batch_daily_prices(symbols):
    """Fetches 6-month daily prices for multiple stocks in one yfinance batch call.

    Args:
        symbols: list of ticker strings

    Returns:
        dict {symbol: {'prices': [float...], 'volumes': [float...]}}
        Oldest-to-newest ordering. Symbols with insufficient data are skipped.
    """
    if not _HAS_YFINANCE or not symbols:
        return {}

    try:
        data = yf.download(symbols, period="6mo", group_by="ticker", threads=True, progress=False)
        if data.empty:
            log.warning("[yfinance batch daily] No data returned for any symbols.")
            return {}

        results = {}
        for sym in symbols:
            try:
                if len(symbols) == 1:
                    sym_data = data
                else:
                    sym_data = data[sym]

                if sym_data.empty:
                    continue

                close_vals = sym_data['Close'].dropna()
                vol_vals = sym_data['Volume'].dropna()

                if len(close_vals) < 20:
                    log.warning(f"[yfinance batch daily] Insufficient data for {sym} ({len(close_vals)} days).")
                    continue

                results[sym] = {
                    'prices': close_vals.tolist(),
                    'volumes': vol_vals.tolist(),
                }
            except (KeyError, IndexError, TypeError) as e:
                log.warning(f"[yfinance batch daily] Skipping {sym}: {e}")
                continue

        log.info(f"[yfinance batch daily] Fetched daily data for {len(results)}/{len(symbols)} stocks.")
        return results

    except Exception as e:
        log.error(f"[yfinance batch daily] Error fetching batch daily prices: {e}")
        return {}


def get_batch_higher_tf_prices(symbols, interval='1wk', period='2y'):
    """Fetch weekly or monthly closes for stocks in one yfinance batch call.

    Args:
        symbols: list of ticker strings
        interval: '1wk' for weekly or '1mo' for monthly
        period: yfinance period string ('2y' is plenty for 20-week / 10-month SMA)

    Returns:
        dict {symbol: [float closes oldest-to-newest]}
    """
    if not _HAS_YFINANCE or not symbols:
        return {}

    try:
        data = yf.download(symbols, period=period, interval=interval,
                           group_by="ticker", threads=True, progress=False)
        if data.empty:
            return {}

        results = {}
        for sym in symbols:
            try:
                sym_data = data if len(symbols) == 1 else data[sym]
                if sym_data.empty:
                    continue
                closes = sym_data['Close'].dropna()
                if len(closes) < 5:
                    continue
                results[sym] = closes.tolist()
            except (KeyError, IndexError, TypeError):
                continue

        log.info(f"[yfinance batch {interval}] Fetched for {len(results)}/{len(symbols)} stocks.")
        return results
    except Exception as e:
        log.error(f"[yfinance batch {interval}] Error: {e}")
        return {}


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
