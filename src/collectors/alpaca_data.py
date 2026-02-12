import os

from src.config import app_config
from src.logger import log

_stock_client = None


def _get_stock_client():
    """Returns an Alpaca StockHistoricalDataClient (no auth needed for market data)."""
    global _stock_client
    if _stock_client is not None:
        return _stock_client
    try:
        from alpaca.data.historical import StockHistoricalDataClient
        api_key = app_config.get('api_keys', {}).get('alpaca', {}).get('api_key')
        api_secret = app_config.get('api_keys', {}).get('alpaca', {}).get('api_secret')
        # Alpaca data API works without keys for IEX feed, but keys unlock SIP feed
        _stock_client = StockHistoricalDataClient(api_key or None, api_secret or None)
        log.info("Initialized Alpaca StockHistoricalDataClient.")
        return _stock_client
    except ImportError:
        log.error("alpaca-py is not installed. Run: pip install alpaca-py")
        return None
    except Exception as e:
        log.error(f"Failed to initialize Alpaca data client: {e}")
        return None


def get_stock_price_alpaca(symbol):
    """
    Fetches the latest stock quote via Alpaca.

    Returns:
        dict with 'symbol', 'price', 'volume', 'change_percent' or None on failure.
    """
    client = _get_stock_client()
    if not client:
        return None
    try:
        from alpaca.data.requests import StockLatestQuoteRequest, StockBarsRequest
        from alpaca.data.timeframe import TimeFrame
        from datetime import datetime, timedelta

        # Get latest quote for current price
        request = StockLatestQuoteRequest(symbol_or_symbols=symbol)
        quotes = client.get_stock_latest_quote(request)
        quote = quotes.get(symbol)
        if not quote:
            log.warning(f"No Alpaca quote data for {symbol}.")
            return None

        price = float(quote.ask_price + quote.bid_price) / 2
        if price <= 0:
            price = float(quote.ask_price or quote.bid_price)

        # Get yesterday's close for change_percent via daily bars
        end = datetime.now()
        start = end - timedelta(days=5)
        bars_request = StockBarsRequest(
            symbol_or_symbols=symbol,
            timeframe=TimeFrame.Day,
            start=start,
            end=end,
            limit=2
        )
        bars = client.get_stock_bars(bars_request)
        bar_list = bars[symbol] if symbol in bars else []

        volume = 0
        change_percent = 0
        if len(bar_list) >= 2:
            prev_close = float(bar_list[-2].close)
            volume = float(bar_list[-1].volume)
            if prev_close > 0:
                change_percent = ((price - prev_close) / prev_close) * 100
        elif len(bar_list) == 1:
            volume = float(bar_list[-1].volume)

        log.info(f"Alpaca quote for {symbol}: ${price:.2f} (change {change_percent:+.2f}%)")
        return {
            'symbol': symbol,
            'price': price,
            'volume': volume,
            'change_percent': change_percent
        }
    except Exception as e:
        log.error(f"Error fetching Alpaca quote for {symbol}: {e}")
        return None


def get_daily_prices_alpaca(symbol, limit=100):
    """
    Fetches daily bars from Alpaca. Returns same format as alpha_vantage_data.get_daily_prices().

    Returns:
        dict with 'prices' (list of floats, oldest->newest) and 'volumes' (list of floats)
        or None on failure.
    """
    client = _get_stock_client()
    if not client:
        return None
    try:
        from alpaca.data.requests import StockBarsRequest
        from alpaca.data.timeframe import TimeFrame
        from datetime import datetime, timedelta

        end = datetime.now()
        start = end - timedelta(days=int(limit * 1.5))  # fetch extra to account for weekends

        request = StockBarsRequest(
            symbol_or_symbols=symbol,
            timeframe=TimeFrame.Day,
            start=start,
            end=end,
            limit=limit
        )
        bars = client.get_stock_bars(request)
        bar_list = bars[symbol] if symbol in bars else []

        if not bar_list:
            log.warning(f"No Alpaca daily bars for {symbol}.")
            return None

        prices = [float(bar.close) for bar in bar_list]
        volumes = [float(bar.volume) for bar in bar_list]

        log.info(f"Fetched {len(prices)} daily prices from Alpaca for {symbol}.")
        return {'prices': prices, 'volumes': volumes}
    except Exception as e:
        log.error(f"Error fetching Alpaca daily prices for {symbol}: {e}")
        return None


def get_intraday_prices_alpaca(symbol, timeframe_minutes=60, limit=100):
    """
    Fetches intraday bars from Alpaca for more granular analysis.

    Returns:
        dict with 'prices', 'volumes', 'timestamps' or None on failure.
    """
    client = _get_stock_client()
    if not client:
        return None
    try:
        from alpaca.data.requests import StockBarsRequest
        from alpaca.data.timeframe import TimeFrame
        from datetime import datetime, timedelta

        tf_map = {1: TimeFrame.Minute, 5: TimeFrame.Minute, 15: TimeFrame.Minute, 60: TimeFrame.Hour}
        timeframe = tf_map.get(timeframe_minutes, TimeFrame.Hour)

        end = datetime.now()
        start = end - timedelta(hours=limit * (timeframe_minutes / 60))

        request = StockBarsRequest(
            symbol_or_symbols=symbol,
            timeframe=timeframe,
            start=start,
            end=end,
            limit=limit
        )
        bars = client.get_stock_bars(request)
        bar_list = bars[symbol] if symbol in bars else []

        if not bar_list:
            return None

        return {
            'prices': [float(bar.close) for bar in bar_list],
            'volumes': [float(bar.volume) for bar in bar_list],
            'timestamps': [bar.timestamp for bar in bar_list],
        }
    except Exception as e:
        log.error(f"Error fetching Alpaca intraday prices for {symbol}: {e}")
        return None
