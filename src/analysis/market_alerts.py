"""Proactive Market Event Alerts — pushes alerts for major events regardless of positions.

Three tiers:
  1. Scheduled event urgency (FOMC/CPI/earnings entering 24h window) + daily digest
  2. Breaking market news (high-confidence Gemini catalyst detections)
  3. Sector-wide coordinated moves (3+ symbols aligned in same direction)

Cost: $0 additional — reuses existing Gemini assessments and DB queries.
"""

import time
from datetime import datetime, timezone

from src.config import app_config
from src.logger import log
from src.analysis.event_calendar import (
    get_upcoming_macro_events, _get_earnings_date, _get_next_event,
    FOMC_DATES_2026, CPI_DATES_2026,
)
import src.analysis.sector_limits as sector_limits_mod


# ---------------------------------------------------------------------------
# Cooldown Manager
# ---------------------------------------------------------------------------

class MarketAlertCooldown:
    """In-memory dedup: maps key → last-sent timestamp. Auto-prunes >24h entries."""

    # Default cooldowns per alert type (hours)
    DEFAULT_COOLDOWNS = {
        'event': 12,
        'digest': 23,
        'breaking': 4,
        'sector': 6,
    }

    def __init__(self):
        self._sent: dict[str, float] = {}

    def _prune(self):
        cutoff = time.time() - 86400  # 24h
        self._sent = {k: v for k, v in self._sent.items() if v > cutoff}

    def is_cooled_down(self, key: str, cooldown_hours: float | None = None) -> bool:
        """Returns True if enough time has passed (alert may fire)."""
        self._prune()
        last = self._sent.get(key)
        if last is None:
            return True
        if cooldown_hours is None:
            prefix = key.split(':')[0]
            cooldown_hours = self.DEFAULT_COOLDOWNS.get(prefix, 6)
        return (time.time() - last) >= cooldown_hours * 3600

    def mark_sent(self, key: str):
        self._sent[key] = time.time()

    def clear(self):
        self._sent.clear()


# Module-level cooldown instance
_cooldown = MarketAlertCooldown()


def get_cooldown() -> MarketAlertCooldown:
    return _cooldown


# ---------------------------------------------------------------------------
# Config helper
# ---------------------------------------------------------------------------

def _get_alerts_config() -> dict:
    return app_config.get('settings', {}).get('market_alerts', {})


# ---------------------------------------------------------------------------
# Tier 1: Scheduled Event Alerts
# ---------------------------------------------------------------------------

def check_scheduled_event_alerts(stock_watchlist: list | None = None) -> list:
    """Check for macro events entering the urgency window (default 24h).

    Returns list of alert dicts with type='event_urgency'.
    """
    cfg = _get_alerts_config()
    if not cfg.get('enabled', True):
        return []
    urgency_cfg = cfg.get('event_urgency', {})
    if not urgency_cfg.get('enabled', True):
        return []

    urgency_hours = urgency_cfg.get('urgency_hours_before', 24)
    cooldown_hours = urgency_cfg.get('cooldown_hours', 12)
    now = datetime.now(timezone.utc)
    alerts = []

    # FOMC + CPI
    for event_dates, event_type in [(FOMC_DATES_2026, 'FOMC'), (CPI_DATES_2026, 'CPI')]:
        next_event = _get_next_event(now, event_dates)
        if next_event is None:
            continue
        hours_until = (next_event - now).total_seconds() / 3600
        if 0 < hours_until <= urgency_hours:
            key = f"event:{event_type}:{next_event.date().isoformat()}"
            if _cooldown.is_cooled_down(key, cooldown_hours):
                alerts.append({
                    'type': 'event_urgency',
                    'event_type': event_type,
                    'event_date': next_event,
                    'hours_until': hours_until,
                })
                _cooldown.mark_sent(key)

    # Earnings for stock watchlist
    if stock_watchlist:
        for symbol in stock_watchlist:
            try:
                earnings_dt = _get_earnings_date(symbol)
                if earnings_dt is None:
                    continue
                hours_until = (earnings_dt - now).total_seconds() / 3600
                if 0 < hours_until <= urgency_hours:
                    key = f"event:Earnings:{symbol}:{earnings_dt.date().isoformat()}"
                    if _cooldown.is_cooled_down(key, cooldown_hours):
                        alerts.append({
                            'type': 'event_urgency',
                            'event_type': f'Earnings ({symbol})',
                            'event_date': earnings_dt,
                            'hours_until': hours_until,
                        })
                        _cooldown.mark_sent(key)
            except Exception as e:
                log.debug(f"Earnings check failed for {symbol}: {e}")

    return alerts


