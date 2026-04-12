"""Auto-adds newly listed tickers to the runtime stock watchlist.

Queries ipo_events for recent 'listed' events that haven't been added yet,
validates the ticker exists on a US exchange via yfinance, and appends to
the runtime watchlist (does NOT modify YAML).
"""

import re

from src.database import get_ipo_events, mark_ipo_watchlist_added
from src.logger import log


def _validate_ticker(ticker: str) -> bool:
    """Check if a ticker is trading on a US exchange via yfinance."""
    try:
        import yfinance as yf
        info = yf.Ticker(ticker).info
        # yfinance returns an empty dict or dict with just 'trailingPegRatio' for invalid tickers
        return bool(info and info.get('regularMarketPrice'))
    except Exception as e:
        log.debug(f"[IPO] Ticker validation failed for {ticker}: {e}")
        return False


def promote_new_listings(settings: dict) -> list:
    """Auto-adds recently listed tickers to the runtime stock watchlist.

    Args:
        settings: The full settings dict (settings['stock_trading']['watch_list'] is modified in place).

    Returns:
        list of ticker strings that were newly added.
    """
    events = get_ipo_events(status='listed', since_hours=72)
    if not events:
        return []

    stock_trading = settings.get('stock_trading', {})
    watch_list = stock_trading.get('watch_list', [])
    current_tickers = {t.upper() for t in watch_list}

    ipo_cfg = settings.get('ipo_tracking', {})
    validate = ipo_cfg.get('validate_ticker', True)

    added = []
    for event in events:
        if event.get('auto_added_to_watchlist'):
            continue

        ticker = event.get('ticker')
        if not ticker:
            continue

        ticker = ticker.upper().strip()
        if not re.match(r'^[A-Z]{1,5}$', ticker):
            continue

        if ticker in current_tickers:
            mark_ipo_watchlist_added(event['id'])
            continue

        # Validate ticker on exchange
        if validate and not _validate_ticker(ticker):
            log.info(f"[IPO] Ticker {ticker} not yet trading, will retry next cycle.")
            continue

        # Add to runtime watchlist
        watch_list.append(ticker)
        current_tickers.add(ticker)
        mark_ipo_watchlist_added(event['id'])

        # Add to SYMBOL_KEYWORDS at runtime for news matching. Short tickers
        # (<4 chars) must go through SYMBOL_REQUIRED_CONTEXT co-occurrence
        # gating to prevent substring false positives.
        try:
            from src.collectors.news_data import (
                SYMBOL_KEYWORDS, SYMBOL_REQUIRED_CONTEXT,
                _KEYWORD_PATTERNS, _compile_keyword,
            )
            company = (event.get('company_name') or '').strip()
            if ticker in SYMBOL_KEYWORDS or ticker in SYMBOL_REQUIRED_CONTEXT:
                pass  # already registered
            elif company and len(company) >= 4:
                keywords = [company, f"{company} stock"]
                SYMBOL_KEYWORDS[ticker] = keywords
                _KEYWORD_PATTERNS[ticker] = [_compile_keyword(kw) for kw in keywords]
                if len(ticker) < 4:
                    # Short ticker: allow bare ticker only with company context
                    SYMBOL_REQUIRED_CONTEXT[ticker] = {
                        'anchor': _compile_keyword(ticker),
                        'context': [_compile_keyword(company)],
                    }
            elif len(ticker) >= 4:
                # Long ticker, no usable company name: register ticker alone
                SYMBOL_KEYWORDS[ticker] = [ticker]
                _KEYWORD_PATTERNS[ticker] = [_compile_keyword(ticker)]
            else:
                log.info(f"[IPO] Skipping keyword registration for short "
                         f"ticker {ticker!r} with no usable company name.")
        except Exception as e:
            log.debug(f"[IPO] Could not add keywords for {ticker}: {e}")

        added.append(ticker)
        log.info(f"[IPO] Added {ticker} ({event.get('company_name', '?')}) to watchlist")

    # Persist watchlist back to settings in case it was modified
    if added:
        stock_trading['watch_list'] = watch_list
        settings['stock_trading'] = stock_trading

    return added
