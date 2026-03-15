"""
Trade Replay Backtester — replays historical trades with alternative exit parameters.

Uses only DB data (no Gemini API calls). Loads closed trades and their price paths
from market_prices, then simulates exits with different SL/TP/trailing stop settings.

This answers: "Would different exit parameters have produced better results?"

Usage:
    from src.analysis.trade_replay import run_exit_sweep, analyze_signal_quality
    results = run_exit_sweep()      # parameter sweep
    quality = analyze_signal_quality()  # signal attribution analysis
"""

import itertools
import statistics
from dataclasses import dataclass, field

from src.database import (
    get_all_trades, get_price_history_for_trade, get_db_connection,
    release_db_connection, _cursor,
)
from src.logger import log

import pandas as pd
import psycopg2


# --- Configuration ---

FEE_RATE = 0.001  # 0.1% per trade (Binance spot default)

DEFAULT_PARAM_GRID = {
    'stop_loss_pct': [0.02, 0.025, 0.03, 0.035, 0.04, 0.05],
    'take_profit_pct': [0.04, 0.06, 0.08, 0.10, 0.12, 0.15],
    'trailing_activation': [0.015, 0.02, 0.025, 0.03],
    'trailing_distance': [0.01, 0.012, 0.015, 0.02],
}


# --- Data classes ---

@dataclass
class ExitParams:
    stop_loss_pct: float = 0.035
    take_profit_pct: float = 0.08
    trailing_enabled: bool = True
    trailing_activation: float = 0.02
    trailing_distance: float = 0.015


@dataclass
class ReplayResult:
    """Result of replaying a single trade with alternative exit params."""
    symbol: str = ''
    entry_price: float = 0.0
    actual_exit_price: float = 0.0
    actual_pnl: float = 0.0
    actual_exit_reason: str = ''
    replay_exit_price: float = 0.0
    replay_pnl: float = 0.0
    replay_exit_reason: str = ''
    replay_bars_held: int = 0
    price_path_length: int = 0
    max_favorable_excursion: float = 0.0  # best PnL% reached
    max_adverse_excursion: float = 0.0    # worst PnL% reached


# --- Core replay engine ---

def replay_trade(entry_price: float, quantity: float, prices: list[dict],
                 params: ExitParams) -> ReplayResult:
    """Replay a single trade through a price path with given exit parameters.

    Args:
        entry_price: The trade's entry price.
        quantity: Position size.
        prices: List of {'price': float, 'timestamp': ...} dicts, chronological.
        params: Exit parameters to test.

    Returns:
        ReplayResult with the simulated exit details.
    """
    result = ReplayResult(entry_price=entry_price, price_path_length=len(prices))
    if not prices:
        result.replay_exit_reason = 'no_price_data'
        return result

    peak_price = entry_price
    mfe = 0.0
    mae = 0.0

    for i, point in enumerate(prices):
        price = point['price']
        pnl_pct = (price - entry_price) / entry_price

        # Track MFE/MAE
        mfe = max(mfe, pnl_pct)
        mae = min(mae, pnl_pct)

        # Update trailing peak
        if price > peak_price:
            peak_price = price

        # --- Trailing stop ---
        if params.trailing_enabled and pnl_pct >= params.trailing_activation:
            drawdown_from_peak = (peak_price - price) / peak_price if peak_price > 0 else 0
            if drawdown_from_peak >= params.trailing_distance:
                pnl = (price - entry_price) * quantity - (price * quantity * FEE_RATE)
                result.replay_exit_price = price
                result.replay_pnl = pnl
                result.replay_exit_reason = 'trailing_stop'
                result.replay_bars_held = i + 1
                result.max_favorable_excursion = mfe
                result.max_adverse_excursion = mae
                return result

        # --- Stop loss ---
        if pnl_pct <= -params.stop_loss_pct:
            pnl = (price - entry_price) * quantity - (price * quantity * FEE_RATE)
            result.replay_exit_price = price
            result.replay_pnl = pnl
            result.replay_exit_reason = 'stop_loss'
            result.replay_bars_held = i + 1
            result.max_favorable_excursion = mfe
            result.max_adverse_excursion = mae
            return result

        # --- Take profit ---
        if pnl_pct >= params.take_profit_pct:
            pnl = (price - entry_price) * quantity - (price * quantity * FEE_RATE)
            result.replay_exit_price = price
            result.replay_pnl = pnl
            result.replay_exit_reason = 'take_profit'
            result.replay_bars_held = i + 1
            result.max_favorable_excursion = mfe
            result.max_adverse_excursion = mae
            return result

    # Never exited — use last price
    last_price = prices[-1]['price']
    pnl = (last_price - entry_price) * quantity - (last_price * quantity * FEE_RATE)
    result.replay_exit_price = last_price
    result.replay_pnl = pnl
    result.replay_exit_reason = 'end_of_data'
    result.replay_bars_held = len(prices)
    result.max_favorable_excursion = mfe
    result.max_adverse_excursion = mae
    return result


