"""Signal Scorecard — connects signals to trade outcomes.

Runs a backtest with extended metadata and breaks down win rate / PnL
by entry condition buckets:
  - RSI zone at entry (oversold / neutral / overbought)
  - SMA alignment (above / below)
  - Market regime (trending / ranging / volatile)
  - Exit reason (stop_loss / take_profit / trailing_stop)
  - Symbol
  - Day of week / hour of day

Usage:
    .venv/bin/python scripts/signal_scorecard.py
    .venv/bin/python scripts/signal_scorecard.py --top-n 10
"""
import argparse
import logging
import sys
import os

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.analysis.backtest import Backtester, DataLoader, run_walk_forward
from src.config import app_config
from src.logger import log

# Suppress verbose backtest debug/info logging (must be after imports)
logging.getLogger("CryptoBotLogger").setLevel(logging.WARNING)


def _get_default_params():
    """Build params namespace from config, matching backtest.py CLI defaults."""
    s = app_config.get('settings', {})
    from argparse import Namespace
    return Namespace(
        initial_capital=s.get('paper_trading_initial_capital', 10000.0),
        trade_risk_percentage=s.get('trade_risk_percentage', 0.01),
        stop_loss_percentage=s.get('stop_loss_percentage', 0.02),
        take_profit_percentage=s.get('take_profit_percentage', 0.05),
        max_concurrent_positions=s.get('max_concurrent_positions', 3),
        signal_threshold=s.get('signal_threshold', 3),
        volume_gate_enabled=s.get('volume_gate_enabled', True),
        volume_gate_period=s.get('volume_gate_period', 20),
        stoploss_cooldown_bars=s.get('stoploss_cooldown_hours', 6),
        slippage_bps=5,
        trailing_stop_enabled=True,
        trailing_stop_activation=s.get('trailing_stop', {}).get('activation_percentage', 0.02),
        trailing_stop_distance=s.get('trailing_stop', {}).get('distance_percentage', 0.015),
        sma_period=s.get('sma_period', 20),
        rsi_period=s.get('rsi_period', 14),
        rsi_overbought_threshold=s.get('rsi_overbought_threshold', 70),
        rsi_oversold_threshold=s.get('rsi_oversold_threshold', 30),
        bar_interval_minutes=60,
        signal_mode='scoring',
        sentiment_config=None,
    )


def _rsi_zone(rsi, oversold=30, overbought=70):
    if rsi is None:
        return 'unknown'
    if rsi < oversold:
        return 'oversold'
    elif rsi > overbought:
        return 'overbought'
    return 'neutral'


def _rsi_bucket(rsi):
    """Finer RSI buckets for deeper analysis."""
    if rsi is None:
        return 'unknown'
    if rsi < 30:
        return '<30'
    elif rsi < 40:
        return '30-40'
    elif rsi < 50:
        return '40-50'
    elif rsi < 60:
        return '50-60'
    elif rsi < 70:
        return '60-70'
    return '70+'


def _analyze_bucket(trades, label):
    """Compute stats for a bucket of trades."""
    if not trades:
        return None
    n = len(trades)
    wins = [t for t in trades if t['pnl'] > 0]
    losses = [t for t in trades if t['pnl'] <= 0]
    total_pnl = sum(t['pnl'] for t in trades)
    avg_pnl = total_pnl / n
    win_rate = len(wins) / n * 100
    avg_win = sum(t['pnl'] for t in wins) / len(wins) if wins else 0
    avg_loss = abs(sum(t['pnl'] for t in losses) / len(losses)) if losses else 0
    avg_mfe = sum(t.get('mfe', 0) for t in trades) / n * 100
    avg_mae = sum(t.get('mae', 0) for t in trades) / n * 100
    pf = sum(t['pnl'] for t in wins) / abs(sum(t['pnl'] for t in losses)) if losses else float('inf')
    return {
        'label': label,
        'trades': n,
        'win_rate': round(win_rate, 1),
        'total_pnl': round(total_pnl, 2),
        'avg_pnl': round(avg_pnl, 2),
        'avg_win': round(avg_win, 2),
        'avg_loss': round(avg_loss, 2),
        'profit_factor': round(pf, 2) if pf != float('inf') else 'inf',
        'avg_mfe_pct': round(avg_mfe, 2),
        'avg_mae_pct': round(avg_mae, 2),
    }


