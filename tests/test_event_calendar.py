"""Tests for the event calendar integration."""

import time
from datetime import datetime, timezone, timedelta
from unittest.mock import patch, MagicMock

import pytest

from src.analysis.event_calendar import (
    check_event_gate,
    get_event_warnings_for_positions,
    get_upcoming_macro_events,
    clear_event_cache,
    _get_next_event,
    _check_earnings_gate,
    _check_macro_event_gate,
    _check_crypto_event_gate,
    _parse_earnings_date,
    FOMC_DATES_2026,
    CPI_DATES_2026,
)


@pytest.fixture(autouse=True)
def clear_caches():
    clear_event_cache()
    yield
    clear_event_cache()


class TestGetNextEvent:
    def test_returns_next_future_event(self):
        events = [
            datetime(2026, 1, 1, tzinfo=timezone.utc),
            datetime(2026, 6, 1, tzinfo=timezone.utc),
            datetime(2026, 12, 1, tzinfo=timezone.utc),
        ]
        now = datetime(2026, 3, 1, tzinfo=timezone.utc)
        result = _get_next_event(now, events)
        assert result == datetime(2026, 6, 1, tzinfo=timezone.utc)

    def test_returns_none_if_all_passed(self):
        events = [
            datetime(2025, 1, 1, tzinfo=timezone.utc),
            datetime(2025, 6, 1, tzinfo=timezone.utc),
        ]
        now = datetime(2026, 1, 1, tzinfo=timezone.utc)
        assert _get_next_event(now, events) is None

    def test_returns_first_future(self):
        events = [
            datetime(2026, 1, 1, tzinfo=timezone.utc),
            datetime(2026, 1, 2, tzinfo=timezone.utc),
        ]
        now = datetime(2025, 12, 31, tzinfo=timezone.utc)
        assert _get_next_event(now, events) == events[0]


class TestCheckMacroEventGate:
    def test_block_within_24h(self):
        now = datetime(2026, 3, 18, 10, 0, tzinfo=timezone.utc)  # 8h before FOMC
        cfg = {'block_hours_before': 24, 'reduce_hours_before': 48,
               'reduce_multiplier': 0.5}
        action, mult, reason = _check_macro_event_gate(
            now, FOMC_DATES_2026, 'FOMC', cfg)
        assert action == 'block'
        assert 'FOMC' in reason

    def test_reduce_within_48h(self):
        now = datetime(2026, 3, 17, 0, 0, tzinfo=timezone.utc)  # ~42h before FOMC
        cfg = {'block_hours_before': 24, 'reduce_hours_before': 48,
               'reduce_multiplier': 0.5}
        action, mult, reason = _check_macro_event_gate(
            now, FOMC_DATES_2026, 'FOMC', cfg)
        assert action == 'reduce'
        assert mult == 0.5

    def test_allow_beyond_48h(self):
        now = datetime(2026, 3, 10, 0, 0, tzinfo=timezone.utc)  # 8+ days before FOMC
        cfg = {'block_hours_before': 24, 'reduce_hours_before': 48,
               'reduce_multiplier': 0.5}
        action, mult, reason = _check_macro_event_gate(
            now, FOMC_DATES_2026, 'FOMC', cfg)
        assert action == 'allow'
        assert mult == 1.0

    def test_cpi_block(self):
        now = datetime(2026, 1, 14, 6, 0, tzinfo=timezone.utc)  # 7.5h before CPI
        cfg = {'block_hours_before': 0, 'reduce_hours_before': 24,
               'reduce_multiplier': 0.5}
        # With block_hours_before=0, even 7.5h before should not block
        action, _, _ = _check_macro_event_gate(now, CPI_DATES_2026, 'CPI', cfg)
        assert action == 'reduce'  # within 24h reduce window

    def test_all_events_passed(self):
        now = datetime(2027, 6, 1, tzinfo=timezone.utc)
        cfg = {'block_hours_before': 24, 'reduce_hours_before': 48,
               'reduce_multiplier': 0.5}
        action, mult, _ = _check_macro_event_gate(
            now, FOMC_DATES_2026, 'FOMC', cfg)
        assert action == 'allow'