def _load_closed_trades(trading_strategy: str | None = None,
                        asset_type: str | None = None) -> list[dict]:
    """Load closed BUY trades from the DB with their metadata."""
    conn = None
    try:
        conn = get_db_connection()
        is_pg = isinstance(conn, psycopg2.extensions.connection)

        conditions = ["status = 'CLOSED'", "side = 'BUY'", "pnl IS NOT NULL"]
        params_list = []

        if trading_strategy:
            conditions.append(f"trading_strategy = {'%s' if is_pg else '?'}")
            params_list.append(trading_strategy)
        if asset_type:
            conditions.append(f"asset_type = {'%s' if is_pg else '?'}")
            params_list.append(asset_type)

        where = " AND ".join(conditions)
        query = (f"SELECT symbol, order_id, entry_price, exit_price, quantity, "
                 f"pnl, entry_timestamp, exit_timestamp, exit_reason, "
                 f"dynamic_sl_pct, dynamic_tp_pct, asset_type, trading_strategy "
                 f"FROM trades WHERE {where} ORDER BY entry_timestamp ASC")

        with _cursor(conn) as cursor:
            cursor.execute(query, tuple(params_list))
            return [dict(row) for row in cursor.fetchall()]
    except Exception as e:
        log.error(f"Failed to load closed trades: {e}", exc_info=True)
        return []
    finally:
        release_db_connection(conn)


def _load_price_path(symbol: str, start_time) -> list[dict]:
    """Load price snapshots for a symbol from entry time onward."""
    return get_price_history_for_trade(symbol, start_time)


# --- Sweep engine ---

def run_exit_sweep(param_grid: dict | None = None,
                   trading_strategy: str | None = None,
                   asset_type: str | None = None) -> dict:
    """Run a parameter sweep over exit parameters on historical trades.

    Args:
        param_grid: Dict of parameter names to lists of values. Uses DEFAULT_PARAM_GRID if None.
        trading_strategy: Filter trades by strategy ('manual', 'auto', or None for all).
        asset_type: Filter by 'crypto' or 'stock' (or None for all).

    Returns:
        Dict with 'current' (baseline results), 'sweep' (list of param combos + results),
        and 'best' (best performing combo).
    """
    grid = param_grid or DEFAULT_PARAM_GRID
    trades = _load_closed_trades(trading_strategy, asset_type)

    if not trades:
        log.warning("No closed trades found for replay.")
        return {'current': {}, 'sweep': [], 'best': {}, 'trade_count': 0}

    log.info(f"Loaded {len(trades)} closed trades for exit sweep.")

    # Pre-load all price paths (avoid repeated DB queries)
    price_cache: dict[str, list[dict]] = {}
    for trade in trades:
        symbol = trade['symbol']
        start = trade['entry_timestamp']
        cache_key = f"{symbol}_{start}"
        if cache_key not in price_cache:
            price_cache[cache_key] = _load_price_path(symbol, start)

    # --- Baseline: replay with actual SL/TP from each trade ---
    baseline_results = _replay_all_trades(trades, price_cache, None)

    # --- Sweep ---
    sl_values = grid.get('stop_loss_pct', [0.035])
    tp_values = grid.get('take_profit_pct', [0.08])
    trail_act_values = grid.get('trailing_activation', [0.02])
    trail_dist_values = grid.get('trailing_distance', [0.015])

    sweep_results = []
    total_combos = len(sl_values) * len(tp_values) * len(trail_act_values) * len(trail_dist_values)
    log.info(f"Running {total_combos} parameter combinations...")

    for sl, tp, t_act, t_dist in itertools.product(
        sl_values, tp_values, trail_act_values, trail_dist_values
    ):
        if tp <= sl:  # Skip invalid combos where TP <= SL
            continue

        params = ExitParams(
            stop_loss_pct=sl,
            take_profit_pct=tp,
            trailing_enabled=True,
            trailing_activation=t_act,
            trailing_distance=t_dist,
        )
        combo_results = _replay_all_trades(trades, price_cache, params)
        combo_results['params'] = {
            'stop_loss_pct': sl,
            'take_profit_pct': tp,
            'trailing_activation': t_act,
            'trailing_distance': t_dist,
        }
        sweep_results.append(combo_results)

    # Sort by total PnL descending
    sweep_results.sort(key=lambda x: x.get('total_pnl', 0), reverse=True)

    best = sweep_results[0] if sweep_results else {}

    log.info(f"Sweep complete. Best params: {best.get('params', {})} "
             f"→ PnL=${best.get('total_pnl', 0):.2f}, "
             f"WR={best.get('win_rate', 0):.1f}%")

    return {
        'current': baseline_results,
        'sweep': sweep_results[:20],  # top 20
        'best': best,
        'trade_count': len(trades),
    }


