"""Tests for auto signal quality gate (Pillars 1 & 2)."""

import asyncio
from unittest.mock import patch, AsyncMock

from src.orchestration.trade_executor import process_trade_signal


def _mock_config(min_strength=0.65, min_sell_strength=0.50, caution_boost=0.10):
    return {
        'settings': {
            'auto_trading': {
                'min_signal_strength': min_strength,
                'min_sell_signal_strength': min_sell_strength,
                'caution_strength_boost': caution_boost,
            },
            'min_trade_notional': 5.00,
        }
    }


def _base_args(**overrides):
    """Common arguments for process_trade_signal."""
    args = {
        'symbol': 'BTC',
        'current_price': 50000.0,
        'positions': [],
        'balance': 10000.0,
        'risk_pct': 0.05,
        'signal_cooldown_hours': 4,
        'max_positions': 5,
        'suppress_buys': False,
        'macro_multiplier': 1.0,
        'is_auto': True,
        'trading_strategy': 'auto',
        'label': 'AUTO',
    }
    args.update(overrides)
    return args


@patch('src.orchestration.trade_executor.check_signal_cooldown', new_callable=AsyncMock, return_value=False)
@patch('src.orchestration.trade_executor.check_stoploss_cooldown', new_callable=AsyncMock, return_value=False)
@patch('src.orchestration.trade_executor.app_config', _mock_config())
def test_auto_buy_weak_signal_blocked(mock_sl_cd, mock_sig_cd):
    """Auto BUY with strength 0.5 should be blocked (below 0.65 threshold)."""
    args = _base_args(signal={'signal': 'BUY', 'signal_strength': 0.5,
                              'symbol': 'BTC', 'current_price': 50000})
    result = asyncio.run(process_trade_signal(**args))
    assert result is None


@patch('src.orchestration.trade_executor.execute_buy', new_callable=AsyncMock, return_value={'status': 'FILLED'})
@patch('src.orchestration.trade_executor.check_buy_gates', return_value=(True, 1.0, ''))
@patch('src.orchestration.trade_executor.check_signal_cooldown', new_callable=AsyncMock, return_value=False)
@patch('src.orchestration.trade_executor.check_stoploss_cooldown', new_callable=AsyncMock, return_value=False)
@patch('src.orchestration.trade_executor.app_config', _mock_config())
def test_auto_buy_strong_signal_passes(mock_sl_cd, mock_sig_cd, mock_gates, mock_execute):
    """Auto BUY with strength 0.7 should pass the gate (above 0.65)."""
    args = _base_args(signal={'signal': 'BUY', 'signal_strength': 0.7,
                              'symbol': 'BTC', 'current_price': 50000})
    result = asyncio.run(process_trade_signal(**args))
    assert result == {'status': 'FILLED'}


@patch('src.orchestration.trade_executor.execute_buy', new_callable=AsyncMock, return_value={'status': 'FILLED'})
@patch('src.orchestration.trade_executor.check_buy_gates', return_value=(True, 1.0, ''))
@patch('src.orchestration.trade_executor.check_signal_cooldown', new_callable=AsyncMock, return_value=False)
@patch('src.orchestration.trade_executor.check_stoploss_cooldown', new_callable=AsyncMock, return_value=False)
@patch('src.orchestration.trade_executor.app_config', _mock_config())
def test_manual_buy_weak_signal_not_blocked(mock_sl_cd, mock_sig_cd, mock_gates, mock_execute):
    """Manual BUY with strength 0.5 should NOT be blocked by the auto gate."""
    args = _base_args(
        is_auto=False, trading_strategy='manual',
        signal={'signal': 'BUY', 'signal_strength': 0.5,
                'symbol': 'BTC', 'current_price': 50000})
    result = asyncio.run(process_trade_signal(**args))
    assert result == {'status': 'FILLED'}
    mock_execute.assert_called_once()


@patch('src.orchestration.trade_executor.check_signal_cooldown', new_callable=AsyncMock, return_value=False)
@patch('src.orchestration.trade_executor.check_stoploss_cooldown', new_callable=AsyncMock, return_value=False)
@patch('src.orchestration.trade_executor.app_config', _mock_config())
def test_auto_sell_weak_signal_blocked(mock_sl_cd, mock_sig_cd):
    """Auto SELL with strength 0.4 should be blocked (below 0.50 threshold)."""
    args = _base_args(signal={'signal': 'SELL', 'signal_strength': 0.4,
                              'symbol': 'BTC', 'current_price': 50000})
    result = asyncio.run(process_trade_signal(**args))
    assert result is None


# --- Pillar 2: Regime-Aware Confidence Boost ---

@patch('src.orchestration.trade_executor.check_signal_cooldown', new_callable=AsyncMock, return_value=False)
@patch('src.orchestration.trade_executor.check_stoploss_cooldown', new_callable=AsyncMock, return_value=False)
@patch('src.orchestration.trade_executor.app_config', _mock_config())
def test_caution_regime_raises_threshold(mock_sl_cd, mock_sig_cd):
    """Auto BUY with strength 0.70 in CAUTION (macro_mult=0.6) should be blocked.

    Normal threshold=0.65, CAUTION boost=0.10, so effective threshold=0.75.
    Signal strength 0.70 < 0.75 -> blocked.
    """
    args = _base_args(
        macro_multiplier=0.6,
        signal={'signal': 'BUY', 'signal_strength': 0.70,
                'symbol': 'BTC', 'current_price': 50000})
    result = asyncio.run(process_trade_signal(**args))
    assert result is None


@patch('src.orchestration.trade_executor.execute_buy', new_callable=AsyncMock, return_value={'status': 'FILLED'})
@patch('src.orchestration.trade_executor.check_buy_gates', return_value=(True, 0.6, ''))
@patch('src.orchestration.trade_executor.check_signal_cooldown', new_callable=AsyncMock, return_value=False)
@patch('src.orchestration.trade_executor.check_stoploss_cooldown', new_callable=AsyncMock, return_value=False)
@patch('src.orchestration.trade_executor.app_config', _mock_config())
def test_caution_regime_strong_signal_passes(mock_sl_cd, mock_sig_cd, mock_gates, mock_execute):
    """Auto BUY with strength 0.80 in CAUTION should pass (0.80 >= 0.75)."""
    args = _base_args(
        macro_multiplier=0.6,
        signal={'signal': 'BUY', 'signal_strength': 0.80,
                'symbol': 'BTC', 'current_price': 50000})
    result = asyncio.run(process_trade_signal(**args))
    assert result == {'status': 'FILLED'}