def _print_bucket_table(title, rows):
    """Print a formatted table of bucket statistics."""
    if not rows:
        return
    rows = [r for r in rows if r is not None]
    if not rows:
        return

    print(f"\n{'='*80}")
    print(f"  {title}")
    print(f"{'='*80}")
    header = f"{'Bucket':<18} {'Trades':>6} {'WR%':>6} {'TotalPnL':>10} {'AvgPnL':>8} {'AvgWin':>8} {'AvgLoss':>8} {'PF':>6} {'MFE%':>6} {'MAE%':>7}"
    print(header)
    print('-' * len(header))
    for r in sorted(rows, key=lambda x: -x['total_pnl']):
        pf_str = f"{r['profit_factor']:>6}" if isinstance(r['profit_factor'], float) else f"{'inf':>6}"
        print(f"{r['label']:<18} {r['trades']:>6} {r['win_rate']:>5.1f}% "
              f"${r['total_pnl']:>9.2f} ${r['avg_pnl']:>7.2f} "
              f"${r['avg_win']:>7.2f} ${r['avg_loss']:>7.2f} "
              f"{pf_str} {r['avg_mfe_pct']:>5.1f}% {r['avg_mae_pct']:>6.1f}%")


def run_scorecard(top_n=15):
    """Run backtest and produce signal scorecard analysis."""
    print("\n" + "=" * 80)
    print("  SIGNAL SCORECARD — Entry Condition → Trade Outcome Analysis")
    print("=" * 80)

    # Load data and run backtest
    prices_df = DataLoader.load_historical_data()
    if prices_df.empty:
        print("No historical data available.")
        return

    params = _get_default_params()
    watchlist = prices_df['symbol'].unique().tolist()
    bt = Backtester(watchlist, prices_df, params)
    bt.precompute_signals_parallel(n_workers=4)
    results = bt.run()
    trades = bt.portfolio.trade_history

    if not trades:
        print("No trades generated. Check data / parameters.")
        return

    # Overall summary
    wins = [t for t in trades if t['pnl'] > 0]
    losses = [t for t in trades if t['pnl'] <= 0]
    total_pnl = sum(t['pnl'] for t in trades)
    print(f"\nTotal trades: {len(trades)} | Wins: {len(wins)} | Losses: {len(losses)}")
    print(f"Win rate: {len(wins)/len(trades)*100:.1f}% | Total PnL: ${total_pnl:.2f}")
    print(f"Avg win: ${sum(t['pnl'] for t in wins)/len(wins):.2f}" if wins else "")
    print(f"Avg loss: ${abs(sum(t['pnl'] for t in losses)/len(losses)):.2f}" if losses else "")

    # --- Breakdown by RSI zone ---
    rsi_buckets = {}
    for t in trades:
        zone = _rsi_zone(t.get('rsi_at_entry'),
                         params.rsi_oversold_threshold,
                         params.rsi_overbought_threshold)
        rsi_buckets.setdefault(zone, []).append(t)
    rows = [_analyze_bucket(v, k) for k, v in rsi_buckets.items()]
    _print_bucket_table("BY RSI ZONE AT ENTRY", rows)

    # --- Finer RSI buckets ---
    rsi_fine = {}
    for t in trades:
        bucket = _rsi_bucket(t.get('rsi_at_entry'))
        rsi_fine.setdefault(bucket, []).append(t)
    rows = [_analyze_bucket(v, k) for k, v in rsi_fine.items()]
    _print_bucket_table("BY RSI BUCKET (fine-grained)", rows)

    # --- Breakdown by SMA alignment ---
    sma_buckets = {}
    for t in trades:
        align = t.get('sma_alignment', 'unknown')
        sma_buckets.setdefault(align, []).append(t)
    rows = [_analyze_bucket(v, k) for k, v in sma_buckets.items()]
    _print_bucket_table("BY SMA ALIGNMENT AT ENTRY", rows)

    # --- Breakdown by market regime ---
    regime_buckets = {}
    for t in trades:
        regime = t.get('regime', 'unknown')
        regime_buckets.setdefault(regime, []).append(t)
    rows = [_analyze_bucket(v, k) for k, v in regime_buckets.items()]
    _print_bucket_table("BY MARKET REGIME AT ENTRY", rows)

    # --- Breakdown by exit reason ---
    exit_buckets = {}
    for t in trades:
        reason = t.get('exit_reason', 'unknown')
        exit_buckets.setdefault(reason, []).append(t)
    rows = [_analyze_bucket(v, k) for k, v in exit_buckets.items()]
    _print_bucket_table("BY EXIT REASON", rows)

    # --- Breakdown by RSI + SMA combined ---
    combined = {}
    for t in trades:
        zone = _rsi_zone(t.get('rsi_at_entry'),
                         params.rsi_oversold_threshold,
                         params.rsi_overbought_threshold)
        align = t.get('sma_alignment', '?')
        key = f"{zone}+{align}"
        combined.setdefault(key, []).append(t)
    rows = [_analyze_bucket(v, k) for k, v in combined.items()]
    _print_bucket_table("BY RSI ZONE + SMA ALIGNMENT (combined)", rows)

    # --- Breakdown by symbol (top N) ---
    sym_buckets = {}
    for t in trades:
        sym_buckets.setdefault(t['symbol'], []).append(t)
    rows = [_analyze_bucket(v, k) for k, v in sym_buckets.items()]
    rows = [r for r in rows if r is not None]
    rows.sort(key=lambda x: -x['total_pnl'])
    _print_bucket_table(f"BY SYMBOL (top {top_n} by PnL)", rows[:top_n])

    # Bottom symbols
    _print_bucket_table(f"BY SYMBOL (bottom {top_n} by PnL)", rows[-top_n:])

    # --- Breakdown by side (LONG vs SHORT) ---
    side_buckets = {}
    for t in trades:
        side_buckets.setdefault(t['side'], []).append(t)
    rows = [_analyze_bucket(v, k) for k, v in side_buckets.items()]
    _print_bucket_table("BY SIDE (LONG vs SHORT)", rows)

    # --- Breakdown by day of week ---
    dow_buckets = {}
    dow_names = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun']
    for t in trades:
        try:
            dow = t['entry_time'].weekday()
            dow_buckets.setdefault(dow_names[dow], []).append(t)
        except (AttributeError, TypeError):
            pass
    rows = [_analyze_bucket(v, k) for k, v in dow_buckets.items()]
    _print_bucket_table("BY DAY OF WEEK (entry)", rows)

    # --- Key insights ---
    print(f"\n{'='*80}")
    print("  KEY INSIGHTS")
    print(f"{'='*80}")

    # Find best/worst RSI + SMA combos
    combo_stats = {}
    for t in trades:
        zone = _rsi_zone(t.get('rsi_at_entry'),
                         params.rsi_oversold_threshold,
                         params.rsi_overbought_threshold)
        align = t.get('sma_alignment', '?')
        regime = t.get('regime', '?')
        key = f"{zone}+{align}+{regime}"
        combo_stats.setdefault(key, []).append(t)

    combos = []
    for k, v in combo_stats.items():
        if len(v) >= 5:  # minimum sample
            wr = len([t for t in v if t['pnl'] > 0]) / len(v) * 100
            combos.append((k, len(v), wr, sum(t['pnl'] for t in v)))

    combos.sort(key=lambda x: -x[2])
    if combos:
        print("\nBest condition combos (RSI+SMA+Regime, min 5 trades):")
        for k, n, wr, pnl in combos[:5]:
            print(f"  {k:<35} trades={n:>3}  WR={wr:>5.1f}%  PnL=${pnl:>8.2f}")
        print("\nWorst condition combos:")
        for k, n, wr, pnl in combos[-5:]:
            print(f"  {k:<35} trades={n:>3}  WR={wr:>5.1f}%  PnL=${pnl:>8.2f}")

    # MFE/MAE insight for stop-loss trades
    sl_trades = [t for t in trades if t.get('exit_reason') == 'stop_loss']
    if sl_trades:
        sl_with_positive_mfe = [t for t in sl_trades if t.get('mfe', 0) > 0.01]
        pct = len(sl_with_positive_mfe) / len(sl_trades) * 100
        avg_mfe = sum(t.get('mfe', 0) for t in sl_with_positive_mfe) / len(sl_with_positive_mfe) * 100 if sl_with_positive_mfe else 0
        print(f"\nStop-loss trades: {len(sl_trades)}")
        print(f"  {pct:.0f}% had MFE > 1% before hitting SL (avg MFE: {avg_mfe:.1f}%)")
        print(f"  → These trades were profitable but the trailing stop didn't catch them")
        sl_high_mfe = [t for t in sl_trades if t.get('mfe', 0) >= params.take_profit_percentage]
        if sl_high_mfe:
            print(f"  {len(sl_high_mfe)} trades ({len(sl_high_mfe)/len(sl_trades)*100:.0f}%) "
                  f"had MFE >= TP threshold ({params.take_profit_percentage*100:.0f}%) "
                  f"but still hit SL!")

    print()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description="Signal Scorecard Analysis")
    parser.add_argument('--top-n', type=int, default=15,
                        help='Number of top/bottom symbols to show')
    args = parser.parse_args()
    run_scorecard(top_n=args.top_n)
