#!/usr/bin/env python3
"""
Parallel backtest comparison: OLD vs NEW signal quality settings.
Uses multiprocessing to run both configs simultaneously.
"""
import multiprocessing as mp
import sys
import os
import time
import logging

# Suppress all logging to avoid I/O bottleneck
logging.disable(logging.CRITICAL)

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.analysis.backtest import DataLoader, Backtester
from argparse import Namespace


def make_params(signal_threshold, volume_gate_enabled, volume_gate_period,
                stoploss_cooldown_bars, label=""):
    """Build a params Namespace matching CLI args."""
    return Namespace(
        initial_capital=10000.0,
        trade_risk_percentage=0.03,
        stop_loss_percentage=0.03,
        take_profit_percentage=0.08,
        max_concurrent_positions=5,
        slippage_bps=5,
        trailing_stop_enabled=True,
        trailing_stop_activation=0.02,
        trailing_stop_distance=0.015,
        sma_period=20,
        rsi_period=14,
        rsi_overbought_threshold=70,
        rsi_oversold_threshold=30,
        stablecoin_inflow_threshold_usd=100000000,
        transaction_velocity_baseline_hours=24,
        transaction_velocity_threshold_multiplier=5.0,
        high_interest_wallets=["Grayscale", "US Government"],
        stablecoins_to_monitor=["usdt", "usdc", "busd", "dai", "tusd", "fdusd", "pyusd"],
        bar_interval_minutes=60,
        signal_threshold=signal_threshold,
        volume_gate_enabled=volume_gate_enabled,
        volume_gate_period=volume_gate_period,
        stoploss_cooldown_bars=stoploss_cooldown_bars,
        _label=label,
    )


def run_backtest(args):
    """Run a single backtest config. Returns (label, results_dict)."""
    params, prices_df, whales_df = args
    label = params._label

    # Suppress logging in worker
    logging.disable(logging.CRITICAL)

    watchlist = prices_df['symbol'].unique().tolist()
    bt = Backtester(watchlist, prices_df.copy(), whales_df.copy(), params)
    results = bt.run()
    return label, results


def main():
    # Load data once in the main process
    print("Loading historical data...")
    t0 = time.time()
    prices_df, whales_df = DataLoader.load_historical_data()
    print(f"Loaded {len(prices_df)} price records, {len(whales_df)} whale txns in {time.time()-t0:.1f}s")

    if prices_df.empty:
        print("No data found. Exiting.")
        return

    configs = [
        make_params(signal_threshold=2, volume_gate_enabled=False,
                    volume_gate_period=20, stoploss_cooldown_bars=0,
                    label="OLD (threshold=2, no gates)"),
        make_params(signal_threshold=3, volume_gate_enabled=False,
                    volume_gate_period=20, stoploss_cooldown_bars=0,
                    label="NEW-threshold-only (threshold=3)"),
        make_params(signal_threshold=3, volume_gate_enabled=True,
                    volume_gate_period=20, stoploss_cooldown_bars=0,
                    label="NEW-threshold+volgate"),
        make_params(signal_threshold=3, volume_gate_enabled=True,
                    volume_gate_period=20, stoploss_cooldown_bars=6,
                    label="NEW-full (threshold=3, volgate=20, cooldown=6)"),
    ]

    work = [(cfg, prices_df, whales_df) for cfg in configs]

    n_workers = min(len(configs), mp.cpu_count())
    print(f"\nRunning {len(configs)} configs on {n_workers} cores...\n")
    t1 = time.time()

    with mp.Pool(n_workers) as pool:
        results = pool.map(run_backtest, work)

    elapsed = time.time() - t1
    print(f"All backtests complete in {elapsed:.1f}s\n")

    # Print comparison table
    header = f"{'Config':<45} {'PnL':>10} {'Return%':>9} {'Trades':>7} {'WinRate':>8} {'Sharpe':>8} {'Sortino':>9} {'MaxDD%':>8} {'PF':>7} {'AvgWin':>8} {'AvgLoss':>8}"
    print(header)
    print("-" * len(header))

    for label, r in results:
        print(f"{label:<45} "
              f"${r['total_pnl']:>8.2f} "
              f"{r['total_return_pct']:>8.2f}% "
              f"{r['total_trades']:>6} "
              f"{r['win_rate']:>7.1f}% "
              f"{r.get('sharpe_ratio') or 0:>7.3f} "
              f"{r.get('sortino_ratio') or 0:>8.3f} "
              f"{r['max_drawdown_pct']:>7.2f}% "
              f"{r['profit_factor']:>6.3f} "
              f"${r['avg_win']:>6.2f} "
              f"${r['avg_loss']:>6.2f}")


if __name__ == '__main__':
    main()
