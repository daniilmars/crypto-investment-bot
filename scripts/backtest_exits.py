#!/usr/bin/env python3
"""
Exit Parameter Sweep — replays historical trades with different SL/TP/trailing settings.

Uses only saved DB data (no API calls). Answers: "Would different exit parameters
have produced better results?"

Usage:
    .venv/bin/python scripts/backtest_exits.py
    .venv/bin/python scripts/backtest_exits.py --strategy auto --asset crypto
    .venv/bin/python scripts/backtest_exits.py --quality-only
    .venv/bin/python scripts/backtest_exits.py --mfe-mae
"""

import argparse
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.analysis.trade_replay import (
    run_exit_sweep, analyze_signal_quality, analyze_mfe_mae,
    format_sweep_report, format_quality_report,
)


def main():
    parser = argparse.ArgumentParser(description="Backtest exit parameters on historical trades.")
    parser.add_argument('--strategy', type=str, default=None,
                        help="Filter by trading_strategy: 'manual', 'auto', or None for all")
    parser.add_argument('--asset', type=str, default=None,
                        help="Filter by asset_type: 'crypto', 'stock', or None for all")
    parser.add_argument('--quality-only', action='store_true',
                        help="Run only signal quality analysis (skip parameter sweep)")
    parser.add_argument('--mfe-mae', action='store_true',
                        help="Run MFE/MAE analysis (how much profit is left on the table)")
    parser.add_argument('--compact-grid', action='store_true',
                        help="Use a smaller parameter grid for faster sweeps")

    args = parser.parse_args()

    # --- Signal Quality Analysis ---
    print("=" * 60)
    print("SIGNAL QUALITY ANALYSIS")
    print("=" * 60)
    quality = analyze_signal_quality(trading_strategy=args.strategy)
    print(format_quality_report(quality))
    print()

    if args.quality_only:
        return

    # --- MFE/MAE Analysis ---
    if args.mfe_mae:
        print("=" * 60)
        print("MFE/MAE ANALYSIS")
        print("=" * 60)
        mfe_data = analyze_mfe_mae(trading_strategy=args.strategy, asset_type=args.asset)
        print(f"Trades analyzed: {mfe_data.get('trade_count', 0)}\n")
        for reason, data in mfe_data.get('by_exit_reason', {}).items():
            print(f"  {reason} ({data['count']} trades):")
            print(f"    Avg MFE: {data['avg_mfe_pct']:+.2f}%  (median: {data['median_mfe_pct']:+.2f}%)")
            print(f"    Avg MAE: {data['avg_mae_pct']:+.2f}%  (median: {data['median_mae_pct']:+.2f}%)")
            print(f"    Was profitable before exit: {data['was_profitable_count']}/{data['count']}")
            print(f"    Avg PnL: ${data['avg_pnl']:.2f}")
        print()

    # --- Exit Parameter Sweep ---
    print("=" * 60)
    print("EXIT PARAMETER SWEEP")
    print("=" * 60)

    grid = None
    if args.compact_grid:
        grid = {
            'stop_loss_pct': [0.025, 0.035, 0.05],
            'take_profit_pct': [0.06, 0.08, 0.12],
            'trailing_activation': [0.02, 0.03],
            'trailing_distance': [0.012, 0.015],
        }

    sweep = run_exit_sweep(
        param_grid=grid,
        trading_strategy=args.strategy,
        asset_type=args.asset,
    )
    print(format_sweep_report(sweep))


if __name__ == '__main__':
    main()
