"""Shared Telegram formatting helpers for sparklines, progress bars, and PnL display."""

import re

# Unicode block chars for sparklines (lowest to highest)
_SPARK_CHARS = '▁▂▃▄▅▆▇█'


def escape_md(text: str) -> str:
    """Escape Telegram Markdown v1 special characters in user-facing text.

    Markdown v1 only uses *bold*, _italic_, `code`, and [text](url).
    Only these four chars need escaping in regular text.
    """
    return re.sub(r'([_*`\[])', r'\\\1', str(text))


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


# Ticker → short display name for Telegram readability.
# Only needed for codes that aren't self-explanatory (Asian numeric codes,
# some EU tickers). US/well-known symbols are omitted — they pass through as-is.
_STOCK_NAMES: dict[str, str] = {
    # Japan (.T)
    '7203.T': 'Toyota', '6758.T': 'Sony', '9984.T': 'SoftBank',
    '6861.T': 'Keyence', '8306.T': 'MUFG', '6902.T': 'Denso',
    '7741.T': 'HOYA', '4063.T': 'Shin-Etsu', '6501.T': 'Hitachi',
    '8035.T': 'Tokyo Electron', '9432.T': 'NTT', '6367.T': 'Daikin',
    '7267.T': 'Honda', '4519.T': 'Chugai Pharma', '6954.T': 'Fanuc',
    '6098.T': 'Recruit', '9983.T': 'Uniqlo', '4502.T': 'Takeda',
    '8058.T': 'Mitsubishi Corp', '8001.T': 'ITOCHU', '6273.T': 'SMC',
    '4568.T': 'Daiichi Sankyo', '7974.T': 'Nintendo', '9999.HK': 'NetEase',
    # Hong Kong (.HK)
    '0700.HK': 'Tencent', '9988.HK': 'Alibaba', '1299.HK': 'AIA',
    '0005.HK': 'HSBC HK', '3690.HK': 'Meituan', '0941.HK': 'China Mobile',
    '1211.HK': 'BYD', '2318.HK': 'Ping An', '0388.HK': 'HKEX',
    '9618.HK': 'JD.com', '1810.HK': 'Xiaomi', '2020.HK': 'Anta Sports',
    '3968.HK': 'CM Bank', '2269.HK': 'WuXi Bio', '1024.HK': 'Kuaishou',
    '3888.HK': 'Kingsoft', '2382.HK': 'Sunny Optical',
    # South Korea (.KS)
    '005930.KS': 'Samsung', '000660.KS': 'SK Hynix',
    '373220.KS': 'LG Energy', '035420.KS': 'Naver',
    '005380.KS': 'Hyundai Motor', '051910.KS': 'LG Chem',
    '066570.KS': 'LG Electronics', '028260.KS': 'Samsung C&T',
    # Taiwan (.TW)
    '2330.TW': 'TSMC', '2317.TW': 'Foxconn', '2454.TW': 'MediaTek',
    '2308.TW': 'Delta Elec', '2603.TW': 'Evergreen Marine',
    # India (.NS)
    'RELIANCE.NS': 'Reliance', 'TCS.NS': 'TCS', 'INFY.NS': 'Infosys',
    'HDFCBANK.NS': 'HDFC Bank', 'ICICIBANK.NS': 'ICICI Bank',
    'WIPRO.NS': 'Wipro', 'BAJFINANCE.NS': 'Bajaj Finance',
    'LT.NS': 'Larsen&Toubro', 'HINDUNILVR.NS': 'Hindustan Unilever',
    'SBIN.NS': 'SBI',
    # Australia (.AX)
    'BHP.AX': 'BHP', 'CBA.AX': 'CommBank', 'CSL.AX': 'CSL',
    'WBC.AX': 'Westpac', 'NAB.AX': 'NAB', 'FMG.AX': 'Fortescue',
    'MQG.AX': 'Macquarie', 'WDS.AX': 'Woodside', 'ALL.AX': 'Aristocrat',
    # Singapore (.SI)
    'D05.SI': 'DBS', 'O39.SI': 'OCBC', 'U11.SI': 'UOB',
    # EU — only the less obvious ones
    'HSBA.L': 'HSBC', 'ULVR.L': 'Unilever', 'LSEG.L': 'LSE Group',
    'DGE.L': 'Diageo', 'GLEN.L': 'Glencore', 'AAL.L': 'Anglo American',
    'NG.L': 'Natl Grid', 'BARC.L': 'Barclays', 'LLOY.L': 'Lloyds',
    'VOD.L': 'Vodafone', 'PRU.L': 'Prudential', 'BATS.L': 'BAT',
    'EXPN.L': 'Experian', 'RKT.L': 'Reckitt', 'WPP.L': 'WPP',
    'FLTR.L': 'Flutter', 'IHG.L': 'IHG Hotels',
    'SAP.DE': 'SAP', 'SIE.DE': 'Siemens', 'ALV.DE': 'Allianz', 'DTE.DE': 'Deutsche Telekom',
    'BAS.DE': 'BASF', 'MBG.DE': 'Mercedes', 'BMW.DE': 'BMW',
    'VOW3.DE': 'VW', 'MUV2.DE': 'Munich Re', 'IFX.DE': 'Infineon',
    'ADS.DE': 'adidas', 'HEN3.DE': 'Henkel', 'FRE.DE': 'Fresenius',
    'DB1.DE': 'Deutsche Boerse', 'RHM.DE': 'Rheinmetall',
    'MC.PA': 'LVMH', 'TTE.PA': 'TotalEnergies', 'SAN.PA': 'Sanofi',
    'OR.PA': 'L\'Oreal', 'AI.PA': 'Air Liquide', 'BNP.PA': 'BNP Paribas',
    'SU.PA': 'Schneider', 'AIR.PA': 'Airbus', 'SAF.PA': 'Safran',
    'CS.PA': 'AXA', 'DSY.PA': 'Dassault Sys', 'KER.PA': 'Kering',
    'RMS.PA': 'Hermes', 'EL.PA': 'EssilorLuxottica',
    'ASML.AS': 'ASML', 'INGA.AS': 'ING', 'PHIA.AS': 'Philips',
    'WKL.AS': 'Wolters Kluwer', 'ADYEN.AS': 'Adyen', 'AD.AS': 'Ahold',
    'NESN.SW': 'Nestle', 'ROG.SW': 'Roche', 'NOVN.SW': 'Novartis',
    'UBSG.SW': 'UBS', 'ZURN.SW': 'Zurich Insurance',
    'SAN.MC': 'Santander', 'ITX.MC': 'Inditex', 'IBE.MC': 'Iberdrola',
    'BBVA.MC': 'BBVA',
    'ENI.MI': 'ENI', 'ENEL.MI': 'Enel', 'UCG.MI': 'UniCredit',
    'ISP.MI': 'Intesa', 'RACE.MI': 'Ferrari', 'STLAM.MI': 'Stellantis',
    'NOVO-B.CO': 'Novo Nordisk', 'MAERSK-B.CO': 'Maersk',
    'DANSKE.CO': 'Danske Bank', 'ORSTED.CO': 'Orsted',
    'VOLV-B.ST': 'Volvo', 'ERIC-B.ST': 'Ericsson', 'ABB.ST': 'ABB',
    'SAND.ST': 'Sandvik', 'ATCO-A.ST': 'Atlas Copco',
    'NESTE.HE': 'Neste', 'UPM.HE': 'UPM', 'NOKIA.HE': 'Nokia',
    'KNEBV.HE': 'Kone', 'NZYM-B.CO': 'Novozymes',
}


def symbol_display_name(symbol: str) -> str:
    """Return a human-readable label for Telegram messages.

    For mapped symbols: 'Samsung (005930.KS)'
    For unmapped symbols: returns as-is ('AAPL')
    """
    name = _STOCK_NAMES.get(symbol.upper() if '.' not in symbol else symbol)
    if name:
        return f"{name} ({symbol})"
    return symbol


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