class TestCheckEarningsGate:
    def test_block_within_24h(self):
        now = datetime(2026, 4, 15, 12, 0, tzinfo=timezone.utc)
        earnings_dt = datetime(2026, 4, 16, 0, 0, tzinfo=timezone.utc)
        cfg = {'block_hours_before': 24, 'reduce_hours_before': 48,
               'reduce_multiplier': 0.5}

        with patch('src.analysis.event_calendar._get_earnings_date',
                   return_value=earnings_dt):
            action, _, reason = _check_earnings_gate('AAPL', now, cfg)
            assert action == 'block'
            assert 'Earnings' in reason

    def test_reduce_within_48h(self):
        now = datetime(2026, 4, 14, 12, 0, tzinfo=timezone.utc)
        earnings_dt = datetime(2026, 4, 16, 0, 0, tzinfo=timezone.utc)
        cfg = {'block_hours_before': 24, 'reduce_hours_before': 48,
               'reduce_multiplier': 0.5}

        with patch('src.analysis.event_calendar._get_earnings_date',
                   return_value=earnings_dt):
            action, mult, _ = _check_earnings_gate('AAPL', now, cfg)
            assert action == 'reduce'
            assert mult == 0.5

    def test_allow_when_no_earnings(self):
        cfg = {'block_hours_before': 24, 'reduce_hours_before': 48,
               'reduce_multiplier': 0.5}
        now = datetime(2026, 4, 15, tzinfo=timezone.utc)

        with patch('src.analysis.event_calendar._get_earnings_date',
                   return_value=None):
            action, mult, _ = _check_earnings_gate('AAPL', now, cfg)
            assert action == 'allow'
            assert mult == 1.0

    def test_allow_when_earnings_passed(self):
        now = datetime(2026, 4, 20, tzinfo=timezone.utc)
        earnings_dt = datetime(2026, 4, 15, tzinfo=timezone.utc)
        cfg = {'block_hours_before': 24, 'reduce_hours_before': 48,
               'reduce_multiplier': 0.5}

        with patch('src.analysis.event_calendar._get_earnings_date',
                   return_value=earnings_dt):
            action, _, _ = _check_earnings_gate('AAPL', now, cfg)
            assert action == 'allow'


class TestCheckEventGate:
    @patch('src.analysis.event_calendar._get_earnings_date')
    def test_stock_earnings_block(self, mock_earnings):
        """Stock BUY near earnings → block."""
        mock_earnings.return_value = datetime(2026, 4, 16, 0, 0,
                                              tzinfo=timezone.utc)
        with patch('src.analysis.event_calendar.datetime') as mock_dt:
            mock_dt.now.return_value = datetime(2026, 4, 15, 12, 0,
                                                tzinfo=timezone.utc)
            mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
            action, _, reason = check_event_gate('AAPL', 'BUY',
                                                  asset_type='stock')
            assert action == 'block'

    def test_crypto_skips_earnings_check(self):
        """Crypto symbols should never be blocked by earnings."""
        # Even if _get_earnings_date returned something, crypto should skip it
        with patch('src.analysis.event_calendar._get_earnings_date') as mock_ed:
            mock_ed.return_value = datetime(2026, 4, 16, tzinfo=timezone.utc)
            # Use a date far from FOMC/CPI
            with patch('src.analysis.event_calendar.datetime') as mock_dt:
                mock_dt.now.return_value = datetime(2026, 2, 1, 0, 0,
                                                    tzinfo=timezone.utc)
                mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
                action, _, _ = check_event_gate('BTC', 'BUY',
                                                 asset_type='crypto')
                # Should not have called _get_earnings_date for crypto
                mock_ed.assert_not_called()

    @patch('src.analysis.event_calendar.app_config')
    def test_disabled_always_allows(self, mock_config):
        mock_config.get.return_value = {'event_calendar': {'enabled': False}}
        action, mult, _ = check_event_gate('AAPL', 'BUY', asset_type='stock')
        assert action == 'allow'
        assert mult == 1.0


