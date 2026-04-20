"""Tests for src/analysis/dynamic_risk.resolve_sl_tp_for_entry."""

import pytest

from src.analysis.dynamic_risk import resolve_sl_tp_for_entry


BASE_SETTINGS = {
    'dynamic_risk': {
        'enabled': True,
        'atr_period': 14,
        'sl_atr_multiplier': 2.0,
        'tp_atr_multiplier': 5.0,
        'sl_floor': 0.07,
        'sl_ceiling': 0.15,
        'tp_floor': 0.10,
        'tp_ceiling': 0.50,
    },
    'stop_loss_percentage': 0.10,
    'take_profit_percentage': 0.50,
}


def test_cycle_cache_hit_used():
    """cycle_atr_cache[symbol] trumps live-compute and fallback."""
    sl, tp, source = resolve_sl_tp_for_entry(
        'BTC', current_price=75000.0, asset_type='crypto',
        settings=BASE_SETTINGS, cycle_atr_cache={'BTC': 0.05})
    assert source == 'atr_cache'
    # 0.05 * 2.0 = 0.10 (within sl_floor 0.07..ceiling 0.15)
    assert sl == pytest.approx(0.10, abs=0.001)
    # 0.05 * 5.0 = 0.25 (within tp_floor 0.10..ceiling 0.50)
    assert tp == pytest.approx(0.25, abs=0.001)


def test_live_atr_from_daily_klines():
    """No cache hit → live compute from daily_klines if present."""
    # Fabricate 15 klines with rising prices so ATR is non-trivial
    klines = [
        {'high': 100 + i, 'low': 95 + i, 'close': 98 + i}
        for i in range(15)
    ]
    sl, tp, source = resolve_sl_tp_for_entry(
        'AAPL', current_price=112.0, asset_type='stock',
        settings=BASE_SETTINGS, daily_klines=klines)
    assert source == 'atr_live'
    # Value must be clamped inside configured bounds
    assert 0.07 <= sl <= 0.15
    assert 0.10 <= tp <= 0.50


def test_config_fallback_when_no_cache_no_klines():
    """With neither cache nor klines, static config values are returned."""
    sl, tp, source = resolve_sl_tp_for_entry(
        'XYZ', current_price=50.0, asset_type='stock',
        settings=BASE_SETTINGS)
    assert source == 'config_fallback'
    assert sl == 0.10
    assert tp == 0.50


def test_config_fallback_when_dyn_disabled():
    """dynamic_risk.enabled=False → always config_fallback regardless of inputs."""
    settings = dict(BASE_SETTINGS)
    settings['dynamic_risk'] = dict(settings['dynamic_risk'])
    settings['dynamic_risk']['enabled'] = False
    sl, tp, source = resolve_sl_tp_for_entry(
        'BTC', current_price=75000.0, asset_type='crypto',
        settings=settings, cycle_atr_cache={'BTC': 0.05})
    assert source == 'config_fallback'
    assert sl == 0.10
    assert tp == 0.50


def test_config_fallback_when_klines_too_short():
    """<15 klines → can't compute ATR(14) → config_fallback."""
    klines = [{'high': 100, 'low': 95, 'close': 98}] * 10  # 10 < period+1
    sl, tp, source = resolve_sl_tp_for_entry(
        'AAPL', current_price=100.0, asset_type='stock',
        settings=BASE_SETTINGS, daily_klines=klines)
    assert source == 'config_fallback'


def test_never_returns_none():
    """Core invariant: returned SL/TP must never be None."""
    scenarios = [
        # (cache, klines, dyn_enabled)
        ({}, None, True),
        (None, None, False),
        ({'X': 0.03}, None, True),
        (None, [{'high': 100, 'low': 99, 'close': 99.5}] * 20, True),
    ]
    for cache, klines, dyn_enabled in scenarios:
        settings = dict(BASE_SETTINGS)
        settings['dynamic_risk'] = dict(settings['dynamic_risk'])
        settings['dynamic_risk']['enabled'] = dyn_enabled
        sl, tp, source = resolve_sl_tp_for_entry(
            'X', current_price=100.0, asset_type='stock',
            settings=settings, cycle_atr_cache=cache, daily_klines=klines)
        assert sl is not None, f"SL None for {cache=} {klines=} {dyn_enabled=}"
        assert tp is not None, f"TP None for {cache=} {klines=} {dyn_enabled=}"
        assert sl > 0
        assert tp > 0


def test_empty_settings_dict_still_returns_sane_defaults():
    """Even if settings is empty, hard-coded defaults kick in."""
    sl, tp, source = resolve_sl_tp_for_entry(
        'X', current_price=100.0, asset_type='stock', settings={})
    # Bare settings dict → defaults from helper signature (0.10 / 0.50)
    assert sl > 0
    assert tp > 0
    assert source == 'config_fallback'
