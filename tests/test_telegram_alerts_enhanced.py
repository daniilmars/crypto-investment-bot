"""Tests for src/notify/telegram_alerts_enhanced.py"""

import asyncio
import pytest
from unittest.mock import patch, AsyncMock, MagicMock

from src.notify.telegram_alerts_enhanced import (
    build_morning_briefing,
    build_portfolio_digest,
    check_realtime_alerts,
    reset_alert_state,
    send_morning_briefing,
    send_portfolio_digest,
    send_realtime_alerts,
)


def run_async(coro):
    """Helper to run async functions in sync tests."""
    return asyncio.run(coro)


@pytest.fixture(autouse=True)
def clean_alert_state():
    """Reset module-level state before each test."""
    reset_alert_state()
    yield
    reset_alert_state()


@pytest.fixture
def mock_regime():
    return {
        'regime': 'RISK_ON',
        'position_size_multiplier': 1.0,
        'suppress_buys': False,
        'score': 4.2,
        'signals': {'vix_signal': 'low', 'sp500_trend': 'bullish'},
        'indicators': {'vix': 14.3, 'sp500_change_pct': 0.4, 'btc_price': 67000},
    }


class TestBuildMorningBriefing:
    @patch('src.notify.telegram_alerts_enhanced.get_upcoming_macro_events', return_value=[])
    @patch('src.notify.telegram_alerts_enhanced.get_open_positions', return_value=[])
    @patch('src.notify.telegram_alerts_enhanced.get_daily_pnl', return_value=245.0)
    @patch('src.notify.telegram_alerts_enhanced.get_account_balance')
    @patch('src.notify.telegram_alerts_enhanced.get_macro_regime')
    def test_basic_briefing(self, mock_regime_fn, mock_bal, mock_pnl,
                            mock_pos, mock_events):
        mock_regime_fn.return_value = {
            'regime': 'RISK_ON', 'score': 4.2,
            'indicators': {'vix': 14.3}, 'signals': {},
            'position_size_multiplier': 1.0, 'suppress_buys': False,
        }
        mock_bal.return_value = {'total_usd': 10000, 'USDT': 5000}

        msg = build_morning_briefing()
        assert 'MORNING BRIEFING' in msg
        assert 'RISK_ON' in msg
        assert 'VIX' in msg
        assert '$10,000' in msg or '$20,000' in msg

    @patch('src.notify.telegram_alerts_enhanced.get_upcoming_macro_events')
    @patch('src.notify.telegram_alerts_enhanced.get_open_positions', return_value=[])
    @patch('src.notify.telegram_alerts_enhanced.get_daily_pnl', return_value=0)
    @patch('src.notify.telegram_alerts_enhanced.get_account_balance')
    @patch('src.notify.telegram_alerts_enhanced.get_macro_regime')
    def test_with_events(self, mock_regime_fn, mock_bal, mock_pnl,
                         mock_pos, mock_events):
        mock_regime_fn.return_value = {
            'regime': 'CAUTION', 'score': 1.1,
            'indicators': {'vix': 18.7}, 'signals': {},
            'position_size_multiplier': 0.7, 'suppress_buys': False,
        }
        mock_bal.return_value = {'total_usd': 5000, 'USDT': 5000}
        mock_events.return_value = [
            {'event_type': 'CPI', 'hours_until': 4, 'event_date': '2026-03-07'},
        ]

        msg = build_morning_briefing()
        assert 'CPI' in msg
        assert 'Events Today' in msg


class TestBuildPortfolioDigest:
    @patch('src.execution.circuit_breaker.get_circuit_breaker_status')
    @patch('src.notify.telegram_alerts_enhanced.get_macro_regime')
    @patch('src.notify.telegram_alerts_enhanced.get_open_positions', return_value=[])
    @patch('src.notify.telegram_alerts_enhanced.get_daily_pnl', return_value=123.0)
    @patch('src.notify.telegram_alerts_enhanced.get_account_balance')
    def test_basic_digest(self, mock_bal, mock_pnl, mock_pos,
                          mock_regime, mock_cb):
        mock_bal.return_value = {'total_usd': 10000, 'USDT': 5000}
        mock_regime.return_value = {'regime': 'RISK_ON'}
        mock_cb.return_value = {'in_cooldown': False}

        msg = build_portfolio_digest()
        assert 'PORTFOLIO' in msg
        assert '$10,000' in msg or '$20,000' in msg
        assert 'RISK_ON' in msg


