"""Tests for proactive market event alerts (src/analysis/market_alerts.py)."""

import time
from datetime import datetime, timedelta, timezone
from unittest.mock import patch, MagicMock

import pytest

from src.analysis.market_alerts import (
    MarketAlertCooldown,
    check_scheduled_event_alerts,
    generate_daily_digest,
    check_breaking_market_news,
    check_sector_moves,
    run_market_alerts,
    _cooldown,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def reset_cooldown():
    """Clear module-level cooldown between tests."""
    _cooldown.clear()
    yield
    _cooldown.clear()


def _make_assessment(direction='bullish', confidence=0.8,
                     catalyst_type='regulatory', catalyst_freshness='breaking',
                     key_headline='Test headline'):
    return {
        'direction': direction,
        'confidence': confidence,
        'catalyst_type': catalyst_type,
        'catalyst_freshness': catalyst_freshness,
        'reasoning': 'test reasoning',
        'key_headline': key_headline,
        'sentiment_divergence': False,
    }


def _make_gemini(symbols_assessments, cross_asset_theme=None):
    return {
        'symbol_assessments': symbols_assessments,
        'market_mood': 'test mood',
        'cross_asset_theme': cross_asset_theme,
    }


# ---------------------------------------------------------------------------
# Cooldown Tests
# ---------------------------------------------------------------------------

class TestMarketAlertCooldown:
    def test_first_alert_passes(self):
        cd = MarketAlertCooldown()
        assert cd.is_cooled_down('event:FOMC:2026-03-18', 12) is True

    def test_repeat_suppressed(self):
        cd = MarketAlertCooldown()
        cd.mark_sent('event:FOMC:2026-03-18')
        assert cd.is_cooled_down('event:FOMC:2026-03-18', 12) is False

    def test_expires_after_ttl(self):
        cd = MarketAlertCooldown()
        cd._sent['event:FOMC:2026-03-18'] = time.time() - 13 * 3600  # 13h ago
        assert cd.is_cooled_down('event:FOMC:2026-03-18', 12) is True

    def test_different_keys_independent(self):
        cd = MarketAlertCooldown()
        cd.mark_sent('event:FOMC:2026-03-18')
        assert cd.is_cooled_down('event:CPI:2026-03-11', 12) is True

    def test_auto_prune_old_entries(self):
        cd = MarketAlertCooldown()
        cd._sent['old:key'] = time.time() - 90000  # >24h ago
        cd._sent['fresh:key'] = time.time()
        cd.is_cooled_down('test', 1)  # triggers prune
        assert 'old:key' not in cd._sent
        assert 'fresh:key' in cd._sent

    def test_default_cooldown_from_prefix(self):
        cd = MarketAlertCooldown()
        cd.mark_sent('breaking:regulatory:BTC')
        # Default for 'breaking' is 4h, so should not be cooled down
        assert cd.is_cooled_down('breaking:regulatory:BTC') is False


# ---------------------------------------------------------------------------
# Tier 1: Scheduled Event Alerts
# ---------------------------------------------------------------------------

class TestScheduledEventAlerts:
    @patch('src.analysis.market_alerts._get_alerts_config')
    @patch('src.analysis.market_alerts._get_next_event')
    def test_urgency_within_24h_triggers(self, mock_next_event, mock_cfg):
        mock_cfg.return_value = {
            'enabled': True,
            'event_urgency': {'enabled': True, 'urgency_hours_before': 24, 'cooldown_hours': 12},
        }
        now = datetime.now(timezone.utc)
        # FOMC in 12h
        mock_next_event.return_value = now + timedelta(hours=12)

        alerts = check_scheduled_event_alerts()
        # Should get at least one alert (FOMC or CPI depending on mock behavior)
        assert any(a['type'] == 'event_urgency' for a in alerts)

    @patch('src.analysis.market_alerts._get_alerts_config')
    @patch('src.analysis.market_alerts._get_next_event')
    def test_beyond_24h_does_not_trigger(self, mock_next_event, mock_cfg):
        mock_cfg.return_value = {
            'enabled': True,
            'event_urgency': {'enabled': True, 'urgency_hours_before': 24, 'cooldown_hours': 12},
        }
        mock_next_event.return_value = datetime.now(timezone.utc) + timedelta(hours=48)

        alerts = check_scheduled_event_alerts()
        assert len(alerts) == 0

    @patch('src.analysis.market_alerts._get_alerts_config')
    @patch('src.analysis.market_alerts._get_next_event')
    def test_disabled_returns_empty(self, mock_next_event, mock_cfg):
        mock_cfg.return_value = {'enabled': False}
        alerts = check_scheduled_event_alerts()
        assert alerts == []

    @patch('src.analysis.market_alerts._get_alerts_config')
    @patch('src.analysis.market_alerts._get_earnings_date')
    @patch('src.analysis.market_alerts._get_next_event')
    def test_earnings_included_for_stocks(self, mock_next_event, mock_earnings, mock_cfg):
        mock_cfg.return_value = {
            'enabled': True,
            'event_urgency': {'enabled': True, 'urgency_hours_before': 24, 'cooldown_hours': 12},
        }
        mock_next_event.return_value = None  # no macro events
        now = datetime.now(timezone.utc)
        mock_earnings.return_value = now + timedelta(hours=10)

        alerts = check_scheduled_event_alerts(stock_watchlist=['AAPL'])
        assert len(alerts) == 1
        assert 'Earnings (AAPL)' in alerts[0]['event_type']


class TestDailyDigest:
    @patch('src.analysis.market_alerts._get_alerts_config')
    @patch('src.analysis.market_alerts.get_upcoming_macro_events')
    def test_digest_includes_72h_events(self, mock_events, mock_cfg):
        mock_cfg.return_value = {
            'enabled': True,
            'daily_digest_lookahead_hours': 72,
        }
        now = datetime.now(timezone.utc)
        mock_events.return_value = [
            {'event_type': 'FOMC', 'event_date': now + timedelta(hours=48),
             'hours_until': 48},
            {'event_type': 'CPI', 'event_date': now + timedelta(hours=100),
             'hours_until': 100},  # beyond 72h
        ]

        digest = generate_daily_digest()
        assert digest is not None
        assert digest['type'] == 'daily_digest'
        # Only the FOMC within 72h
        assert len(digest['events']) == 1
        assert digest['events'][0]['event_type'] == 'FOMC'

    @patch('src.analysis.market_alerts._get_alerts_config')
    @patch('src.analysis.market_alerts.get_upcoming_macro_events')
    def test_digest_none_when_no_events(self, mock_events, mock_cfg):
        mock_cfg.return_value = {'enabled': True, 'daily_digest_lookahead_hours': 72}
        mock_events.return_value = []
        assert generate_daily_digest() is None

    @patch('src.analysis.market_alerts._get_alerts_config')
    @patch('src.analysis.market_alerts.get_upcoming_macro_events')
    def test_digest_cooldown_prevents_duplicate(self, mock_events, mock_cfg):
        mock_cfg.return_value = {'enabled': True, 'daily_digest_lookahead_hours': 72}
        now = datetime.now(timezone.utc)
        mock_events.return_value = [
            {'event_type': 'FOMC', 'event_date': now + timedelta(hours=48), 'hours_until': 48},
        ]
        # First call succeeds
        assert generate_daily_digest() is not None
        # Second call cooled down
        assert generate_daily_digest() is None


# ---------------------------------------------------------------------------
# Tier 2: Breaking Market News
# ---------------------------------------------------------------------------

class TestBreakingMarketNews:
    @patch('src.analysis.market_alerts._get_alerts_config')
    def test_breaking_regulatory_high_confidence_triggers(self, mock_cfg):
        mock_cfg.return_value = {
            'enabled': True,
            'breaking_news': {
                'enabled': True, 'min_confidence': 0.7,
                'catalyst_types': ['regulatory', 'hack_exploit', 'macro', 'etf'],
                'cooldown_hours': 4,
            },
        }
        gemini = _make_gemini({
            'BTC': _make_assessment('bearish', 0.9, 'regulatory', 'breaking'),
        })
        alerts = check_breaking_market_news(gemini)
        assert len(alerts) == 1
        assert alerts[0]['type'] == 'breaking'
        assert alerts[0]['symbols'] == ['BTC']

    @patch('src.analysis.market_alerts._get_alerts_config')
    def test_stale_catalyst_suppressed(self, mock_cfg):
        mock_cfg.return_value = {
            'enabled': True,
            'breaking_news': {
                'enabled': True, 'min_confidence': 0.7,
                'catalyst_types': ['regulatory'], 'cooldown_hours': 4,
            },
        }
        gemini = _make_gemini({
            'BTC': _make_assessment('bearish', 0.9, 'regulatory', 'stale'),
        })
        alerts = check_breaking_market_news(gemini)
        assert len(alerts) == 0

    @patch('src.analysis.market_alerts._get_alerts_config')
    def test_low_confidence_suppressed(self, mock_cfg):
        mock_cfg.return_value = {
            'enabled': True,
            'breaking_news': {
                'enabled': True, 'min_confidence': 0.7,
                'catalyst_types': ['regulatory'], 'cooldown_hours': 4,
            },
        }
        gemini = _make_gemini({
            'BTC': _make_assessment('bearish', 0.5, 'regulatory', 'breaking'),
        })
        alerts = check_breaking_market_news(gemini)
        assert len(alerts) == 0

    @patch('src.analysis.market_alerts._get_alerts_config')
    def test_noise_catalyst_type_suppressed(self, mock_cfg):
        mock_cfg.return_value = {
            'enabled': True,
            'breaking_news': {
                'enabled': True, 'min_confidence': 0.7,
                'catalyst_types': ['regulatory', 'hack_exploit', 'macro', 'etf'],
                'cooldown_hours': 4,
            },
        }
        gemini = _make_gemini({
            'BTC': _make_assessment('bullish', 0.9, 'partnership', 'breaking'),
        })
        alerts = check_breaking_market_news(gemini)
        assert len(alerts) == 0

    @patch('src.analysis.market_alerts._get_alerts_config')
    def test_market_wide_from_cross_asset_theme(self, mock_cfg):
        mock_cfg.return_value = {
            'enabled': True,
            'breaking_news': {
                'enabled': True, 'min_confidence': 0.7,
                'catalyst_types': ['macro'], 'cooldown_hours': 4,
            },
        }
        gemini = _make_gemini(
            {'BTC': _make_assessment('bearish', 0.85, 'macro', 'breaking')},
            cross_asset_theme='broad risk-off on Fed hawkishness',
        )
        alerts = check_breaking_market_news(gemini)
        assert len(alerts) == 1
        assert alerts[0]['market_wide'] is True

    @patch('src.analysis.market_alerts._get_alerts_config')
    def test_market_wide_from_3plus_symbols(self, mock_cfg):
        mock_cfg.return_value = {
            'enabled': True,
            'breaking_news': {
                'enabled': True, 'min_confidence': 0.7,
                'catalyst_types': ['regulatory'], 'cooldown_hours': 4,
            },
        }
        gemini = _make_gemini({
            'BTC': _make_assessment('bearish', 0.9, 'regulatory', 'breaking'),
            'ETH': _make_assessment('bearish', 0.85, 'regulatory', 'breaking'),
            'SOL': _make_assessment('bearish', 0.8, 'regulatory', 'breaking'),
        })
        alerts = check_breaking_market_news(gemini)
        assert len(alerts) == 1
        assert alerts[0]['market_wide'] is True
        assert len(alerts[0]['symbols']) == 3

    @patch('src.analysis.market_alerts._get_alerts_config')
    def test_cooldown_prevents_duplicate_breaking(self, mock_cfg):
        mock_cfg.return_value = {
            'enabled': True,
            'breaking_news': {
                'enabled': True, 'min_confidence': 0.7,
                'catalyst_types': ['regulatory'], 'cooldown_hours': 4,
            },
        }
        gemini = _make_gemini({
            'BTC': _make_assessment('bearish', 0.9, 'regulatory', 'breaking'),
        })
        alerts1 = check_breaking_market_news(gemini)
        assert len(alerts1) == 1
        alerts2 = check_breaking_market_news(gemini)
        assert len(alerts2) == 0

    @patch('src.analysis.market_alerts._get_alerts_config')
    def test_none_gemini_handled(self, mock_cfg):
        mock_cfg.return_value = {'enabled': True, 'breaking_news': {'enabled': True}}
        assert check_breaking_market_news(None) == []


# ---------------------------------------------------------------------------
# Tier 3: Sector Moves
# ---------------------------------------------------------------------------

class TestSectorMoves:
    def _mock_sector_config(self):
        """Set up sector config with a test group."""
        import src.analysis.market_alerts as ma
        import src.analysis.sector_limits as sl
        sl._sector_config = {
            'default_max_positions_per_group': 2,
            'groups': {
                'test_group': {
                    'max_positions': 3,
                    'symbols': ['BTC', 'ETH', 'SOL', 'BNB'],
                },
                'other_group': {
                    'max_positions': 2,
                    'symbols': ['AAPL', 'MSFT', 'GOOGL', 'AMZN'],
                },
            },
        }

    @patch('src.analysis.market_alerts._get_alerts_config')
    def test_3_bearish_symbols_triggers(self, mock_cfg):
        mock_cfg.return_value = {
            'enabled': True,
            'sector_moves': {
                'enabled': True, 'min_symbols_for_alert': 3,
                'min_avg_confidence': 0.5, 'cooldown_hours': 6,
            },
        }
        self._mock_sector_config()
        gemini = _make_gemini({
            'BTC': _make_assessment('bearish', 0.7, 'macro', 'recent'),
            'ETH': _make_assessment('bearish', 0.6, 'macro', 'recent'),
            'SOL': _make_assessment('bearish', 0.65, 'macro', 'recent'),
        })
        alerts = check_sector_moves(gemini)
        assert len(alerts) == 1
        assert alerts[0]['type'] == 'sector_move'
        assert alerts[0]['group'] == 'test_group'
        assert alerts[0]['direction'] == 'bearish'

    @patch('src.analysis.market_alerts._get_alerts_config')
    def test_2_symbols_insufficient(self, mock_cfg):
        mock_cfg.return_value = {
            'enabled': True,
            'sector_moves': {
                'enabled': True, 'min_symbols_for_alert': 3,
                'min_avg_confidence': 0.5, 'cooldown_hours': 6,
            },
        }
        self._mock_sector_config()
        gemini = _make_gemini({
            'BTC': _make_assessment('bearish', 0.7),
            'ETH': _make_assessment('bearish', 0.6),
        })
        alerts = check_sector_moves(gemini)
        assert len(alerts) == 0

    @patch('src.analysis.market_alerts._get_alerts_config')
    def test_mixed_directions_no_trigger(self, mock_cfg):
        mock_cfg.return_value = {
            'enabled': True,
            'sector_moves': {
                'enabled': True, 'min_symbols_for_alert': 3,
                'min_avg_confidence': 0.5, 'cooldown_hours': 6,
            },
        }
        self._mock_sector_config()
        gemini = _make_gemini({
            'BTC': _make_assessment('bearish', 0.7),
            'ETH': _make_assessment('bullish', 0.6),
            'SOL': _make_assessment('bearish', 0.65),
        })
        alerts = check_sector_moves(gemini)
        assert len(alerts) == 0

    @patch('src.analysis.market_alerts._get_alerts_config')
    def test_different_sectors_independent(self, mock_cfg):
        mock_cfg.return_value = {
            'enabled': True,
            'sector_moves': {
                'enabled': True, 'min_symbols_for_alert': 3,
                'min_avg_confidence': 0.5, 'cooldown_hours': 6,
            },
        }
        self._mock_sector_config()
        gemini = _make_gemini({
            'BTC': _make_assessment('bearish', 0.7),
            'ETH': _make_assessment('bearish', 0.6),
            'SOL': _make_assessment('bearish', 0.65),
            'AAPL': _make_assessment('bullish', 0.8),
            'MSFT': _make_assessment('bullish', 0.7),
            'GOOGL': _make_assessment('bullish', 0.75),
        })
        alerts = check_sector_moves(gemini)
        assert len(alerts) == 2
        types = {a['group'] for a in alerts}
        assert 'test_group' in types
        assert 'other_group' in types

    @patch('src.analysis.market_alerts._get_alerts_config')
    def test_velocity_support_detected(self, mock_cfg):
        mock_cfg.return_value = {
            'enabled': True,
            'sector_moves': {
                'enabled': True, 'min_symbols_for_alert': 3,
                'min_avg_confidence': 0.5, 'cooldown_hours': 6,
            },
        }
        self._mock_sector_config()
        gemini = _make_gemini({
            'BTC': _make_assessment('bearish', 0.7),
            'ETH': _make_assessment('bearish', 0.6),
            'SOL': _make_assessment('bearish', 0.65),
        })
        velocity_cache = {
            'BTC': {'sentiment_trend': 'deteriorating'},
            'ETH': {'sentiment_trend': 'deteriorating'},
            'SOL': {'sentiment_trend': 'stable'},
        }
        alerts = check_sector_moves(gemini, news_velocity_cache=velocity_cache)
        assert len(alerts) == 1
        assert alerts[0]['velocity_support'] is True

    @patch('src.analysis.market_alerts._get_alerts_config')
    def test_none_gemini_handled(self, mock_cfg):
        mock_cfg.return_value = {'enabled': True, 'sector_moves': {'enabled': True}}
        assert check_sector_moves(None) == []


# ---------------------------------------------------------------------------
# Integration: run_market_alerts
# ---------------------------------------------------------------------------

class TestRunMarketAlerts:
    @patch('src.analysis.market_alerts._get_alerts_config')
    @patch('src.analysis.market_alerts.check_scheduled_event_alerts')
    @patch('src.analysis.market_alerts.check_breaking_market_news')
    @patch('src.analysis.market_alerts.check_sector_moves')
    def test_all_tiers_combined(self, mock_sector, mock_breaking, mock_events, mock_cfg):
        mock_cfg.return_value = {'enabled': True}
        mock_events.return_value = [{'type': 'event_urgency', 'event_type': 'FOMC'}]
        mock_breaking.return_value = [{'type': 'breaking', 'symbols': ['BTC']}]
        mock_sector.return_value = [{'type': 'sector_move', 'group': 'tech_mega'}]

        alerts = run_market_alerts()
        assert len(alerts) == 3
        types = {a['type'] for a in alerts}
        assert types == {'event_urgency', 'breaking', 'sector_move'}

    @patch('src.analysis.market_alerts._get_alerts_config')
    @patch('src.analysis.market_alerts.check_scheduled_event_alerts')
    @patch('src.analysis.market_alerts.check_breaking_market_news')
    @patch('src.analysis.market_alerts.check_sector_moves')
    def test_quiet_market_empty_list(self, mock_sector, mock_breaking, mock_events, mock_cfg):
        mock_cfg.return_value = {'enabled': True}
        mock_events.return_value = []
        mock_breaking.return_value = []
        mock_sector.return_value = []

        alerts = run_market_alerts()
        assert alerts == []

    @patch('src.analysis.market_alerts._get_alerts_config')
    def test_disabled_returns_empty(self, mock_cfg):
        mock_cfg.return_value = {'enabled': False}
        alerts = run_market_alerts(gemini_assessments={'symbol_assessments': {}})
        assert alerts == []

    @patch('src.analysis.market_alerts._get_alerts_config')
    @patch('src.analysis.market_alerts.check_scheduled_event_alerts')
    @patch('src.analysis.market_alerts.check_breaking_market_news')
    @patch('src.analysis.market_alerts.check_sector_moves')
    def test_tier_failure_doesnt_block_others(self, mock_sector, mock_breaking, mock_events, mock_cfg):
        mock_cfg.return_value = {'enabled': True}
        mock_events.side_effect = Exception("DB error")
        mock_breaking.return_value = [{'type': 'breaking', 'symbols': ['BTC']}]
        mock_sector.return_value = []

        alerts = run_market_alerts()
        assert len(alerts) == 1
        assert alerts[0]['type'] == 'breaking'