class TestParseEarningsDate:
    def test_datetime_with_tz(self):
        dt = datetime(2026, 4, 15, 12, 0, tzinfo=timezone.utc)
        assert _parse_earnings_date(dt) == dt

    def test_datetime_without_tz(self):
        dt = datetime(2026, 4, 15, 12, 0)
        result = _parse_earnings_date(dt)
        assert result.tzinfo == timezone.utc

    def test_string_iso(self):
        result = _parse_earnings_date('2026-04-15')
        assert result is not None
        assert result.year == 2026

    def test_none_returns_none(self):
        assert _parse_earnings_date(None) is None

    def test_invalid_returns_none(self):
        assert _parse_earnings_date('not-a-date') is None


class TestGetUpcomingMacroEvents:
    def test_returns_sorted_events(self):
        with patch('src.analysis.event_calendar.datetime') as mock_dt:
            mock_dt.now.return_value = datetime(2026, 1, 1, 0, 0,
                                                tzinfo=timezone.utc)
            mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
            events = get_upcoming_macro_events(days_ahead=60)
            assert len(events) > 0
            # Should be sorted by date
            dates = [e['event_date'] for e in events]
            assert dates == sorted(dates)
            # Should include both FOMC and CPI
            types = {e['event_type'] for e in events}
            assert 'FOMC' in types
            assert 'CPI' in types


class TestEventWarnings:
    def test_warns_for_position_near_fomc(self):
        # Place "now" close to a FOMC date
        fomc = FOMC_DATES_2026[0]
        now = fomc - timedelta(hours=48)

        positions = [
            {'symbol': 'BTC', 'status': 'OPEN', 'asset_type': 'crypto'},
        ]

        with patch('src.analysis.event_calendar.datetime') as mock_dt:
            mock_dt.now.return_value = now
            mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
            warnings = get_event_warnings_for_positions(positions,
                                                         lookahead_hours=72)
            fomc_warnings = [w for w in warnings if w['event_type'] == 'FOMC']
            assert len(fomc_warnings) >= 1

    def test_no_warning_for_closed_positions(self):
        positions = [
            {'symbol': 'BTC', 'status': 'CLOSED', 'asset_type': 'crypto'},
        ]
        warnings = get_event_warnings_for_positions(positions)
        assert len(warnings) == 0

    def test_cooldown_prevents_repeated_warnings(self):
        import src.analysis.event_calendar as ec
        ec._warning_cooldown['AAPL'] = time.time()  # just warned

        fomc = FOMC_DATES_2026[0]
        now = fomc - timedelta(hours=48)

        positions = [
            {'symbol': 'AAPL', 'status': 'OPEN', 'asset_type': 'stock'},
        ]

        with patch('src.analysis.event_calendar.datetime') as mock_dt:
            mock_dt.now.return_value = now
            mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
            warnings = get_event_warnings_for_positions(positions)
            aapl_warnings = [w for w in warnings if w['symbol'] == 'AAPL']
            assert len(aapl_warnings) == 0