class TestCheckRealtimeAlerts:
    def test_no_alerts_on_first_call(self, mock_regime):
        alerts = check_realtime_alerts(mock_regime)
        assert alerts == []

    def test_regime_change_alert(self, mock_regime):
        check_realtime_alerts(mock_regime)

        mock_regime['regime'] = 'CAUTION'
        mock_regime['score'] = 1.1
        mock_regime['position_size_multiplier'] = 0.7
        alerts = check_realtime_alerts(mock_regime)

        assert len(alerts) == 1
        assert 'REGIME CHANGE' in alerts[0]
        assert 'RISK_ON' in alerts[0]
        assert 'CAUTION' in alerts[0]

    def test_no_alert_same_regime(self, mock_regime):
        check_realtime_alerts(mock_regime)
        alerts = check_realtime_alerts(mock_regime)
        assert len(alerts) == 0

    def test_vix_spike_alert(self, mock_regime):
        check_realtime_alerts(mock_regime)

        mock_regime['indicators']['vix'] = 18.7
        alerts = check_realtime_alerts(mock_regime)

        assert len(alerts) == 1
        assert 'VIX SPIKE' in alerts[0]
        assert '14.3' in alerts[0]
        assert '18.7' in alerts[0]

    def test_vix_below_threshold(self, mock_regime):
        check_realtime_alerts(mock_regime)

        mock_regime['indicators']['vix'] = 15.0
        alerts = check_realtime_alerts(mock_regime)
        assert len(alerts) == 0

    def test_both_regime_and_vix(self, mock_regime):
        check_realtime_alerts(mock_regime)

        mock_regime['regime'] = 'RISK_OFF'
        mock_regime['indicators']['vix'] = 25.0
        alerts = check_realtime_alerts(mock_regime)

        assert len(alerts) == 2

    @patch('src.notify.telegram_alerts_enhanced.app_config', {
        'settings': {'telegram_enhancements': {'realtime_alerts': {'enabled': False}}},
        'notification_services': {'telegram': {'chat_id': '123'}},
    })
    def test_disabled(self, mock_regime):
        check_realtime_alerts(mock_regime)
        mock_regime['regime'] = 'RISK_OFF'
        alerts = check_realtime_alerts(mock_regime)
        assert len(alerts) == 0


class TestSendRealtimeAlerts:
    @patch('src.notify.telegram_alerts_enhanced._get_chat_id', return_value='12345')
    def test_sends_alerts(self, mock_chat):
        app = MagicMock()
        app.bot.send_message = AsyncMock()

        run_async(send_realtime_alerts(app, ['Alert 1', 'Alert 2']))
        assert app.bot.send_message.call_count == 2

    @patch('src.notify.telegram_alerts_enhanced._get_chat_id', return_value='')
    def test_no_chat_id(self, mock_chat):
        app = MagicMock()
        app.bot.send_message = AsyncMock()

        run_async(send_realtime_alerts(app, ['Alert 1']))
        app.bot.send_message.assert_not_called()


class TestSendMorningBriefing:
    @patch('src.notify.telegram_alerts_enhanced.build_morning_briefing',
           return_value='Test briefing')
    @patch('src.notify.telegram_alerts_enhanced._get_chat_id', return_value='12345')
    def test_sends(self, mock_chat, mock_build):
        app = MagicMock()
        app.bot.send_message = AsyncMock()

        run_async(send_morning_briefing(app))
        app.bot.send_message.assert_called_once()


class TestSendPortfolioDigest:
    @patch('src.notify.telegram_alerts_enhanced.build_portfolio_digest',
           return_value='Test digest')
    @patch('src.notify.telegram_alerts_enhanced._get_chat_id', return_value='12345')
    def test_sends(self, mock_chat, mock_build):
        app = MagicMock()
        app.bot.send_message = AsyncMock()

        run_async(send_portfolio_digest(app))
        app.bot.send_message.assert_called_once()
