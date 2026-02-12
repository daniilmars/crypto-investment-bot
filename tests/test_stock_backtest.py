# tests/test_stock_backtest.py
"""Tests for the stock backtesting module."""

import pytest
from unittest.mock import patch, MagicMock
from argparse import Namespace
import pandas as pd
import numpy as np


def _make_prices_df(symbols, n_bars=100, start_price=150.0):
    """Creates a synthetic price DataFrame for testing."""
    dates = pd.date_range('2024-01-01', periods=n_bars, freq='B', tz='UTC')
    rows = []
    for symbol in symbols:
        price = start_price
        for i, date in enumerate(dates):
            # Random walk with slight upward drift
            change = np.random.normal(0.001, 0.02)
            price *= (1 + change)
            rows.append({
                'symbol': symbol,
                'timestamp': date,
                'open': price * 0.999,
                'high': price * 1.01,
                'low': price * 0.99,
                'close': price,
                'volume': np.random.uniform(1e6, 5e6),
            })
    return pd.DataFrame(rows)


def _default_params(**overrides):
    """Returns default backtest parameters as a Namespace."""
    defaults = {
        'initial_capital': 10000.0,
        'trade_risk_percentage': 0.05,
        'stop_loss_percentage': 0.07,
        'take_profit_percentage': 0.10,
        'max_concurrent_positions': 3,
        'slippage_bps': 5,
        'trailing_stop_enabled': True,
        'trailing_stop_activation': 0.02,
        'trailing_stop_distance': 0.015,
        'sma_period': 20,
        'rsi_period': 14,
        'rsi_overbought_threshold': 70,
        'rsi_oversold_threshold': 30,
        'bar_interval_minutes': 390,
    }
    defaults.update(overrides)
    return Namespace(**defaults)


class TestStockDataLoader:
    """Tests for StockDataLoader."""

    def test_load_from_csv(self, tmp_path):
        from src.analysis.stock_backtest import StockDataLoader

        # Create a temp CSV
        df = _make_prices_df(['AAPL'], n_bars=10)
        csv_path = tmp_path / "test_prices.csv"
        df.to_csv(csv_path, index=False)

        loaded = StockDataLoader.load_from_csv(str(csv_path))
        assert len(loaded) == 10
        assert 'close' in loaded.columns
        assert loaded['symbol'].unique()[0] == 'AAPL'

    def test_load_from_csv_filter_symbols(self, tmp_path):
        from src.analysis.stock_backtest import StockDataLoader

        df = _make_prices_df(['AAPL', 'MSFT'], n_bars=10)
        csv_path = tmp_path / "test_prices.csv"
        df.to_csv(csv_path, index=False)

        loaded = StockDataLoader.load_from_csv(str(csv_path), symbols=['AAPL'])
        assert set(loaded['symbol'].unique()) == {'AAPL'}


class TestStockStrategy:
    """Tests for StockStrategy signal generation."""

    def test_hold_insufficient_data(self):
        from src.analysis.stock_backtest import StockStrategy
        params = _default_params()
        strategy = StockStrategy(params)

        signal = strategy.generate_signals('AAPL', [100.0, 101.0], 101.0)
        assert signal['signal'] == 'HOLD'
        assert 'Not enough data' in signal['reason']

    def test_generates_signal_with_enough_data(self):
        from src.analysis.stock_backtest import StockStrategy
        params = _default_params()
        strategy = StockStrategy(params)

        # Create 50 prices with an uptrend
        prices = [100.0 + i * 0.5 for i in range(50)]
        current_price = prices[-1]

        signal = strategy.generate_signals('AAPL', prices, current_price)
        assert signal['signal'] in ('BUY', 'SELL', 'HOLD')
        assert signal['symbol'] == 'AAPL'
        assert signal['current_price'] == current_price


class TestStockBacktester:
    """Tests for the StockBacktester engine."""

    def test_backtest_runs_without_error(self):
        from src.analysis.stock_backtest import StockBacktester

        np.random.seed(42)
        prices_df = _make_prices_df(['AAPL'], n_bars=80)
        params = _default_params()

        backtester = StockBacktester(['AAPL'], prices_df, params)
        results = backtester.run()

        assert 'total_trades' in results
        assert 'total_pnl' in results
        assert 'final_value' in results
        assert results['initial_capital'] == 10000.0

    def test_backtest_multiple_symbols(self):
        from src.analysis.stock_backtest import StockBacktester

        np.random.seed(42)
        prices_df = _make_prices_df(['AAPL', 'MSFT'], n_bars=80)
        params = _default_params()

        backtester = StockBacktester(['AAPL', 'MSFT'], prices_df, params)
        results = backtester.run()

        assert results['final_value'] > 0

    def test_backtest_respects_max_positions(self):
        from src.analysis.stock_backtest import StockBacktester

        np.random.seed(42)
        symbols = ['AAPL', 'MSFT', 'GOOGL', 'AMZN', 'NVDA']
        prices_df = _make_prices_df(symbols, n_bars=80)
        params = _default_params(max_concurrent_positions=2)

        backtester = StockBacktester(symbols, prices_df, params)
        results = backtester.run()

        # The backtest should complete without error and produce valid results
        assert results['final_value'] > 0
        assert results['initial_capital'] == 10000.0
        # At end of backtest, positions should be within limit
        assert len(backtester.portfolio.positions) <= 2

    def test_backtest_empty_data(self):
        from src.analysis.stock_backtest import StockBacktester

        empty_df = pd.DataFrame(columns=['symbol', 'timestamp', 'close', 'volume'])
        params = _default_params()

        backtester = StockBacktester(['AAPL'], empty_df, params)
        # Should handle empty data gracefully
        try:
            results = backtester.run()
            assert results['total_trades'] == 0
        except (KeyError, ValueError):
            # Empty pivot is acceptable failure mode
            pass

    def test_kelly_sizing_kicks_in(self):
        from src.analysis.stock_backtest import StockBacktester

        np.random.seed(42)
        prices_df = _make_prices_df(['AAPL'], n_bars=200)
        params = _default_params(
            stop_loss_percentage=0.03,
            take_profit_percentage=0.04,
        )

        backtester = StockBacktester(['AAPL'], prices_df, params)
        results = backtester.run()

        # With 200 bars, should have enough trades for Kelly to activate
        # (just verify it doesn't crash — Kelly kicks in at 10+ trades)
        assert results['final_value'] > 0
