"""Tests for the macro regime detector."""

import time
from unittest.mock import patch, MagicMock

import pandas as pd
import pytest

from src.analysis.macro_regime import (
    MacroRegime,
    get_macro_regime,
    _fetch_macro_indicators,
    _compute_signals,
    _classify_regime,
    _compute_score,
    clear_regime_cache,
    _regime_cache,
)


@pytest.fixture(autouse=True)
def clear_cache():
    """Clear regime cache before each test."""
    clear_regime_cache()
    yield
    clear_regime_cache()


def _make_series(values):
    """Helper to create a pandas Series from a list."""
    return pd.Series(values, dtype=float)


class TestComputeSignals:
    """Tests for signal derivation from indicators."""

    def test_risk_on_signals(self):
        """Low VIX, S&P above SMA200, BTC above SMA50 → all positive."""
        indicators = {
            'vix': {'current': 14.0, 'sma20': 16.0},
            'sp500': {'current': 5500.0, 'sma200': 5200.0},
            'yield_10y': {'current': 4.2, 'prev_20d': 4.25},
            'btc': {'current': 90000.0, 'sma50': 85000.0},
        }
        signals = _compute_signals(indicators)
        assert signals['vix_signal'] == 1    # below 18
        assert signals['vix_trend'] == 1     # falling (14/16 < 0.95)
        assert signals['sp500_trend'] == 1   # above SMA200
        assert signals['btc_trend'] == 1     # above SMA50
        assert _compute_score(signals) >= 2  # RISK_ON territory

    def test_risk_off_signals(self):
        """High VIX, S&P below SMA200, BTC below SMA50 → all negative."""
        indicators = {
            'vix': {'current': 38.0, 'sma20': 30.0},
            'sp500': {'current': 4500.0, 'sma200': 5000.0},
            'yield_10y': {'current': 5.0, 'prev_20d': 4.5},
            'btc': {'current': 60000.0, 'sma50': 70000.0},
        }
        signals = _compute_signals(indicators)
        assert signals['vix_signal'] == -2   # above 35
        assert signals['vix_trend'] == -1    # rising (38/30 > 1.05)
        assert signals['sp500_trend'] == -1  # below SMA200
        assert signals['yield_direction'] == -1  # rising fast
        assert signals['btc_trend'] == -1    # below SMA50
        assert _compute_score(signals) <= -2  # RISK_OFF territory

    def test_caution_signals(self):
        """Mixed signals → score in CAUTION band (-1 to +1)."""
        indicators = {
            'vix': {'current': 20.0, 'sma20': 20.0},         # neutral (0 + 0)
            'sp500': {'current': 5100.0, 'sma200': 5000.0},   # above SMA (+1)
            'yield_10y': {'current': 4.5, 'prev_20d': 4.1},   # rising fast (-1)
            'btc': {'current': 70000.0, 'sma50': 80000.0},    # below SMA (-1)
        }
        signals = _compute_signals(indicators)
        assert signals['vix_signal'] == 0    # 18-25 range
        score = _compute_score(signals)
        assert -1 <= score <= 1  # CAUTION band

    def test_missing_indicators_default_to_zero(self):
        """None indicators produce neutral signals."""
        indicators = {
            'vix': None,
            'sp500': None,
            'yield_10y': None,
            'btc': None,
        }
        signals = _compute_signals(indicators)
        assert all(v == 0 for v in signals.values())

    def test_vix_threshold_boundaries(self):
        """Test VIX at exact threshold values (uses > not >=)."""
        # At exactly 18 → not > 18, so risk-on (+1)
        indicators = {'vix': {'current': 18.0, 'sma20': 18.0},
                      'sp500': None, 'yield_10y': None, 'btc': None}
        signals = _compute_signals(indicators)
        assert signals['vix_signal'] == 1

        # Just above 18 → neutral
        indicators['vix'] = {'current': 18.1, 'sma20': 18.1}
        signals = _compute_signals(indicators)
        assert signals['vix_signal'] == 0

        # At 25 → > 25 is False, so > 18 is True → neutral
        indicators['vix'] = {'current': 25.0, 'sma20': 25.0}
        signals = _compute_signals(indicators)
        assert signals['vix_signal'] == 0

        # Just above 25 → risk-off (-1)
        indicators['vix'] = {'current': 25.1, 'sma20': 25.1}
        signals = _compute_signals(indicators)
        assert signals['vix_signal'] == -1

        # At 36 → extreme (-2)
        indicators['vix'] = {'current': 36.0, 'sma20': 36.0}
        signals = _compute_signals(indicators)
        assert signals['vix_signal'] == -2

    def test_yield_direction_thresholds(self):
        """Yield rising gently → neutral, falling → risk-on."""
        base = {'vix': None, 'sp500': None, 'btc': None}

        # Rising gently (< 0.3)
        indicators = {**base, 'yield_10y': {'current': 4.4, 'prev_20d': 4.2}}
        signals = _compute_signals(indicators)
        assert signals['yield_direction'] == 0

        # Falling
        indicators = {**base, 'yield_10y': {'current': 4.0, 'prev_20d': 4.2}}
        signals = _compute_signals(indicators)
        assert signals['yield_direction'] == 1