def generate_daily_digest(lookahead_hours: int | None = None) -> dict | None:
    """Generate a daily digest of upcoming events within the lookahead window.

    Returns an alert dict with type='daily_digest', or None if cooled down / no events.
    """
    cfg = _get_alerts_config()
    if not cfg.get('enabled', True):
        return None

    if lookahead_hours is None:
        lookahead_hours = cfg.get('daily_digest_lookahead_hours', 72)

    now = datetime.now(timezone.utc)
    key = f"digest:{now.date().isoformat()}"
    if not _cooldown.is_cooled_down(key, 23):
        return None

    # Macro events
    days_ahead = max(lookahead_hours // 24 + 1, 3)
    events = get_upcoming_macro_events(days_ahead=days_ahead)
    # Filter to lookahead window
    events = [e for e in events if e['hours_until'] <= lookahead_hours]

    if not events:
        return None

    _cooldown.mark_sent(key)
    return {
        'type': 'daily_digest',
        'events': events,
        'lookahead_hours': lookahead_hours,
    }


# ---------------------------------------------------------------------------
# Tier 2: Breaking Market News
# ---------------------------------------------------------------------------

_BREAKING_CATALYST_TYPES = {'regulatory', 'hack_exploit', 'macro', 'etf'}


def check_breaking_market_news(gemini_assessments: dict | None,
                               news_per_symbol: dict | None = None) -> list:
    """Detect high-impact breaking catalysts from existing Gemini assessments.

    No extra Gemini calls — reads symbol_assessments already produced in cycle.

    Returns list of alert dicts with type='breaking'.
    """
    cfg = _get_alerts_config()
    if not cfg.get('enabled', True):
        return []
    breaking_cfg = cfg.get('breaking_news', {})
    if not breaking_cfg.get('enabled', True):
        return []

    if not gemini_assessments:
        return []

    symbol_assessments = gemini_assessments.get('symbol_assessments', {})
    if not symbol_assessments:
        return []

    min_confidence = breaking_cfg.get('min_confidence', 0.7)
    catalyst_types = set(breaking_cfg.get('catalyst_types',
                                          list(_BREAKING_CATALYST_TYPES)))
    cooldown_hours = breaking_cfg.get('cooldown_hours', 4)

    cross_asset_theme = gemini_assessments.get('cross_asset_theme')

    # Collect qualifying symbols
    breaking_symbols = []
    for symbol, assessment in symbol_assessments.items():
        if (assessment.get('catalyst_freshness') == 'breaking'
                and assessment.get('confidence', 0) >= min_confidence
                and assessment.get('catalyst_type') in catalyst_types):
            breaking_symbols.append((symbol, assessment))

    if not breaking_symbols:
        return []

    alerts = []

    # Check for market-wide alert
    is_market_wide = bool(cross_asset_theme) or len(breaking_symbols) >= 3

    if is_market_wide:
        # Single consolidated market-wide alert
        symbols_sorted = sorted(s for s, _ in breaking_symbols)
        catalyst = breaking_symbols[0][1].get('catalyst_type', 'unknown')
        key = f"breaking:{catalyst}:{','.join(symbols_sorted)}"
        if _cooldown.is_cooled_down(key, cooldown_hours):
            alerts.append({
                'type': 'breaking',
                'market_wide': True,
                'symbols': symbols_sorted,
                'catalyst_type': catalyst,
                'cross_asset_theme': cross_asset_theme,
                'assessments': {s: a for s, a in breaking_symbols},
            })
            _cooldown.mark_sent(key)
    else:
        # Per-symbol alerts
        for symbol, assessment in breaking_symbols:
            catalyst = assessment.get('catalyst_type', 'unknown')
            key = f"breaking:{catalyst}:{symbol}"
            if _cooldown.is_cooled_down(key, cooldown_hours):
                alerts.append({
                    'type': 'breaking',
                    'market_wide': False,
                    'symbols': [symbol],
                    'catalyst_type': catalyst,
                    'cross_asset_theme': None,
                    'assessments': {symbol: assessment},
                })
                _cooldown.mark_sent(key)

    return alerts


# ---------------------------------------------------------------------------
# Tier 3: Sector-Wide Coordinated Moves
# ---------------------------------------------------------------------------

def check_sector_moves(gemini_assessments: dict | None,
                       news_per_symbol: dict | None = None,
                       news_velocity_cache: dict | None = None) -> list:
    """Detect when 3+ symbols in the same sector group share direction.

    Uses existing Gemini assessments (zero extra cost).

    Returns list of alert dicts with type='sector_move'.
    """
    cfg = _get_alerts_config()
    if not cfg.get('enabled', True):
        return []
    sector_cfg = cfg.get('sector_moves', {})
    if not sector_cfg.get('enabled', True):
        return []

    if not gemini_assessments:
        return []

    symbol_assessments = gemini_assessments.get('symbol_assessments', {})
    if not symbol_assessments:
        return []

    min_symbols = sector_cfg.get('min_symbols_for_alert', 3)
    min_avg_confidence = sector_cfg.get('min_avg_confidence', 0.5)
    cooldown_hours = sector_cfg.get('cooldown_hours', 6)

    sector_limits_mod._ensure_loaded()
    if not sector_limits_mod._sector_config:
        return []

    groups = sector_limits_mod._sector_config.get('groups', {})
    alerts = []

    for group_name, group_data in groups.items():
        group_symbols = [str(s).upper() for s in group_data.get('symbols', [])]

        # Collect assessments for symbols in this group
        directional = {}  # direction → [(symbol, confidence)]
        for sym in group_symbols:
            assessment = symbol_assessments.get(sym)
            if not assessment:
                continue
            direction = assessment.get('direction')
            confidence = assessment.get('confidence', 0)
            if direction in ('bullish', 'bearish') and confidence >= min_avg_confidence:
                directional.setdefault(direction, []).append((sym, confidence))

        # Check if any direction has enough aligned symbols
        for direction, syms_confs in directional.items():
            if len(syms_confs) >= min_symbols:
                avg_conf = sum(c for _, c in syms_confs) / len(syms_confs)
                if avg_conf >= min_avg_confidence:
                    key = f"sector:{group_name}:{direction}"
                    if _cooldown.is_cooled_down(key, cooldown_hours):
                        # Reinforce with news velocity if available
                        velocity_support = False
                        if news_velocity_cache:
                            aligned_velocity = 0
                            for sym, _ in syms_confs:
                                vel = news_velocity_cache.get(sym, {})
                                trend = vel.get('sentiment_trend', 'stable')
                                if (direction == 'bullish' and trend == 'improving') or \
                                   (direction == 'bearish' and trend == 'deteriorating'):
                                    aligned_velocity += 1
                            velocity_support = aligned_velocity >= 2

                        alerts.append({
                            'type': 'sector_move',
                            'group': group_name,
                            'direction': direction,
                            'symbols': [s for s, _ in syms_confs],
                            'avg_confidence': round(avg_conf, 2),
                            'velocity_support': velocity_support,
                        })
                        _cooldown.mark_sent(key)

    return alerts


# ---------------------------------------------------------------------------
# Top-Level Orchestrator
# ---------------------------------------------------------------------------

def run_market_alerts(gemini_assessments: dict | None = None,
                      news_per_symbol: dict | None = None,
                      news_velocity_cache: dict | None = None,
                      stock_watchlist: list | None = None) -> list:
    """Run all alert tiers. Called from run_bot_cycle().

    Returns combined list of alert dicts ready for send_market_event_alert().
    """
    cfg = _get_alerts_config()
    if not cfg.get('enabled', True):
        return []

    alerts = []

    # Tier 1: scheduled events
    try:
        alerts.extend(check_scheduled_event_alerts(stock_watchlist=stock_watchlist))
    except Exception as e:
        log.warning(f"Market alerts tier 1 (events) failed: {e}")

    # Tier 2: breaking news
    try:
        alerts.extend(check_breaking_market_news(gemini_assessments, news_per_symbol))
    except Exception as e:
        log.warning(f"Market alerts tier 2 (breaking) failed: {e}")

    # Tier 3: sector moves
    try:
        alerts.extend(check_sector_moves(gemini_assessments, news_per_symbol,
                                         news_velocity_cache))
    except Exception as e:
        log.warning(f"Market alerts tier 3 (sector) failed: {e}")

    if alerts:
        log.info(f"Market alerts: {len(alerts)} alerts generated "
                 f"({', '.join(a['type'] for a in alerts)})")

    return alerts
