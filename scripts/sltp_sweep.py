#!/usr/bin/env python3
"""
SL/TP parameter sweep: find optimal stop-loss and take-profit percentages.
Uses multiprocessing to run all combos in parallel.
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


def make_params(sl_pct, tp_pct, label=""):
    """Build a params Namespace with given SL/TP."""
    return Namespace(
        initial_capital=10000.0,
        trade_risk_percentage=0.03,
        stop_loss_percentage=sl_pct,
        take_profit_percentage=tp_pct,
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
        signal_threshold=2,
        volume_gate_enabled=False,
        volume_gate_period=20,
        stoploss_cooldown_bars=0,
        _label=label,
    )


def run_backtest(args):
    """Run a single backtest config. Returns (label, sl, tp, results_dict)."""
    params, prices_df, whales_df = args
    label = params._label

    logging.disable(logging.CRITICAL)

    watchlist = prices_df['symbol'].unique().tolist()
    bt = Backtester(watchlist, prices_df.copy(), whales_df.copy(), params)
    results = bt.run()
    return label, params.stop_loss_percentage, params.take_profit_percentage, results


def main():
    print("Loading historical data...")
    t0 = time.time()
    prices_df, whales_df = DataLoader.load_historical_data()
    print(f"Loaded {len(prices_df)} price records, {len(whales_df)} whale txns in {time.time()-t0:.1f}s")

    if prices_df.empty:
        print("No data found. Exiting.")
        return

    # SL: 1.5% to 5% in 0.5% steps
    # TP: 4% to 12% in 2% steps
    sl_values = [0.015, 0.02, 0.025, 0.03, 0.035, 0.04, 0.05]
    tp_values = [0.04, 0.06, 0.08, 0.10, 0.12]

    configs = []
    for sl in sl_values:
        for tp in tp_values:
            label = f"SL={sl:.1%} TP={tp:.1%}"
            configs.append(make_params(sl, tp, label=label))

    work = [(cfg, prices_df, whales_df) for cfg in configs]

    n_workers = mp.cpu_count()
    print(f"\nRunning {len(configs)} SL/TP combos on {n_workers} cores...\n")
    t1 = time.time()

    with mp.Pool(n_workers) as pool:
        results = pool.map(run_backtest, work)

    elapsed = time.time() - t1
    print(f"All {len(configs)} backtests complete in {elapsed:.1f}s\n")

    # Sort by Sharpe ratio descending
    results.sort(key=lambda x: x[3].get('sharpe_ratio') or 0, reverse=True)

    # Print table
    header = (f"{'Config':<20} {'PnL':>10} {'Return%':>9} {'Trades':>7} "
              f"{'WinRate':>8} {'Sharpe':>8} {'Sortino':>9} {'MaxDD%':>8} "
              f"{'PF':>7} {'AvgWin':>8} {'AvgLoss':>8}")
    print(header)
    print("-" * len(header))

    for label, sl, tp, r in results:
        print(f"{label:<20} "
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

    # Print heatmap-style grid: Return% by SL x TP
    print(f"\n\n{'='*60}")
    print("RETURN % HEATMAP (SL rows x TP columns)")
    print(f"{'='*60}")

    # Build lookup
    lookup = {}
    for label, sl, tp, r in results:
        lookup[(sl, tp)] = r

    # Header row
    print(f"{'SL \\ TP':>10}", end="")
    for tp in tp_values:
        print(f" {tp:>8.1%}", end="")
    print()
    print("-" * (10 + 9 * len(tp_values)))

    for sl in sl_values:
        print(f"{sl:>10.1%}", end="")
        for tp in tp_values:
            r = lookup.get((sl, tp))
            if r:
                ret = r['total_return_pct']
                print(f" {ret:>8.2f}%", end="")
            else:
                print(f" {'N/A':>8}", end="")
        print()

    # Sharpe heatmap
    print(f"\n\n{'='*60}")
    print("SHARPE RATIO HEATMAP (SL rows x TP columns)")
    print(f"{'='*60}")

    print(f"{'SL \\ TP':>10}", end="")
    for tp in tp_values:
        print(f" {tp:>8.1%}", end="")
    print()
    print("-" * (10 + 9 * len(tp_values)))

    for sl in sl_values:
        print(f"{sl:>10.1%}", end="")
        for tp in tp_values:
            r = lookup.get((sl, tp))
            if r:
                sharpe = r.get('sharpe_ratio') or 0
                print(f" {sharpe:>8.3f}", end="")
            else:
                print(f" {'N/A':>8}", end="")
        print()


if __name__ == '__main__':
    main()
