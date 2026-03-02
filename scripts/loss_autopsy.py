"""Loss Autopsy — categorizes losing trades and identifies actionable patterns.

Focuses on MFE/MAE analysis to find:
  - "SL too tight" (MFE > X% but SL hit first)
  - "Bad entry" (never profitable — MAE close to SL)
  - "Regime shift" (entered in one regime, exited in different conditions)
  - "Slow bleed" (gradual decline, long hold times)

Usage:
    .venv/bin/python scripts/loss_autopsy.py
"""
import argparse
import logging
import sys
import os
from datetime import timedelta

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.analysis.backtest import Backtester, DataLoader
from src.config import app_config
from src.logger import log

# Suppress verbose backtest debug/info logging (must be after imports)
logging.getLogger("CryptoBotLogger").setLevel(logging.WARNING)


def _get_default_params():
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


def _classify_loss(trade, sl_pct, tp_pct, trailing_act):
    """Classify a losing trade into a category.

    Categories:
    - sl_too_tight: Trade was profitable (MFE > 1%) but SL hit
    - trailing_missed: Trade reached trailing activation but trailing stop
      didn't lock in profit (MFE >= activation but exited at loss)
    - bad_entry: Trade was never significantly profitable (MFE < 0.5%)
    - slow_bleed: Long hold time (>48h) with gradual decline
    - normal_loss: Standard stop-loss exit within expectations
    """
    mfe = trade.get('mfe', 0)
    mae = trade.get('mae', 0)
    exit_reason = trade.get('exit_reason', 'unknown')

    # Trade reached TP-level MFE but still lost
    if mfe >= tp_pct:
        return 'sl_too_tight_extreme'

    # Trade was profitable (>1% MFE) but SL hit
    if mfe > 0.01 and exit_reason == 'stop_loss':
        return 'sl_too_tight'

    # Reached trailing activation but didn't close in profit
    if mfe >= trailing_act and trade['pnl'] <= 0:
        return 'trailing_missed'

    # Never profitable — bad entry
    if mfe < 0.005:
        return 'bad_entry'

    # Long hold time with slow decline
    try:
        hold_hours = (trade['exit_time'] - trade['entry_time']).total_seconds() / 3600
        if hold_hours > 48:
            return 'slow_bleed'
    except (TypeError, AttributeError):
        pass

    return 'normal_loss'


def _print_section(title):
    print(f"\n{'='*80}")
    print(f"  {title}")
    print(f"{'='*80}")


