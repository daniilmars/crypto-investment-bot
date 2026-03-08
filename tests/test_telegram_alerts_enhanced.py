"""Tests for src/notify/telegram_alerts_enhanced.py"""

import asyncio
import pytest
from unittest.mock import patch, AsyncMock, MagicMock

from src.notify.telegram_alerts_enhanced import (
    check_realtime_alerts,
    reset_alert_state,
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