def _replay_all_trades(trades: list[dict], price_cache: dict,
                       params: ExitParams | None) -> dict:
    """Replay all trades with given params. If params is None, use each trade's own SL/TP."""
    total_pnl = 0.0
    wins = 0
    losses = 0
    exit_reasons = {}
    pnls = []
    mfes = []
    maes = []

    for trade in trades:
        symbol = trade['symbol']
        entry_price = trade['entry_price']
        quantity = trade['quantity']
        start = trade['entry_timestamp']
        cache_key = f"{symbol}_{start}"
        prices = price_cache.get(cache_key, [])

        if params is None:
            # Use actual trade params
            ep = ExitParams(
                stop_loss_pct=trade.get('dynamic_sl_pct') or 0.035,
                take_profit_pct=trade.get('dynamic_tp_pct') or 0.08,
                trailing_enabled=True,
                trailing_activation=0.02,
                trailing_distance=0.015,
            )
        else:
            ep = params

        result = replay_trade(entry_price, quantity, prices, ep)

        pnl = result.replay_pnl
        total_pnl += pnl
        pnls.append(pnl)
        mfes.append(result.max_favorable_excursion)
        maes.append(result.max_adverse_excursion)

        if pnl > 0:
            wins += 1
        elif pnl < 0:
            losses += 1

        reason = result.replay_exit_reason
        exit_reasons[reason] = exit_reasons.get(reason, 0) + 1

    total = wins + losses
    win_rate = (wins / total * 100) if total > 0 else 0.0
    avg_pnl = total_pnl / len(pnls) if pnls else 0.0
    win_pnls = [p for p in pnls if p > 0]
    loss_pnls = [p for p in pnls if p < 0]
    avg_win = statistics.mean(win_pnls) if win_pnls else 0.0
    avg_loss = statistics.mean(loss_pnls) if loss_pnls else 0.0
    profit_factor = (sum(win_pnls) / abs(sum(loss_pnls))) if loss_pnls else float('inf')

    return {
        'total_pnl': round(total_pnl, 2),
        'win_rate': round(win_rate, 1),
        'wins': wins,
        'losses': losses,
        'total_trades': len(pnls),
        'avg_pnl': round(avg_pnl, 2),
        'avg_win': round(avg_win, 2),
        'avg_loss': round(avg_loss, 2),
        'profit_factor': round(profit_factor, 3),
        'exit_reasons': exit_reasons,
        'avg_mfe': round(statistics.mean(mfes) * 100, 2) if mfes else 0.0,
        'avg_mae': round(statistics.mean(maes) * 100, 2) if maes else 0.0,
    }


# --- Signal quality analysis ---

