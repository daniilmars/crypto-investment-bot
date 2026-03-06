"""Tests for src/notify/telegram_dashboard.py"""

import pytest
from unittest.mock import patch, MagicMock, AsyncMock
from datetime import datetime, timezone

from src.notify.telegram_dashboard import (
    build_dashboard_message,
    build_crypto_detail,
    build_stocks_detail,
    build_auto_detail,
    build_regime_detail,
    _enrich_positions,
    _get_price_sparkline,
)


@pytest.fixture
def mock_balance():
    return {'USDT': 5000, 'total_usd': 10000}


@pytest.fixture
def mock_positions():
    return [
        {'symbol': 'BTC', 'entry_price': 60000, 'quantity': 0.1, 'status': 'OPEN'},
        {'symbol': 'ETH', 'entry_price': 3000, 'quantity': 1.0, 'status': 'OPEN'},
    ]


@pytest.fixture
def mock_stock_positions():
    return [
        {'symbol': 'NVDA', 'entry_price': 130, 'quantity': 10, 'status': 'OPEN'},
        {'symbol': 'SAP.DE', 'entry_price': 180, 'quantity': 5, 'status': 'OPEN'},
    ]


@pytest.fixture
def mock_regime():
    return {
        'regime': 'RISK_ON',
        'position_size_multiplier': 1.0,
        'suppress_buys': False,
        'score': 4.2,
        'signals': {
            'vix_signal': 'low',
            'vix_trend': 'falling',
            'sp500_trend': 'bullish',
            'yield_direction': 'stable',
            'btc_trend': 'bullish',
        },
        'indicators': {'vix': 14.3, 'sp500_sma': 5200},
    }


@pytest.fixture
def mock_cb_status():
    return {'in_cooldown': False, 'cooldown_hours': 4,
            'balance_floor': 8000, 'daily_loss_limit_pct': 0.05,
            'max_drawdown_pct': 0.1, 'max_consecutive_losses': 5}


class TestEnrichPositions:
    @patch('src.notify.telegram_dashboard._get_position_price', return_value=65000)
    def test_enriches_crypto(self, mock_price):
        positions = [{'symbol': 'BTC', 'entry_price': 60000, 'quantity': 0.1}]
        result = _enrich_positions(positions)
        assert len(result) == 1
        assert result[0]['current_price'] == 65000
        assert result[0]['pnl'] == pytest.approx(500.0)
        assert result[0]['pnl_pct'] == pytest.approx(8.333, rel=0.01)

    @patch('src.notify.telegram_dashboard._get_position_price', return_value=0)
    def test_fallback_to_entry(self, mock_price):
        positions = [{'symbol': 'XYZ', 'entry_price': 100, 'quantity': 1}]
        result = _enrich_positions(positions)
        assert result[0]['current_price'] == 100  # fallback


class TestBuildDashboardMessage:
    @patch('src.notify.telegram_dashboard.get_last_signal', return_value=None)
    @patch('src.notify.telegram_dashboard.get_macro_regime')
    @patch('src.notify.telegram_dashboard.get_circuit_breaker_status')
    @patch('src.notify.telegram_dashboard.get_daily_pnl', return_value=50.0)
    @patch('src.notify.telegram_dashboard.get_open_positions', return_value=[])
    @patch('src.notify.telegram_dashboard.get_account_balance')
    def test_basic_dashboard(self, mock_bal, mock_pos, mock_pnl, mock_cb,
                             mock_regime, mock_signal):
        mock_bal.return_value = {'total_usd': 10000, 'USDT': 5000}
        mock_regime.return_value = {
            'regime': 'RISK_ON', 'position_size_multiplier': 1.0,
            'suppress_buys': False, 'score': 4,
            'signals': {}, 'indicators': {},
        }
        mock_cb.return_value = {'in_cooldown': False}

        msg = build_dashboard_message()
        assert 'DASHBOARD' in msg
        assert 'Portfolio' in msg
        assert 'RISK_ON' in msg
        assert 'CB:' in msg

    @patch('src.notify.telegram_dashboard.get_last_signal')
    @patch('src.notify.telegram_dashboard.get_macro_regime')
    @patch('src.notify.telegram_dashboard.get_circuit_breaker_status')
    @patch('src.notify.telegram_dashboard.get_daily_pnl', return_value=0)
    @patch('src.notify.telegram_dashboard.get_open_positions', return_value=[])
    @patch('src.notify.telegram_dashboard.get_account_balance')
    def test_with_last_signal(self, mock_bal, mock_pos, mock_pnl, mock_cb,
                              mock_regime, mock_signal):
        mock_bal.return_value = {'total_usd': 5000, 'USDT': 5000}
        mock_regime.return_value = {
            'regime': 'CAUTION', 'position_size_multiplier': 0.7,
            'suppress_buys': False, 'score': 1,
            'signals': {}, 'indicators': {},
        }
        mock_cb.return_value = {'in_cooldown': False}
        mock_signal.return_value = {
            'signal_type': 'BUY', 'symbol': 'ETH',
            'timestamp': datetime.now(timezone.utc).isoformat(),
        }

        msg = build_dashboard_message()
        assert 'BUY' in msg
        assert 'ETH' in msg