class TestCryptoEventGate:
    """Tests for crypto-specific event gating."""

    def test_block_within_crypto_event(self):
        """Crypto event within block window → block."""
        now = datetime(2026, 9, 14, 18, 0, tzinfo=timezone.utc)
        events = [{
            'date': datetime(2026, 9, 15, 0, 0, tzinfo=timezone.utc),
            'name': 'ETH Pectra upgrade',
            'block_hours': 12,
            'reduce_hours': 24,
        }]
        action, mult, reason = _check_crypto_event_gate(now, events)
        assert action == 'block'
        assert 'ETH Pectra upgrade' in reason

    def test_reduce_within_crypto_event(self):
        """Crypto event within reduce window → reduce."""
        now = datetime(2026, 9, 14, 8, 0, tzinfo=timezone.utc)
        events = [{
            'date': datetime(2026, 9, 15, 0, 0, tzinfo=timezone.utc),
            'name': 'ETH Pectra upgrade',
            'block_hours': 12,
            'reduce_hours': 24,
        }]
        action, mult, reason = _check_crypto_event_gate(now, events)
        assert action == 'reduce'
        assert mult == 0.5

    def test_allow_beyond_crypto_event(self):
        """Crypto event far away → allow."""
        now = datetime(2026, 9, 1, 0, 0, tzinfo=timezone.utc)
        events = [{
            'date': datetime(2026, 9, 15, 0, 0, tzinfo=timezone.utc),
            'name': 'ETH Pectra upgrade',
            'block_hours': 12,
            'reduce_hours': 24,
        }]
        action, mult, _ = _check_crypto_event_gate(now, events)
        assert action == 'allow'

    def test_empty_events_allow(self):
        """No crypto events → allow."""
        now = datetime(2026, 6, 1, tzinfo=timezone.utc)
        action, mult, _ = _check_crypto_event_gate(now, [])
        assert action == 'allow'

    def test_past_events_ignored(self):
        """Past crypto events are skipped."""
        now = datetime(2026, 10, 1, tzinfo=timezone.utc)
        events = [{
            'date': datetime(2026, 9, 15, 0, 0, tzinfo=timezone.utc),
            'name': 'Past event',
            'block_hours': 12,
            'reduce_hours': 24,
        }]
        action, mult, _ = _check_crypto_event_gate(now, events)
        assert action == 'allow'

    def test_crypto_gate_wired_into_check_event_gate(self):
        """Crypto events are checked for crypto asset_type."""
        crypto_events = [{
            'date': datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc),
            'name': 'Test upgrade',
            'block_hours': 12,
            'reduce_hours': 24,
        }]
        with patch('src.analysis.event_calendar._get_crypto_events',
                   return_value=crypto_events):
            with patch('src.analysis.event_calendar.datetime') as mock_dt:
                mock_dt.now.return_value = datetime(2026, 6, 1, 6, 0,
                                                     tzinfo=timezone.utc)
                mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
                action, _, reason = check_event_gate('BTC', 'BUY',
                                                      asset_type='crypto')
                assert action == 'block'
                assert 'Test upgrade' in reason

    def test_crypto_events_in_upcoming(self):
        """Crypto events appear in get_upcoming_macro_events."""
        crypto_events = [{
            'date': datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc),
            'name': 'Test upgrade',
            'block_hours': 12,
            'reduce_hours': 24,
        }]
        with patch('src.analysis.event_calendar._get_crypto_events',
                   return_value=crypto_events):
            with patch('src.analysis.event_calendar.datetime') as mock_dt:
                mock_dt.now.return_value = datetime(2026, 5, 15, 0, 0,
                                                     tzinfo=timezone.utc)
                mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
                events = get_upcoming_macro_events(days_ahead=30)
                crypto = [e for e in events if 'Crypto' in e['event_type']]
                assert len(crypto) == 1
                assert 'Test upgrade' in crypto[0]['event_type']


class TestFOMCAndCPIDates:
    def test_fomc_dates_are_in_2026(self):
        for dt in FOMC_DATES_2026:
            assert dt.year == 2026

    def test_cpi_dates_are_in_2026(self):
        for dt in CPI_DATES_2026:
            assert dt.year == 2026

    def test_fomc_count(self):
        assert len(FOMC_DATES_2026) == 8

    def test_cpi_count(self):
        assert len(CPI_DATES_2026) == 12

    def test_dates_are_timezone_aware(self):
        for dt in FOMC_DATES_2026 + CPI_DATES_2026:
            assert dt.tzinfo is not None