def analyze_signal_quality(trading_strategy: str | None = None) -> dict:
    """Analyze which signal characteristics lead to profitable trades.

    Groups resolved signal attributions by catalyst_type and confidence bucket.
    Returns win rates, avg PnL, and optimal threshold per group.
    """
    conn = None
    try:
        conn = get_db_connection()
        is_pg = isinstance(conn, psycopg2.extensions.connection)

        conditions = ["trade_pnl IS NOT NULL", "resolved_at IS NOT NULL"]
        params_list = []

        if trading_strategy:
            # Join with trades to filter by strategy
            conditions.append(f"t.trading_strategy = {'%s' if is_pg else '?'}")
            params_list.append(trading_strategy)

        where = " AND ".join(conditions)

        if trading_strategy:
            query = (f"SELECT sa.gemini_direction, sa.gemini_confidence, sa.catalyst_type, "
                     f"sa.source_names, sa.trade_pnl, sa.trade_pnl_pct, "
                     f"sa.trade_duration_hours, sa.exit_reason "
                     f"FROM signal_attribution sa "
                     f"JOIN trades t ON sa.trade_order_id = t.order_id "
                     f"WHERE {where}")
        else:
            query = (f"SELECT gemini_direction, gemini_confidence, catalyst_type, "
                     f"source_names, trade_pnl, trade_pnl_pct, "
                     f"trade_duration_hours, exit_reason "
                     f"FROM signal_attribution "
                     f"WHERE {where}")

        with _cursor(conn) as cursor:
            cursor.execute(query, tuple(params_list))
            rows = [dict(row) for row in cursor.fetchall()]

        if not rows:
            return {'total_signals': 0, 'by_catalyst': {}, 'by_confidence': {},
                    'by_exit_reason': {}, 'optimal_threshold': None}

        # --- Group by catalyst type ---
        by_catalyst = {}
        for row in rows:
            cat = row.get('catalyst_type') or 'unknown'
            if cat not in by_catalyst:
                by_catalyst[cat] = []
            by_catalyst[cat].append(row)

        catalyst_analysis = {}
        for cat, cat_rows in by_catalyst.items():
            pnls = [r['trade_pnl'] for r in cat_rows if r.get('trade_pnl') is not None]
            wins = [p for p in pnls if p > 0]
            catalyst_analysis[cat] = {
                'count': len(cat_rows),
                'win_rate': round(len(wins) / len(pnls) * 100, 1) if pnls else 0,
                'avg_pnl': round(statistics.mean(pnls), 2) if pnls else 0,
                'total_pnl': round(sum(pnls), 2) if pnls else 0,
            }

        # --- Group by confidence bucket ---
        confidence_buckets = {}
        for row in rows:
            conf = row.get('gemini_confidence')
            if conf is None:
                bucket = 'unknown'
            elif conf < 0.5:
                bucket = '0.0-0.5'
            elif conf < 0.6:
                bucket = '0.5-0.6'
            elif conf < 0.7:
                bucket = '0.6-0.7'
            elif conf < 0.8:
                bucket = '0.7-0.8'
            else:
                bucket = '0.8-1.0'

            if bucket not in confidence_buckets:
                confidence_buckets[bucket] = []
            confidence_buckets[bucket].append(row)

        confidence_analysis = {}
        for bucket, bucket_rows in sorted(confidence_buckets.items()):
            pnls = [r['trade_pnl'] for r in bucket_rows if r.get('trade_pnl') is not None]
            wins = [p for p in pnls if p > 0]
            confidence_analysis[bucket] = {
                'count': len(bucket_rows),
                'win_rate': round(len(wins) / len(pnls) * 100, 1) if pnls else 0,
                'avg_pnl': round(statistics.mean(pnls), 2) if pnls else 0,
                'total_pnl': round(sum(pnls), 2) if pnls else 0,
            }

        # --- Group by exit reason ---
        by_exit = {}
        for row in rows:
            reason = row.get('exit_reason') or 'unknown'
            if reason not in by_exit:
                by_exit[reason] = []
            by_exit[reason].append(row)

        exit_analysis = {}
        for reason, reason_rows in by_exit.items():
            pnls = [r['trade_pnl'] for r in reason_rows if r.get('trade_pnl') is not None]
            wins = [p for p in pnls if p > 0]
            exit_analysis[reason] = {
                'count': len(reason_rows),
                'win_rate': round(len(wins) / len(pnls) * 100, 1) if pnls else 0,
                'avg_pnl': round(statistics.mean(pnls), 2) if pnls else 0,
            }

        # --- Optimal confidence threshold ---
        # Find the threshold that maximizes total PnL (trades above threshold)
        best_threshold = None
        best_pnl = float('-inf')
        for threshold in [0.4, 0.45, 0.5, 0.55, 0.6, 0.65, 0.7, 0.75, 0.8]:
            above = [r for r in rows
                     if r.get('gemini_confidence') is not None
                     and r['gemini_confidence'] >= threshold
                     and r.get('trade_pnl') is not None]
            if len(above) < 3:
                continue
            total = sum(r['trade_pnl'] for r in above)
            if total > best_pnl:
                best_pnl = total
                best_threshold = {
                    'threshold': threshold,
                    'total_pnl': round(total, 2),
                    'trades': len(above),
                    'win_rate': round(sum(1 for r in above if r['trade_pnl'] > 0) / len(above) * 100, 1),
                }

        return {
            'total_signals': len(rows),
            'by_catalyst': catalyst_analysis,
            'by_confidence': confidence_analysis,
            'by_exit_reason': exit_analysis,
            'optimal_threshold': best_threshold,
        }

    except Exception as e:
        log.error(f"Signal quality analysis failed: {e}", exc_info=True)
        return {'total_signals': 0, 'error': str(e)}
    finally:
        release_db_connection(conn)


