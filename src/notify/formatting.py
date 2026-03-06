"""Shared Telegram formatting helpers for sparklines, progress bars, and PnL display."""

import re

# Unicode block chars for sparklines (lowest to highest)
_SPARK_CHARS = '▁▂▃▄▅▆▇█'


def escape_md(text: str) -> str:
    """Escape Telegram Markdown v1 special characters in user-facing text."""
    return re.sub(r'([_*\[\]()~`>#+\-=|{}.!])', r'\\\1', str(text))


def text_sparkline(values: list, width: int = 10) -> str:
    """Render a list of numeric values as a Unicode sparkline.

    Args:
        values: list of numbers (at least 2 for meaningful output)
        width: target character width (values are resampled if needed)

    Returns:
        String of Unicode block chars, e.g. '▁▃▅▇█▆▃▁'
    """
    if not values or len(values) < 2:
        return '▁' * width

    # Resample to target width if needed
    if len(values) > width:
        step = len(values) / width
        values = [values[int(i * step)] for i in range(width)]
    elif len(values) < width:
        # Use available values as-is (don't pad)
        pass

    v_min = min(values)
    v_max = max(values)
    spread = v_max - v_min

    if spread == 0:
        return _SPARK_CHARS[4] * len(values)

    result = []
    for v in values:
        idx = int((v - v_min) / spread * (len(_SPARK_CHARS) - 1))
        idx = min(idx, len(_SPARK_CHARS) - 1)
        result.append(_SPARK_CHARS[idx])

    return ''.join(result)


def progress_bar(current: int, maximum: int, width: int = 10) -> str:
    """Render a progress bar using block chars.

    Args:
        current: filled portion
        maximum: total capacity
        width: character width of the bar

    Returns:
        String like '██████░░░░'
    """
    if maximum <= 0:
        return '░' * width
    filled = min(int(current / maximum * width), width)
    return '█' * filled + '░' * (width - filled)


def pnl_emoji(pnl_pct: float) -> str:
    """Returns a green/red/neutral indicator based on PnL percentage."""
    if pnl_pct > 1.0:
        return '🟢'
    elif pnl_pct < -1.0:
        return '🔴'
    return '⚪'


def pnl_sign(pnl_pct: float) -> str:
    """Returns +/- formatted PnL string."""
    return f"{pnl_pct:+.1f}%"


def format_position_line(symbol: str, pnl_pct: float, price: float,
                         sparkline: str = '') -> str:
    """Format a single position as a compact one-line display.

    Returns:
        String like '🟢 NVDA  +3.2% $142.50  ▁▃▅▇█'
    """
    emoji = pnl_emoji(pnl_pct)
    spark_part = f'  {sparkline}' if sparkline else ''
    return f"{emoji} {symbol}  {pnl_pct:+.1f}% ${price:,.2f}{spark_part}"


def truncate_for_telegram(text: str, max_len: int = 4096) -> str:
    """Truncate text to fit Telegram's message limit, cutting at a line boundary.

    Args:
        text: message text
        max_len: Telegram max message length (default 4096)

    Returns:
        Truncated text with '...' suffix if truncated.
    """
    if len(text) <= max_len:
        return text

    # Reserve space for truncation indicator
    limit = max_len - 20
    truncated = text[:limit]

    # Cut at last newline to avoid breaking formatting
    last_nl = truncated.rfind('\n')
    if last_nl > limit // 2:
        truncated = truncated[:last_nl]

    return truncated + '\n\n_...truncated_'


def format_region_label(symbol: str) -> str:
    """Determine region label for a stock symbol based on suffix/convention."""
    # Common EU exchanges
    eu_suffixes = ('.DE', '.PA', '.AS', '.MI', '.MC', '.L', '.SW', '.BR',
                   '.VI', '.HE', '.CO', '.ST', '.OL', '.WA', '.LS')
    asia_suffixes = ('.T', '.HK', '.SS', '.SZ', '.KS', '.TW', '.SI',
                     '.AX', '.NZ', '.BO', '.NS')

    upper = symbol.upper()
    for suffix in eu_suffixes:
        if upper.endswith(suffix):
            return 'EU'
    for suffix in asia_suffixes:
        if upper.endswith(suffix):
            return 'Asia'
    return 'US'
