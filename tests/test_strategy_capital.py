"""Tests for per-strategy paper-trading capital pool resolution.

Backstop for the regression where every strategy got 2× $10k (one
crypto wallet + one stock wallet) instead of the configured per-strategy
amount, and disabled strategies still had phantom wallets.
"""
import sqlite3
from contextlib import contextmanager
from unittest.mock import MagicMock, patch

import pytest


_STRATEGIES_CFG = {
    'settings': {
        'paper_trading_initial_capital': 10000.0,
        'auto_trading': {'paper_trading_initial_capital': 9999.0},  # legacy
        'strategies': {
            'auto':         {'enabled': True,  'paper_trading_initial_capital': 10000.0},
            'conservative': {'enabled': True,  'paper_trading_initial_capital': 10000.0},
            'longterm':     {'enabled': True,  'paper_trading_initial_capital': 10000.0},
            'momentum':     {'enabled': False, 'paper_trading_initial_capital': 5000.0},
        },
    }
}


def _patched_balance(trading_strategy, asset_type=None,
                     pnl=0.0, locked=0.0, cfg=_STRATEGIES_CFG):
    """Helper: run _get_paper_balance with mocked DB returning given pnl + locked."""
    mock_conn = MagicMock()
    mock_conn.__class__ = sqlite3.Connection
    mock_cursor = MagicMock()
    mock_cursor.fetchone.side_effect = [(pnl,), (locked,)]

    @contextmanager
    def fake_cursor(_):
        yield mock_cursor

    with patch('src.execution.binance_trader.get_db_connection',
               return_value=mock_conn), \
         patch('src.execution.binance_trader.release_db_connection'), \
         patch('src.execution.binance_trader._cursor', fake_cursor), \
         patch('src.execution.binance_trader.app_config', cfg):
        from src.execution.binance_trader import _get_paper_balance
        return _get_paper_balance(asset_type=asset_type,
                                  trading_strategy=trading_strategy)


# --- _strategy_initial_capital ---

def test_strategy_capital_reads_per_strategy_yaml():
    with patch('src.execution.binance_trader.app_config', _STRATEGIES_CFG):
        from src.execution.binance_trader import _strategy_initial_capital
        assert _strategy_initial_capital('auto') == 10000.0
        assert _strategy_initial_capital('conservative') == 10000.0
        assert _strategy_initial_capital('longterm') == 10000.0


def test_strategy_capital_disabled_returns_none():
    """momentum is enabled=False → no wallet."""
    with patch('src.execution.binance_trader.app_config', _STRATEGIES_CFG):
        from src.execution.binance_trader import _strategy_initial_capital
        assert _strategy_initial_capital('momentum') is None


def test_strategy_capital_unknown_falls_back_to_global():
    with patch('src.execution.binance_trader.app_config', _STRATEGIES_CFG):
        from src.execution.binance_trader import _strategy_initial_capital
        # 'manual' or any unknown name → global default
        assert _strategy_initial_capital('manual') == 10000.0
        assert _strategy_initial_capital('typo_strat') == 10000.0


def test_strategy_capital_legacy_auto_path_still_works():
    """When settings.strategies absent, fall back to settings.auto_trading."""
    legacy = {'settings': {
        'paper_trading_initial_capital': 10000.0,
        'auto_trading': {'paper_trading_initial_capital': 5000.0},
    }}
    with patch('src.execution.binance_trader.app_config', legacy):
        from src.execution.binance_trader import _strategy_initial_capital
        assert _strategy_initial_capital('auto') == 5000.0


# --- _get_paper_balance: single-pool semantics ---

def test_each_strategy_starts_at_10k_no_double_wallet():
    """Each enabled strategy returns exactly $10k initial — not 2× $10k."""
    for strat in ('auto', 'conservative', 'longterm'):
        result = _patched_balance(strat)
        assert result['total_usd'] == 10000.0, f"{strat} should start at $10k"
        assert result['USDT'] == 10000.0


def test_disabled_strategy_returns_zero():
    """momentum is disabled → no phantom $10k wallet."""
    result = _patched_balance('momentum')
    assert result['total_usd'] == 0.0
    assert result['USDT'] == 0.0


def test_locked_subtracted_across_all_asset_types():
    """When strategy is set, locked filter ignores asset_type — single pool."""
    # $1000 locked across all asset types → free = 10000 - 1000 = 9000
    result = _patched_balance('conservative', asset_type='crypto', locked=1000.0)
    assert result['total_usd'] == 10000.0  # initial unchanged
    assert result['USDT'] == 9000.0  # 10000 - 1000 locked


def test_pnl_included_in_total():
    """Realized PnL grows the pool. -$50 closed PnL → 10000 - 50 = 9950."""
    result = _patched_balance('auto', pnl=-50.0, locked=0.0)
    assert result['total_usd'] == 9950.0
    assert result['USDT'] == 9950.0


def test_combined_pnl_and_locked():
    """initial + pnl − locked. 10000 + 100 − 500 = 9600."""
    result = _patched_balance('longterm', pnl=100.0, locked=500.0)
    assert result['total_usd'] == 10100.0
    assert result['USDT'] == 9600.0


def test_asset_type_ignored_when_strategy_set():
    """asset_type='crypto' vs 'stock' should give SAME free balance for one strategy."""
    crypto_result = _patched_balance('conservative', asset_type='crypto', locked=300.0)
    stock_result = _patched_balance('conservative', asset_type='stock', locked=300.0)
    assert crypto_result['USDT'] == stock_result['USDT']
    assert crypto_result['total_usd'] == stock_result['total_usd']


# --- legacy mode: no strategy → asset_type still filters ---

def test_legacy_no_strategy_filters_by_asset_type():
    """When trading_strategy is None, asset_type filter still applied (legacy)."""
    legacy_cfg = {'settings': {
        'paper_trading_initial_capital': 10000.0,
    }}
    result = _patched_balance(None, asset_type='crypto', locked=200.0, cfg=legacy_cfg)
    assert result['total_usd'] == 10000.0
    assert result['USDT'] == 9800.0