# --- MFE/MAE analysis (how much opportunity is left on the table) ---

def analyze_mfe_mae(trading_strategy: str | None = None,
                    asset_type: str | None = None) -> dict:
    """Analyze Max Favorable/Adverse Excursion across all trades.

    Answers: "How far did winning trades run before being stopped out?"
    and "How much drawdown did we tolerate before exiting?"
    """
    trades = _load_closed_trades(trading_strategy, asset_type)
    if not trades:
        return {'trade_count': 0}

    price_cache: dict[str, list[dict]] = {}
    for trade in trades:
        symbol = trade['symbol']
        start = trade['entry_timestamp']
        cache_key = f"{symbol}_{start}"
        if cache_key not in price_cache:
            price_cache[cache_key] = _load_price_path(symbol, start)

    # Use actual trade params
    results_by_exit = {}
    for trade in trades:
        symbol = trade['symbol']
        entry_price = trade['entry_price']
        quantity = trade['quantity']
        start = trade['entry_timestamp']
        cache_key = f"{symbol}_{start}"
        prices = price_cache.get(cache_key, [])

        params = ExitParams(
            stop_loss_pct=trade.get('dynamic_sl_pct') or 0.035,
            take_profit_pct=trade.get('dynamic_tp_pct') or 0.08,
        )
        result = replay_trade(entry_price, quantity, prices, params)

        exit_reason = trade.get('exit_reason') or 'unknown'
        if exit_reason not in results_by_exit:
            results_by_exit[exit_reason] = {'mfes': [], 'maes': [], 'pnls': []}
        results_by_exit[exit_reason]['mfes'].append(result.max_favorable_excursion)
        results_by_exit[exit_reason]['maes'].append(result.max_adverse_excursion)
        results_by_exit[exit_reason]['pnls'].append(trade.get('pnl', 0))

    analysis = {}
    for reason, data in results_by_exit.items():
        sl_trades_with_mfe = [(mfe, pnl) for mfe, pnl in zip(data['mfes'], data['pnls'])]
        # How many SL trades had MFE > 2% (i.e., were profitable before being stopped)
        was_profitable = sum(1 for mfe, _ in sl_trades_with_mfe if mfe > 0.02)

        analysis[reason] = {
            'count': len(data['mfes']),
            'avg_mfe_pct': round(statistics.mean(data['mfes']) * 100, 2),
            'avg_mae_pct': round(statistics.mean(data['maes']) * 100, 2),
            'median_mfe_pct': round(statistics.median(data['mfes']) * 100, 2),
            'median_mae_pct': round(statistics.median(data['maes']) * 100, 2),
            'was_profitable_count': was_profitable,
            'avg_pnl': round(statistics.mean(data['pnls']), 2) if data['pnls'] else 0,
        }

    return {
        'trade_count': len(trades),
        'by_exit_reason': analysis,
    }


