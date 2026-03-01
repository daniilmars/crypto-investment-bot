"""
Event Calendar — gates BUY/INCREASE signals around known market-moving events.

Data sources:
- Earnings dates: yfinance Ticker.calendar (24h cache per symbol)
- FOMC 2026: hardcoded 8 dates
- CPI 2026: hardcoded 12 dates

Never blocks exits (SL/TP/trailing stop).
"""

import time
from datetime import datetime, timezone, timedelta

import yfinance as yf

from src.config import app_config
from src.logger import log


# FOMC announcement dates 2026 (Federal Reserve meeting conclusions)
FOMC_DATES_2026 = [
    datetime(2026, 1, 28, 19, 0, tzinfo=timezone.utc),   # Jan
    datetime(2026, 3, 18, 18, 0, tzinfo=timezone.utc),    # Mar
    datetime(2026, 5, 6, 18, 0, tzinfo=timezone.utc),     # May
    datetime(2026, 6, 17, 18, 0, tzinfo=timezone.utc),    # Jun
    datetime(2026, 7, 29, 18, 0, tzinfo=timezone.utc),    # Jul
    datetime(2026, 9, 16, 18, 0, tzinfo=timezone.utc),    # Sep
    datetime(2026, 11, 4, 18, 0, tzinfo=timezone.utc),    # Nov
    datetime(2026, 12, 16, 19, 0, tzinfo=timezone.utc),   # Dec
]

# CPI release dates 2026 (Bureau of Labor Statistics, 8:30 AM ET = 13:30 UTC)
CPI_DATES_2026 = [
    datetime(2026, 1, 14, 13, 30, tzinfo=timezone.utc),   # Jan
    datetime(2026, 2, 12, 13, 30, tzinfo=timezone.utc),   # Feb
    datetime(2026, 3, 11, 13, 30, tzinfo=timezone.utc),   # Mar
    datetime(2026, 4, 14, 12, 30, tzinfo=timezone.utc),   # Apr (DST)
    datetime(2026, 5, 12, 12, 30, tzinfo=timezone.utc),   # May
    datetime(2026, 6, 10, 12, 30, tzinfo=timezone.utc),   # Jun
    datetime(2026, 7, 14, 12, 30, tzinfo=timezone.utc),   # Jul
    datetime(2026, 8, 12, 12, 30, tzinfo=timezone.utc),   # Aug
    datetime(2026, 9, 15, 12, 30, tzinfo=timezone.utc),   # Sep
    datetime(2026, 10, 13, 12, 30, tzinfo=timezone.utc),  # Oct
    datetime(2026, 11, 12, 13, 30, tzinfo=timezone.utc),  # Nov (DST end)
    datetime(2026, 12, 10, 13, 30, tzinfo=timezone.utc),  # Dec
]

# Earnings cache: {symbol: {earnings_date: datetime|None, fetched_at: float}}
_earnings_cache = {}
_EARNINGS_CACHE_TTL = 24 * 3600  # 24 hours

# Warning cooldown: {symbol: last_warned_at}
_warning_cooldown = {}
_WARNING_COOLDOWN_SECONDS = 12 * 3600  # 12 hours


def check_event_gate(symbol: str, signal_type: str,
                     asset_type: str = 'crypto') -> tuple:
    """Check if an upcoming event should gate this signal.

    Only called for BUY and INCREASE signals. Never blocks exits.

    Args:
        symbol: Ticker symbol.
        signal_type: 'BUY' or 'INCREASE'.
        asset_type: 'crypto' or 'stock'.

    Returns:
        (action: str, size_multiplier: float, reason: str)
        action is 'allow', 'reduce', or 'block'.
    """
    cfg = app_config.get('settings', {}).get('event_calendar', {})
    if not cfg.get('enabled', True):
        return 'allow', 1.0, ''

    now = datetime.now(timezone.utc)

    # Check earnings (stocks only)
    if asset_type == 'stock':
        earnings_cfg = cfg.get('earnings', {})
        if earnings_cfg.get('enabled', True):
            action, mult, reason = _check_earnings_gate(symbol, now, earnings_cfg)
            if action != 'allow':
                return action, mult, reason

    # Check FOMC (all assets)
    fomc_cfg = cfg.get('fomc', {})
    if fomc_cfg.get('enabled', True):
        action, mult, reason = _check_macro_event_gate(
            now, FOMC_DATES_2026, 'FOMC', fomc_cfg)
        if action != 'allow':
            return action, mult, reason

    # Check CPI (all assets)
    cpi_cfg = cfg.get('cpi', {})
    if cpi_cfg.get('enabled', True):
        action, mult, reason = _check_macro_event_gate(
            now, CPI_DATES_2026, 'CPI', cpi_cfg)
        if action != 'allow':
            return action, mult, reason

    return 'allow', 1.0, ''


