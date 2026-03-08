"""
Event Calendar — gates BUY/INCREASE signals around known market-moving events.

Data sources:
- Earnings dates: yfinance Ticker.calendar (24h cache per symbol)
- FOMC / CPI: loaded from config/event_dates.yaml (fallback to hardcoded 2026 dates)

Never blocks exits (SL/TP/trailing stop).
"""

import os
import time
from datetime import datetime, timezone, timedelta

import yaml
import yfinance as yf

from src.config import app_config
from src.logger import log


# --- Hardcoded fallback dates (2026) ---
_FOMC_DATES_2026_FALLBACK = [
    datetime(2026, 1, 28, 19, 0, tzinfo=timezone.utc),
    datetime(2026, 3, 18, 18, 0, tzinfo=timezone.utc),
    datetime(2026, 5, 6, 18, 0, tzinfo=timezone.utc),
    datetime(2026, 6, 17, 18, 0, tzinfo=timezone.utc),
    datetime(2026, 7, 29, 18, 0, tzinfo=timezone.utc),
    datetime(2026, 9, 16, 18, 0, tzinfo=timezone.utc),
    datetime(2026, 11, 4, 18, 0, tzinfo=timezone.utc),
    datetime(2026, 12, 16, 19, 0, tzinfo=timezone.utc),
]

_CPI_DATES_2026_FALLBACK = [
    datetime(2026, 1, 14, 13, 30, tzinfo=timezone.utc),
    datetime(2026, 2, 12, 13, 30, tzinfo=timezone.utc),
    datetime(2026, 3, 11, 13, 30, tzinfo=timezone.utc),
    datetime(2026, 4, 14, 12, 30, tzinfo=timezone.utc),
    datetime(2026, 5, 12, 12, 30, tzinfo=timezone.utc),
    datetime(2026, 6, 10, 12, 30, tzinfo=timezone.utc),
    datetime(2026, 7, 14, 12, 30, tzinfo=timezone.utc),
    datetime(2026, 8, 12, 12, 30, tzinfo=timezone.utc),
    datetime(2026, 9, 15, 12, 30, tzinfo=timezone.utc),
    datetime(2026, 10, 13, 12, 30, tzinfo=timezone.utc),
    datetime(2026, 11, 12, 13, 30, tzinfo=timezone.utc),
    datetime(2026, 12, 10, 13, 30, tzinfo=timezone.utc),
]

# --- Dynamic event date loading ---
_loaded_event_dates: dict | None = None


def _load_event_dates() -> dict:
    """Loads event dates from config/event_dates.yaml, falls back to hardcoded."""
    global _loaded_event_dates
    if _loaded_event_dates is not None:
        return _loaded_event_dates

    script_dir = os.path.dirname(os.path.abspath(__file__))
    yaml_path = os.path.join(script_dir, '..', '..', 'config', 'event_dates.yaml')

    fomc_dates = []
    cpi_dates = []

    try:
        with open(yaml_path, 'r', encoding='utf-8') as f:
            data = yaml.safe_load(f) or {}

        fomc_raw = data.get('fomc', {})
        cpi_raw = data.get('cpi', {})

        for year_dates in fomc_raw.values():
            if isinstance(year_dates, list):
                for ds in year_dates:
                    fomc_dates.append(_parse_iso_date(ds))

        for year_dates in cpi_raw.values():
            if isinstance(year_dates, list):
                for ds in year_dates:
                    cpi_dates.append(_parse_iso_date(ds))

        fomc_dates = sorted([d for d in fomc_dates if d is not None])
        cpi_dates = sorted([d for d in cpi_dates if d is not None])

        if fomc_dates or cpi_dates:
            log.info(f"Loaded event_dates.yaml: {len(fomc_dates)} FOMC, {len(cpi_dates)} CPI dates")
        else:
            log.warning("event_dates.yaml loaded but empty — using hardcoded fallback")
            fomc_dates = _FOMC_DATES_2026_FALLBACK
            cpi_dates = _CPI_DATES_2026_FALLBACK

    except FileNotFoundError:
        log.info("config/event_dates.yaml not found — using hardcoded 2026 dates")
        fomc_dates = _FOMC_DATES_2026_FALLBACK
        cpi_dates = _CPI_DATES_2026_FALLBACK
    except Exception as e:
        log.warning(f"Failed to load event_dates.yaml: {e} — using hardcoded fallback")
        fomc_dates = _FOMC_DATES_2026_FALLBACK
        cpi_dates = _CPI_DATES_2026_FALLBACK

    _loaded_event_dates = {'fomc': fomc_dates, 'cpi': cpi_dates}
    return _loaded_event_dates


def _parse_iso_date(s: str) -> datetime | None:
    """Parse an ISO date string (with Z timezone) to a tz-aware datetime."""
    try:
        s = s.replace('Z', '+00:00')
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except (ValueError, TypeError, AttributeError):
        return None