def run_autopsy():
    """Run backtest and produce loss autopsy analysis."""
    _print_section("LOSS AUTOPSY — Categorizing Losing Trades")

    prices_df = DataLoader.load_historical_data()
    if prices_df.empty:
        print("No historical data available.")
        return

    params = _get_default_params()
    watchlist = prices_df['symbol'].unique().tolist()
    bt = Backtester(watchlist, prices_df, params)
    bt.precompute_signals_parallel(n_workers=4)
    bt.run()
    trades = bt.portfolio.trade_history

    if not trades:
        print("No trades generated.")
        return

    losses = [t for t in trades if t['pnl'] <= 0]
    wins = [t for t in trades if t['pnl'] > 0]

    print(f"\nTotal trades: {len(trades)} | Wins: {len(wins)} | Losses: {len(losses)}")
    total_loss_pnl = sum(t['pnl'] for t in losses)
    total_win_pnl = sum(t['pnl'] for t in wins)
    print(f"Total winning PnL: ${total_win_pnl:,.2f}")
    print(f"Total losing PnL:  ${total_loss_pnl:,.2f}")
    print(f"Net PnL:           ${total_win_pnl + total_loss_pnl:,.2f}")

    if not losses:
        print("No losing trades to analyze!")
        return

    # --- Classify each loss ---
    categories = {}
    for t in losses:
        cat = _classify_loss(t, params.stop_loss_percentage,
                             params.take_profit_percentage,
                             params.trailing_stop_activation)
        categories.setdefault(cat, []).append(t)

    _print_section("LOSS CATEGORIES")

    cat_descriptions = {
        'sl_too_tight_extreme': 'SL too tight (MFE reached TP level!)',
        'sl_too_tight': 'SL too tight (MFE > 1% before SL hit)',
        'trailing_missed': 'Trailing missed (reached activation but lost)',
        'bad_entry': 'Bad entry (never profitable, MFE < 0.5%)',
        'slow_bleed': 'Slow bleed (held >48h, gradual decline)',
        'normal_loss': 'Normal loss (standard SL exit)',
    }

    cat_order = ['sl_too_tight_extreme', 'sl_too_tight', 'trailing_missed',
                 'bad_entry', 'slow_bleed', 'normal_loss']

    for cat in cat_order:
        cat_trades = categories.get(cat, [])
        if not cat_trades:
            continue
        n = len(cat_trades)
        pct = n / len(losses) * 100
        total = sum(t['pnl'] for t in cat_trades)
        avg = total / n
        avg_mfe = sum(t.get('mfe', 0) for t in cat_trades) / n * 100
        avg_mae = sum(t.get('mae', 0) for t in cat_trades) / n * 100
        desc = cat_descriptions.get(cat, cat)
        print(f"\n  {desc}")
        print(f"  Count: {n} ({pct:.0f}% of losses) | Total PnL: ${total:,.2f} | Avg: ${avg:.2f}")
        print(f"  Avg MFE: {avg_mfe:+.2f}% | Avg MAE: {avg_mae:.2f}%")

    # --- MFE Distribution for all losses ---
    _print_section("MFE DISTRIBUTION (Losing Trades)")
    print("How profitable were losing trades before they turned?")
    mfe_bins = [
        ('MFE = 0%  (never green)', lambda t: t.get('mfe', 0) <= 0.001),
        ('MFE 0-0.5%', lambda t: 0.001 < t.get('mfe', 0) <= 0.005),
        ('MFE 0.5-1%', lambda t: 0.005 < t.get('mfe', 0) <= 0.01),
        ('MFE 1-2%', lambda t: 0.01 < t.get('mfe', 0) <= 0.02),
        ('MFE 2-3.5% (near SL)', lambda t: 0.02 < t.get('mfe', 0) <= 0.035),
        ('MFE 3.5-5%', lambda t: 0.035 < t.get('mfe', 0) <= 0.05),
        ('MFE >5% (reached TP zone)', lambda t: t.get('mfe', 0) > 0.05),
    ]
    for label, cond in mfe_bins:
        matched = [t for t in losses if cond(t)]
        if matched:
            total = sum(t['pnl'] for t in matched)
            print(f"  {label:<30} {len(matched):>4} trades  ${total:>8.2f} lost")

    # --- MAE Distribution for winning trades ---
    _print_section("MAE DISTRIBUTION (Winning Trades)")
    print("How far did winning trades dip before recovering?")
    mae_bins = [
        ('MAE = 0%  (never red)', lambda t: t.get('mae', 0) >= -0.001),
        ('MAE 0-0.5%', lambda t: -0.005 <= t.get('mae', 0) < -0.001),
        ('MAE 0.5-1%', lambda t: -0.01 <= t.get('mae', 0) < -0.005),
        ('MAE 1-2%', lambda t: -0.02 <= t.get('mae', 0) < -0.01),
        ('MAE 2-3%', lambda t: -0.03 <= t.get('mae', 0) < -0.02),
        ('MAE >3% (nearly hit SL)', lambda t: t.get('mae', 0) < -0.03),
    ]
    for label, cond in mae_bins:
        matched = [t for t in wins if cond(t)]
        if matched:
            total = sum(t['pnl'] for t in matched)
            print(f"  {label:<30} {len(matched):>4} trades  ${total:>8.2f} won")

    # --- Hold time analysis ---
    _print_section("HOLD TIME ANALYSIS")
    hold_times = []
    for t in trades:
        try:
            hours = (t['exit_time'] - t['entry_time']).total_seconds() / 3600
            hold_times.append((t, hours))
        except (TypeError, AttributeError):
            pass

    if hold_times:
        win_holds = [h for t, h in hold_times if t['pnl'] > 0]
        loss_holds = [h for t, h in hold_times if t['pnl'] <= 0]
        if win_holds:
            print(f"  Winning trades avg hold: {sum(win_holds)/len(win_holds):.1f}h "
                  f"(median: {sorted(win_holds)[len(win_holds)//2]:.1f}h)")
        if loss_holds:
            print(f"  Losing trades avg hold:  {sum(loss_holds)/len(loss_holds):.1f}h "
                  f"(median: {sorted(loss_holds)[len(loss_holds)//2]:.1f}h)")

        # Bucket by hold time
        time_buckets = [
            ('<6h', 0, 6), ('6-12h', 6, 12), ('12-24h', 12, 24),
            ('24-48h', 24, 48), ('48-96h', 48, 96), ('>96h', 96, 10000),
        ]
        print(f"\n  {'Bucket':<10} {'Trades':>7} {'Wins':>5} {'WR%':>6} {'AvgPnL':>9}")
        print(f"  {'-'*40}")
        for label, lo, hi in time_buckets:
            bucket = [t for t, h in hold_times if lo <= h < hi]
            if bucket:
                bwins = [t for t in bucket if t['pnl'] > 0]
                wr = len(bwins) / len(bucket) * 100
                avg = sum(t['pnl'] for t in bucket) / len(bucket)
                print(f"  {label:<10} {len(bucket):>7} {len(bwins):>5} {wr:>5.1f}% ${avg:>8.2f}")

    # --- Worst individual trades ---
    _print_section("WORST 10 TRADES")
    worst = sorted(losses, key=lambda t: t['pnl'])[:10]
    print(f"  {'Symbol':<12} {'PnL':>9} {'Entry':>10} {'Exit':>10} {'MFE%':>6} {'MAE%':>7} {'Exit Reason':<15} {'RSI':>5} {'SMA':<6} {'Regime':<10}")
    print(f"  {'-'*100}")
    for t in worst:
        rsi = f"{t.get('rsi_at_entry', 0):.0f}" if t.get('rsi_at_entry') else '?'
        sma = t.get('sma_alignment', '?')
        regime = t.get('regime', '?')
        mfe = t.get('mfe', 0) * 100
        mae = t.get('mae', 0) * 100
        print(f"  {t['symbol']:<12} ${t['pnl']:>8.2f} ${t['entry_price']:>9.2f} "
              f"${t['exit_price']:>9.2f} {mfe:>5.1f}% {mae:>6.1f}% "
              f"{t.get('exit_reason', '?'):<15} {rsi:>5} {sma:<6} {regime:<10}")

    # --- Actionable recommendations ---
    _print_section("ACTIONABLE RECOMMENDATIONS")

    # 1. SL too tight
    sl_tight = categories.get('sl_too_tight', []) + categories.get('sl_too_tight_extreme', [])
    if sl_tight:
        pct = len(sl_tight) / len(losses) * 100
        recoverable = sum(t.get('mfe', 0) * t.get('effective_risk', 0.03) * params.initial_capital
                          for t in sl_tight)
        print(f"\n  1. STOP-LOSS TOO TIGHT ({pct:.0f}% of losses)")
        print(f"     {len(sl_tight)} trades were profitable before SL hit.")
        avg_mfe = sum(t.get('mfe', 0) for t in sl_tight) / len(sl_tight) * 100
        print(f"     Avg MFE before reversal: {avg_mfe:.1f}%")
        print(f"     → Consider widening SL or lowering trailing activation")

    # 2. Bad entries
    bad = categories.get('bad_entry', [])
    if bad:
        pct = len(bad) / len(losses) * 100
        print(f"\n  2. BAD ENTRIES ({pct:.0f}% of losses)")
        print(f"     {len(bad)} trades were never profitable (MFE < 0.5%).")
        # Check RSI distribution of bad entries
        rsi_vals = [t.get('rsi_at_entry', 50) for t in bad if t.get('rsi_at_entry')]
        if rsi_vals:
            avg_rsi = sum(rsi_vals) / len(rsi_vals)
            print(f"     Avg RSI at entry: {avg_rsi:.0f}")
            high_rsi = [r for r in rsi_vals if r > 60]
            print(f"     {len(high_rsi)}/{len(rsi_vals)} ({len(high_rsi)/len(rsi_vals)*100:.0f}%) "
                  f"had RSI > 60 → buying into overbought conditions")

    # 3. Trailing missed
    trail_miss = categories.get('trailing_missed', [])
    if trail_miss:
        pct = len(trail_miss) / len(losses) * 100
        avg_mfe = sum(t.get('mfe', 0) for t in trail_miss) / len(trail_miss) * 100
        print(f"\n  3. TRAILING STOP MISSED ({pct:.0f}% of losses)")
        print(f"     {len(trail_miss)} trades reached {params.trailing_stop_activation*100:.0f}% "
              f"profit but still lost.")
        print(f"     Avg MFE: {avg_mfe:.1f}%")
        print(f"     → Consider tighter trailing distance or earlier activation")

    # 4. Slow bleeds
    slow = categories.get('slow_bleed', [])
    if slow:
        pct = len(slow) / len(losses) * 100
        avg_hold = sum((t['exit_time'] - t['entry_time']).total_seconds() / 3600
                       for t in slow) / len(slow)
        print(f"\n  4. SLOW BLEEDS ({pct:.0f}% of losses)")
        print(f"     {len(slow)} trades held for avg {avg_hold:.0f}h before SL.")
        print(f"     → Consider time-based exit or tighter SL after 24h")

    print()


if __name__ == '__main__':
    run_autopsy()