def _check_earnings_gate(symbol: str, now: datetime, cfg: dict) -> tuple:
    """Check earnings proximity for a stock symbol."""
    block_hours = cfg.get('block_hours_before', 24)
    reduce_hours = cfg.get('reduce_hours_before', 48)
    reduce_mult = cfg.get('reduce_multiplier', 0.5)

    earnings_dt = _get_earnings_date(symbol)
    if earnings_dt is None:
        return 'allow', 1.0, ''

    hours_until = (earnings_dt - now).total_seconds() / 3600

    if hours_until < 0:
        # Earnings already passed
        return 'allow', 1.0, ''
    elif hours_until <= block_hours:
        return 'block', 0.0, (f"Earnings for {symbol} in "
                              f"{hours_until:.0f}h (block window: {block_hours}h)")
    elif hours_until <= reduce_hours:
        return 'reduce', reduce_mult, (f"Earnings for {symbol} in "
                                        f"{hours_until:.0f}h (reduce window: {reduce_hours}h)")

    return 'allow', 1.0, ''


def _check_macro_event_gate(now: datetime, event_dates: list,
                            event_name: str, cfg: dict) -> tuple:
    """Check proximity to a macro event (FOMC/CPI)."""
    block_hours = cfg.get('block_hours_before', 24)
    reduce_hours = cfg.get('reduce_hours_before', 48)
    reduce_mult = cfg.get('reduce_multiplier', 0.5)

    next_event = _get_next_event(now, event_dates)
    if next_event is None:
        return 'allow', 1.0, ''

    hours_until = (next_event - now).total_seconds() / 3600

    if hours_until <= block_hours:
        return 'block', 0.0, (f"{event_name} in {hours_until:.0f}h "
                              f"(block window: {block_hours}h)")
    elif hours_until <= reduce_hours:
        return 'reduce', reduce_mult, (f"{event_name} in {hours_until:.0f}h "
                                        f"(reduce window: {reduce_hours}h)")

    return 'allow', 1.0, ''


def _get_next_event(now: datetime, event_dates: list) -> datetime | None:
    """Returns the next upcoming event datetime, or None if all have passed."""
    for dt in event_dates:
        if dt > now:
            return dt
    return None


def _get_earnings_date(symbol: str) -> datetime | None:
    """Fetches the next earnings date for a stock, with 24h caching."""
    now = time.time()
    cached = _earnings_cache.get(symbol)
    if cached and (now - cached['fetched_at']) < _EARNINGS_CACHE_TTL:
        return cached['earnings_date']

    earnings_dt = None
    try:
        ticker = yf.Ticker(symbol)
        cal = ticker.calendar
        if cal is not None:
            # yfinance returns a dict or DataFrame depending on version
            if isinstance(cal, dict):
                earnings_raw = cal.get('Earnings Date')
                if earnings_raw:
                    if isinstance(earnings_raw, list) and len(earnings_raw) > 0:
                        earnings_dt = _parse_earnings_date(earnings_raw[0])
                    else:
                        earnings_dt = _parse_earnings_date(earnings_raw)
            elif hasattr(cal, 'iloc'):
                # DataFrame format
                if 'Earnings Date' in cal.index:
                    val = cal.loc['Earnings Date'].iloc[0]
                    earnings_dt = _parse_earnings_date(val)
    except Exception as e:
        log.debug(f"Could not fetch earnings for {symbol}: {e}")

    _earnings_cache[symbol] = {
        'earnings_date': earnings_dt,
        'fetched_at': now,
    }
    return earnings_dt


