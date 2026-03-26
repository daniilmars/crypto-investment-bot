"""Trend alignment utilities — multi-timeframe checks and YTD computation."""

from datetime import datetime, timezone
from src.logger import log


def compute_ytd_change(current_price: float, daily_klines: list[dict]) -> float | None:
    """Compute YTD price change from daily klines (Binance format).

    Args:
        current_price: Current market price.
        daily_klines: List of kline dicts with 'timestamp' (ms) and 'open'/'close'.

    Returns:
        YTD change as decimal (e.g., -0.273 for -27.3%), or None if insufficient data.
    """
    if not daily_klines or not current_price:
        return None

    year_start_ms = datetime(datetime.now().year, 1, 1, tzinfo=timezone.utc).timestamp() * 1000

    for k in daily_klines:
        ts = k.get('timestamp', 0)
        if ts >= year_start_ms:
            jan1_price = k.get('open') or k.get('close')
            if jan1_price and jan1_price > 0:
                return (current_price - jan1_price) / jan1_price
            break

    return None


def compute_ytd_changes_from_klines(
    daily_klines_batch: dict[str, list[dict]],
    current_prices: dict[str, float],
) -> dict[str, float]:
    """Compute YTD% for all symbols from daily klines.

    Returns {symbol: ytd_decimal} (e.g., {'SOL': -0.273}).
    """
    result = {}
    for sym, klines in daily_klines_batch.items():
        current = current_prices.get(sym)
        if current and klines:
            ytd = compute_ytd_change(current, klines)
            if ytd is not None:
                result[sym] = ytd
    return result


def compute_ytd_changes_stocks(symbols: list[str], current_prices: dict) -> dict[str, float]:
    """Compute YTD% for stock symbols via yfinance.

    Single batch call, efficient. Returns {symbol: ytd_decimal}.
    """
    if not symbols:
        return {}
    try:
        import yfinance as yf
        # Download YTD data in one batch
        data = yf.download(symbols[:50], period='ytd', progress=False, threads=True)
        if data.empty:
            return {}

        result = {}
        close = data.get('Close')
        if close is None:
            return {}

        for sym in symbols[:50]:
            try:
                if len(symbols) > 1:
                    col = close[sym] if sym in close.columns else None
                else:
                    col = close

                if col is None or col.empty:
                    continue

                first_price = float(col.dropna().iloc[0])
                current = current_prices.get(sym, float(col.dropna().iloc[-1]))
                if first_price > 0:
                    result[sym] = (current - first_price) / first_price
            except (KeyError, IndexError):
                continue

        return result
    except Exception as e:
        log.debug(f"YTD stock computation failed: {e}")
        return {}


def compute_sma(prices: list[float], period: int) -> float | None:
    """Simple moving average from a list of prices."""
    if len(prices) < period:
        return None
    return sum(prices[-period:]) / period