class TestBuildCryptoDetail:
    @patch('src.notify.telegram_dashboard._get_price_sparkline', return_value='▁▃▅▇█')
    @patch('src.notify.telegram_dashboard._get_position_price', return_value=65000)
    @patch('src.notify.telegram_dashboard.get_open_positions')
    def test_with_positions(self, mock_pos, mock_price, mock_spark):
        mock_pos.return_value = [
            {'symbol': 'BTC', 'entry_price': 60000, 'quantity': 0.1},
        ]
        msg = build_crypto_detail()
        assert 'BTC' in msg
        assert '▁▃▅▇█' in msg

    @patch('src.notify.telegram_dashboard.get_open_positions', return_value=[])
    def test_no_positions(self, mock_pos):
        msg = build_crypto_detail()
        assert 'No open crypto' in msg


class TestBuildStocksDetail:
    @patch('src.notify.telegram_dashboard._get_position_price', return_value=150)
    @patch('src.notify.telegram_dashboard.get_open_positions')
    def test_grouped_by_region(self, mock_pos, mock_price):
        mock_pos.return_value = [
            {'symbol': 'NVDA', 'entry_price': 130, 'quantity': 10},
            {'symbol': 'SAP.DE', 'entry_price': 180, 'quantity': 5},
        ]
        msg = build_stocks_detail()
        assert 'US' in msg
        assert 'EU' in msg


class TestBuildRegimeDetail:
    @patch('src.notify.telegram_dashboard.get_macro_regime')
    def test_basic(self, mock_regime):
        mock_regime.return_value = {
            'regime': 'RISK_ON',
            'position_size_multiplier': 1.0,
            'suppress_buys': False,
            'score': 4.2,
            'signals': {
                'vix_signal': 'low', 'vix_trend': 'falling',
                'sp500_trend': 'bullish', 'yield_direction': 'stable',
                'btc_trend': 'bullish',
            },
            'indicators': {'vix': 14.3},
        }
        msg = build_regime_detail()
        assert 'RISK_ON' in msg
        assert 'VIX' in msg.upper() or 'vix' in msg.lower()


class TestBuildAutoDetail:
    @patch('src.notify.telegram_dashboard.app_config', {
        'settings': {'auto_trading': {'enabled': False}}
    })
    def test_disabled(self):
        msg = build_auto_detail()
        assert 'disabled' in msg

    @patch('src.notify.telegram_dashboard.get_account_balance')
    @patch('src.notify.telegram_dashboard.get_open_positions', return_value=[])
    @patch('src.notify.telegram_dashboard.app_config', {
        'settings': {
            'auto_trading': {'enabled': True, 'paper_trading_initial_capital': 10000},
            'paper_trading_initial_capital': 10000,
        }
    })
    def test_enabled(self, mock_pos, mock_bal):
        mock_bal.return_value = {'total_usd': 10500, 'USDT': 5000}
        msg = build_auto_detail()
        assert 'Auto-Bot' in msg
        assert '$10,500' in msg