def _parse_earnings_date(raw) -> datetime | None:
    """Parse various earnings date formats into a timezone-aware datetime."""
    if raw is None:
        return None
    if isinstance(raw, datetime):
        if raw.tzinfo is None:
            return raw.replace(tzinfo=timezone.utc)
        return raw
    # Handle pandas Timestamp
    if hasattr(raw, 'to_pydatetime'):
        dt = raw.to_pydatetime()
        if dt.tzinfo is None:
            return dt.replace(tzinfo=timezone.utc)
        return dt
    # Try string parsing
    try:
        dt = datetime.fromisoformat(str(raw))
        if dt.tzinfo is None:
            return dt.replace(tzinfo=timezone.utc)
        return dt
    except (ValueError, TypeError):
        return None


def get_event_warnings_for_positions(open_positions: list,
                                     lookahead_hours: int = 72) -> list:
    """Returns warnings for open positions with upcoming events.

    Returns list of dicts: {symbol, event_type, event_date, hours_until}
    """
    cfg = app_config.get('settings', {}).get('event_calendar', {})
    if not cfg.get('enabled', True):
        return []

    warnings_cfg = cfg.get('position_warnings', {})
    if not warnings_cfg.get('enabled', True):
        return []

    lookahead = warnings_cfg.get('lookahead_hours', lookahead_hours)
    now = datetime.now(timezone.utc)
    warnings = []

    for pos in open_positions:
        if pos.get('status') != 'OPEN':
            continue
        symbol = pos.get('symbol', '')
        asset_type = pos.get('asset_type', 'crypto')

        # Check per-symbol cooldown
        last_warned = _warning_cooldown.get(symbol, 0)
        if time.time() - last_warned < _WARNING_COOLDOWN_SECONDS:
            continue

        # Earnings (stocks only)
        if asset_type == 'stock':
            earnings_dt = _get_earnings_date(symbol)
            if earnings_dt:
                hours_until = (earnings_dt - now).total_seconds() / 3600
                if 0 < hours_until <= lookahead:
                    warnings.append({
                        'symbol': symbol,
                        'event_type': 'Earnings',
                        'event_date': earnings_dt,
                        'hours_until': hours_until,
                    })
                    _warning_cooldown[symbol] = time.time()

        # FOMC
        next_fomc = _get_next_event(now, FOMC_DATES_2026)
        if next_fomc:
            hours_until = (next_fomc - now).total_seconds() / 3600
            if 0 < hours_until <= lookahead:
                warnings.append({
                    'symbol': symbol,
                    'event_type': 'FOMC',
                    'event_date': next_fomc,
                    'hours_until': hours_until,
                })

        # CPI
        next_cpi = _get_next_event(now, CPI_DATES_2026)
        if next_cpi:
            hours_until = (next_cpi - now).total_seconds() / 3600
            if 0 < hours_until <= lookahead:
                warnings.append({
                    'symbol': symbol,
                    'event_type': 'CPI',
                    'event_date': next_cpi,
                    'hours_until': hours_until,
                })

    return warnings


def get_upcoming_macro_events(days_ahead: int = 30) -> list:
    """Returns upcoming FOMC and CPI events for the /events command.

    Returns list of dicts: {event_type, event_date, hours_until}
    """
    now = datetime.now(timezone.utc)
    cutoff = now + timedelta(days=days_ahead)
    events = []

    for dt in FOMC_DATES_2026:
        if now < dt <= cutoff:
            events.append({
                'event_type': 'FOMC',
                'event_date': dt,
                'hours_until': (dt - now).total_seconds() / 3600,
            })

    for dt in CPI_DATES_2026:
        if now < dt <= cutoff:
            events.append({
                'event_type': 'CPI',
                'event_date': dt,
                'hours_until': (dt - now).total_seconds() / 3600,
            })

    # Check if we're past all hardcoded dates
    all_dates = FOMC_DATES_2026 + CPI_DATES_2026
    if all_dates and now > max(all_dates):
        log.warning("Event calendar: All hardcoded 2026 dates have expired. "
                     "Update FOMC_DATES and CPI_DATES for the new year.")

    events.sort(key=lambda e: e['event_date'])
    return events


def clear_event_cache():
    """Clears earnings and warning caches. For tests."""
    _earnings_cache.clear()
    _warning_cooldown.clear()