def format_sweep_report(sweep_result: dict) -> str:
    """Format sweep results as a readable report string."""
    lines = []
    trade_count = sweep_result.get('trade_count', 0)
    lines.append(f"Exit Parameter Sweep — {trade_count} trades\n")

    # Current baseline
    current = sweep_result.get('current', {})
    if current:
        lines.append("Current (per-trade dynamic SL/TP):")
        lines.append(f"  PnL: ${current.get('total_pnl', 0):.2f}  "
                     f"WR: {current.get('win_rate', 0):.1f}%  "
                     f"PF: {current.get('profit_factor', 0):.2f}  "
                     f"Avg MFE: {current.get('avg_mfe', 0):.1f}%  "
                     f"Avg MAE: {current.get('avg_mae', 0):.1f}%")
        lines.append(f"  Exits: {current.get('exit_reasons', {})}")
        lines.append("")

    # Best combo
    best = sweep_result.get('best', {})
    if best:
        bp = best.get('params', {})
        lines.append("Best parameter set:")
        lines.append(f"  SL={bp.get('stop_loss_pct', 0):.1%}  "
                     f"TP={bp.get('take_profit_pct', 0):.1%}  "
                     f"Trail act={bp.get('trailing_activation', 0):.1%}  "
                     f"dist={bp.get('trailing_distance', 0):.1%}")
        lines.append(f"  PnL: ${best.get('total_pnl', 0):.2f}  "
                     f"WR: {best.get('win_rate', 0):.1f}%  "
                     f"PF: {best.get('profit_factor', 0):.2f}")
        lines.append(f"  Exits: {best.get('exit_reasons', {})}")
        lines.append("")

    # Top 5
    sweep = sweep_result.get('sweep', [])
    if len(sweep) > 1:
        lines.append("Top 5 parameter sets:")
        for i, combo in enumerate(sweep[:5]):
            p = combo.get('params', {})
            lines.append(f"  {i+1}. SL={p.get('stop_loss_pct', 0):.1%} "
                         f"TP={p.get('take_profit_pct', 0):.1%} "
                         f"Trail={p.get('trailing_activation', 0):.1%}/"
                         f"{p.get('trailing_distance', 0):.1%}  "
                         f"→ PnL=${combo.get('total_pnl', 0):.2f}  "
                         f"WR={combo.get('win_rate', 0):.1f}%  "
                         f"PF={combo.get('profit_factor', 0):.2f}")

    return "\n".join(lines)


def format_quality_report(quality: dict) -> str:
    """Format signal quality analysis as a readable report string."""
    lines = []
    lines.append(f"Signal Quality Analysis — {quality.get('total_signals', 0)} resolved signals\n")

    # By catalyst type
    by_catalyst = quality.get('by_catalyst', {})
    if by_catalyst:
        lines.append("By catalyst type:")
        for cat, data in sorted(by_catalyst.items(), key=lambda x: x[1].get('total_pnl', 0), reverse=True):
            lines.append(f"  {cat}: {data['count']} signals, "
                         f"WR={data['win_rate']:.1f}%, "
                         f"PnL=${data['total_pnl']:.2f}, "
                         f"avg=${data['avg_pnl']:.2f}")
        lines.append("")

    # By confidence bucket
    by_conf = quality.get('by_confidence', {})
    if by_conf:
        lines.append("By confidence bucket:")
        for bucket, data in sorted(by_conf.items()):
            lines.append(f"  {bucket}: {data['count']} signals, "
                         f"WR={data['win_rate']:.1f}%, "
                         f"PnL=${data['total_pnl']:.2f}")
        lines.append("")

    # By exit reason
    by_exit = quality.get('by_exit_reason', {})
    if by_exit:
        lines.append("By exit reason:")
        for reason, data in sorted(by_exit.items(), key=lambda x: x[1]['count'], reverse=True):
            lines.append(f"  {reason}: {data['count']} trades, "
                         f"WR={data['win_rate']:.1f}%, "
                         f"avg=${data['avg_pnl']:.2f}")
        lines.append("")

    # Optimal threshold
    opt = quality.get('optimal_threshold')
    if opt:
        lines.append(f"Optimal confidence threshold: {opt['threshold']:.2f}")
        lines.append(f"  Would produce: {opt['trades']} trades, "
                     f"WR={opt['win_rate']:.1f}%, "
                     f"PnL=${opt['total_pnl']:.2f}")

    return "\n".join(lines)