class TestClassifyRegime:
    """Tests for regime classification from signals."""

    def test_risk_on(self):
        signals = {'vix_signal': 1, 'vix_trend': 1, 'sp500_trend': 1,
                   'yield_direction': 0, 'btc_trend': 1}
        regime, mult, suppress = _classify_regime(signals, {})
        assert regime == MacroRegime.RISK_ON
        assert mult == 1.0
        assert suppress is False

    def test_risk_off(self):
        signals = {'vix_signal': -2, 'vix_trend': -1, 'sp500_trend': -1,
                   'yield_direction': -1, 'btc_trend': -1}
        regime, mult, suppress = _classify_regime(signals, {})
        assert regime == MacroRegime.RISK_OFF
        assert mult == 0.3
        assert suppress is True

    def test_caution(self):
        signals = {'vix_signal': 0, 'vix_trend': 0, 'sp500_trend': 1,
                   'yield_direction': 0, 'btc_trend': -1}
        regime, mult, suppress = _classify_regime(signals, {})
        assert regime == MacroRegime.CAUTION
        assert mult == 0.6
        assert suppress is False

    def test_custom_multipliers(self):
        cfg = {'risk_on_multiplier': 1.2, 'caution_multiplier': 0.8,
               'risk_off_multiplier': 0.1, 'suppress_buys_in_risk_off': False}
        signals = {'vix_signal': -2, 'vix_trend': -1, 'sp500_trend': -1,
                   'yield_direction': 0, 'btc_trend': 0}
        regime, mult, suppress = _classify_regime(signals, cfg)
        assert regime == MacroRegime.RISK_OFF
        assert mult == 0.1
        assert suppress is False  # overridden by config

    def test_boundary_score_minus_one_is_caution(self):
        signals = {'vix_signal': 0, 'vix_trend': 0, 'sp500_trend': -1,
                   'yield_direction': 0, 'btc_trend': 0}
        regime, _, _ = _classify_regime(signals, {})
        assert regime == MacroRegime.CAUTION

    def test_boundary_score_plus_one_is_caution(self):
        signals = {'vix_signal': 0, 'vix_trend': 0, 'sp500_trend': 1,
                   'yield_direction': 0, 'btc_trend': 0}
        regime, _, _ = _classify_regime(signals, {})
        assert regime == MacroRegime.CAUTION


class TestGetMacroRegime:
    """Tests for the main get_macro_regime() entry point."""

    @patch('src.analysis.macro_regime._fetch_macro_indicators')
    def test_caches_result(self, mock_fetch):
        """Second call within TTL should not re-fetch."""
        mock_fetch.return_value = {
            'vix': {'current': 15, 'sma20': 16},
            'sp500': {'current': 5500, 'sma200': 5200},
            'yield_10y': {'current': 4.2, 'prev_20d': 4.25},
            'btc': {'current': 90000, 'sma50': 85000},
        }

        result1 = get_macro_regime()
        result2 = get_macro_regime()

        assert mock_fetch.call_count == 1
        assert result1['regime'] == result2['regime']

    @patch('src.analysis.macro_regime._fetch_macro_indicators')
    def test_force_refresh_bypasses_cache(self, mock_fetch):
        mock_fetch.return_value = {
            'vix': None, 'sp500': None, 'yield_10y': None, 'btc': None,
        }

        get_macro_regime()
        get_macro_regime(force_refresh=True)

        assert mock_fetch.call_count == 2

    @patch('src.analysis.macro_regime.app_config')
    def test_disabled_returns_caution(self, mock_config):
        mock_config.get.return_value = {'macro_regime': {'enabled': False}}
        result = get_macro_regime()
        assert result['regime'] == 'CAUTION'
        assert result['suppress_buys'] is False

    @patch('src.analysis.macro_regime._fetch_macro_indicators')
    def test_all_none_indicators_returns_caution(self, mock_fetch):
        mock_fetch.return_value = {
            'vix': None, 'sp500': None, 'yield_10y': None, 'btc': None,
        }
        result = get_macro_regime()
        assert result['regime'] == 'CAUTION'
        assert result['score'] == 0

    @patch('src.analysis.macro_regime._fetch_macro_indicators')
    def test_result_structure(self, mock_fetch):
        mock_fetch.return_value = {
            'vix': {'current': 15, 'sma20': 16},
            'sp500': {'current': 5500, 'sma200': 5200},
            'yield_10y': {'current': 4.2, 'prev_20d': 4.25},
            'btc': {'current': 90000, 'sma50': 85000},
        }
        result = get_macro_regime()
        assert 'regime' in result
        assert 'position_size_multiplier' in result
        assert 'suppress_buys' in result
        assert 'indicators' in result
        assert 'signals' in result
        assert 'score' in result
        assert 'classified_at' in result