def _get_fomc_dates() -> list:
    return _load_event_dates()['fomc']


def _get_cpi_dates() -> list:
    return _load_event_dates()['cpi']


def reload_event_dates():
    """Force-reloads event dates from YAML. For tests and optional Telegram command."""
    global _loaded_event_dates
    _loaded_event_dates = None
    _load_event_dates()


# Backwards-compatible aliases for external consumers (tests, market_alerts)
# These are lazily evaluated properties, but since tests import at module level,
# we provide them as module-level references to the fallback lists.
# The actual runtime code uses _get_fomc_dates() / _get_cpi_dates() which load from YAML.
FOMC_DATES_2026 = _FOMC_DATES_2026_FALLBACK
CPI_DATES_2026 = _CPI_DATES_2026_FALLBACK


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
            now, _get_fomc_dates(), 'FOMC', fomc_cfg)
        if action != 'allow':
            return action, mult, reason

    # Check CPI (all assets)
    cpi_cfg = cfg.get('cpi', {})
    if cpi_cfg.get('enabled', True):
        action, mult, reason = _check_macro_event_gate(
            now, _get_cpi_dates(), 'CPI', cpi_cfg)
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
            if isinstance(cal, dict):
                earnings_raw = cal.get('Earnings Date')
                if earnings_raw:
                    if isinstance(earnings_raw, list) and len(earnings_raw) > 0:
                        earnings_dt = _parse_earnings_date(earnings_raw[0])
                    else:
                        earnings_dt = _parse_earnings_date(earnings_raw)
            elif hasattr(cal, 'iloc'):
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
    if hasattr(raw, 'to_pydatetime'):
        dt = raw.to_pydatetime()
        if dt.tzinfo is None:
            return dt.replace(tzinfo=timezone.utc)
        return dt
    try:
        dt = datetime.fromisoformat(str(raw))
        if dt.tzinfo is None:
            return dt.replace(tzinfo=timezone.utc)
        return dt
    except (ValueError, TypeError):
        return None


def get_event_warnings_for_positions(open_positions: list,
                                     lookahead_hours: int = 72) -> list:
    """Returns warnings for open positions with upcoming events."""
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

        last_warned = _warning_cooldown.get(symbol, 0)
        if time.time() - last_warned < _WARNING_COOLDOWN_SECONDS:
            continue

        entry_price = pos.get('entry_price', 0)

        if asset_type == 'stock':
            earnings_dt = _get_earnings_date(symbol)
            if earnings_dt:
                hours_until = (earnings_dt - now).total_seconds() / 3600
                if 0 < hours_until <= lookahead:
                    warnings.append({
                        'symbol': symbol, 'event_type': 'Earnings',
                        'event_date': earnings_dt, 'hours_until': hours_until,
                        'asset_type': asset_type, 'current_price': entry_price,
                    })
                    _warning_cooldown[symbol] = time.time()

        next_fomc = _get_next_event(now, _get_fomc_dates())
        if next_fomc:
            hours_until = (next_fomc - now).total_seconds() / 3600
            if 0 < hours_until <= lookahead:
                warnings.append({
                    'symbol': symbol, 'event_type': 'FOMC',
                    'event_date': next_fomc, 'hours_until': hours_until,
                    'asset_type': asset_type, 'current_price': entry_price,
                })

        next_cpi = _get_next_event(now, _get_cpi_dates())
        if next_cpi:
            hours_until = (next_cpi - now).total_seconds() / 3600
            if 0 < hours_until <= lookahead:
                warnings.append({
                    'symbol': symbol, 'event_type': 'CPI',
                    'event_date': next_cpi, 'hours_until': hours_until,
                    'asset_type': asset_type, 'current_price': entry_price,
                })

    return warnings


def get_upcoming_macro_events(days_ahead: int = 30) -> list:
    """Returns upcoming FOMC and CPI events for the /events command."""
    now = datetime.now(timezone.utc)
    cutoff = now + timedelta(days=days_ahead)
    events = []

    for dt in _get_fomc_dates():
        if now < dt <= cutoff:
            events.append({
                'event_type': 'FOMC', 'event_date': dt,
                'hours_until': (dt - now).total_seconds() / 3600,
            })

    for dt in _get_cpi_dates():
        if now < dt <= cutoff:
            events.append({
                'event_type': 'CPI', 'event_date': dt,
                'hours_until': (dt - now).total_seconds() / 3600,
            })

    all_dates = _get_fomc_dates() + _get_cpi_dates()
    if all_dates and now > max(all_dates):
        log.warning("Event calendar: All loaded dates have expired. "
                     "Update config/event_dates.yaml for the new year.")

    events.sort(key=lambda e: e['event_date'])
    return events


def clear_event_cache():
    """Clears earnings, warning, and event date caches. For tests."""
    _earnings_cache.clear()
    _warning_cooldown.clear()
    reload_event_dates()
