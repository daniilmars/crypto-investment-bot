"""Auto-parameter tuner — periodically re-optimizes strategy parameters.

Runs parameter sweeps using recent trade data, compares to current config,
and applies improvements with safety guardrails.
"""

import math
from datetime import datetime, timezone

import psycopg2

from src.config import app_config
from src.database import get_db_connection, release_db_connection, _cursor
from src.logger import log


def _ph(is_pg):
    return "%s" if is_pg else "?"


# Default safety bounds
DEFAULT_BOUNDS = {
    'stop_loss_percentage': (0.015, 0.06),
    'take_profit_percentage': (0.03, 0.15),
    'min_gemini_confidence': (0.35, 0.75),
    'signal_cooldown_hours': (2, 8),
}

# Parameter sweep ranges
SWEEP_RANGES = {
    'stop_loss_percentage': [0.02, 0.025, 0.03, 0.035, 0.04, 0.045, 0.05],
    'take_profit_percentage': [0.04, 0.06, 0.08, 0.10, 0.12],
    'min_gemini_confidence': [0.4, 0.45, 0.5, 0.55, 0.6],
    'signal_cooldown_hours': [2, 3, 4, 6, 8],
}


def reload_tuned_params():
    """Reload applied (non-reverted) parameter changes from DB at startup."""
    conn = None
    try:
        conn = get_db_connection()
        is_pg = isinstance(conn, psycopg2.extensions.connection)
        with _cursor(conn) as cur:
            if is_pg:
                query = """
                    SELECT DISTINCT ON (parameter_name) parameter_name, new_value
                    FROM tuning_history
                    WHERE applied = TRUE AND reverted = FALSE
                    ORDER BY parameter_name, tuning_run_at DESC
                """
            else:
                query = """
                    SELECT th.parameter_name, th.new_value
                    FROM tuning_history th
                    INNER JOIN (
                        SELECT parameter_name, MAX(tuning_run_at) AS max_run
                        FROM tuning_history
                        WHERE applied = 1 AND reverted = 0
                        GROUP BY parameter_name
                    ) latest ON th.parameter_name = latest.parameter_name
                        AND th.tuning_run_at = latest.max_run
                    WHERE th.applied = 1 AND th.reverted = 0
                """
            cur.execute(query)
            rows = cur.fetchall()

        settings = app_config.get('settings', {})
        count = 0
        for row in rows:
            param = row[0] if isinstance(row, tuple) else row['parameter_name']
            value = row[1] if isinstance(row, tuple) else row['new_value']
            if param in settings:
                settings[param] = float(value)
                count += 1
        if count:
            log.info(f"Auto-tuner: reloaded {count} tuned parameters from DB")
    except Exception as e:
        log.warning(f"Could not reload tuned params: {e}")
    finally:
        if conn:
            release_db_connection(conn)


def run_auto_tune():
    """Run a full auto-tuning cycle.

    1. Collect recent trade data
    2. Run parameter sweeps
    3. Compare to current config
    4. Apply improvements (with safety guardrails)

    Returns summary dict.
    """
    config = app_config.get('settings', {}).get('autonomous_bot', {})
    tune_config = config.get('auto_tuner', {})

    if not tune_config.get('enabled', False):
        return {'skipped': True, 'reason': 'auto_tuner disabled'}

    min_trades = tune_config.get('min_sample_trades', 20)
    min_sharpe_improvement = tune_config.get('min_sharpe_improvement', 0.1)
    max_changes = tune_config.get('max_param_changes_per_cycle', 2)

    # Get safety bounds from config (or defaults)
    bounds = {
        'stop_loss_percentage': (
            tune_config.get('sl_min', DEFAULT_BOUNDS['stop_loss_percentage'][0]),
            tune_config.get('sl_max', DEFAULT_BOUNDS['stop_loss_percentage'][1]),
        ),
        'take_profit_percentage': (
            tune_config.get('tp_min', DEFAULT_BOUNDS['take_profit_percentage'][0]),
            tune_config.get('tp_max', DEFAULT_BOUNDS['take_profit_percentage'][1]),
        ),
        'min_gemini_confidence': (
            tune_config.get('confidence_min', DEFAULT_BOUNDS['min_gemini_confidence'][0]),
            tune_config.get('confidence_max', DEFAULT_BOUNDS['min_gemini_confidence'][1]),
        ),
    }

    # 1. Collect recent trades
    trades = _get_recent_trades(days=30)
    if len(trades) < min_trades:
        return {
            'skipped': True,
            'reason': f'insufficient_trades ({len(trades)} < {min_trades})',
        }

    # 2. Evaluate current parameters
    settings = app_config.get('settings', {})
    current_params = {
        'stop_loss_percentage': settings.get('stop_loss_percentage', 0.035),
        'take_profit_percentage': settings.get('take_profit_percentage', 0.08),
    }
    current_metrics = _evaluate_params(trades, current_params)

    # 3. Run parameter sweeps
    best_params = dict(current_params)
    best_metrics = dict(current_metrics)
    improvements = []

    for param_name, values in SWEEP_RANGES.items():
        if param_name not in current_params:
            continue

        bound = bounds.get(param_name, (None, None))

        for value in values:
            # Safety bounds check
            if bound[0] is not None and value < bound[0]:
                continue
            if bound[1] is not None and value > bound[1]:
                continue

            test_params = dict(best_params)
            test_params[param_name] = value
            test_metrics = _evaluate_params(trades, test_params)

            sharpe_diff = test_metrics['sharpe'] - best_metrics['sharpe']
            if sharpe_diff >= min_sharpe_improvement:
                improvements.append({
                    'param': param_name,
                    'old_value': best_params[param_name],
                    'new_value': value,
                    'sharpe_improvement': sharpe_diff,
                    'new_sharpe': test_metrics['sharpe'],
                    'new_win_rate': test_metrics['win_rate'],
                })
                best_params[param_name] = value
                best_metrics = test_metrics

    # 4. Sort by improvement and cap at max_changes
    improvements.sort(key=lambda x: x['sharpe_improvement'], reverse=True)
    to_apply = improvements[:max_changes]

    summary = {
        'trades_analyzed': len(trades),
        'current_sharpe': current_metrics['sharpe'],
        'current_win_rate': current_metrics['win_rate'],
        'improvements_found': len(improvements),
        'changes_applied': [],
    }

    # 5. Apply changes
    for change in to_apply:
        applied = _apply_param_change(
            change['param'], change['old_value'], change['new_value'],
            len(trades), current_metrics['sharpe'], change['new_sharpe'],
            current_metrics['win_rate'], change['new_win_rate'],
        )
        if applied:
            summary['changes_applied'].append(change)

    if summary['changes_applied']:
        log.info(f"Auto-tuner applied {len(summary['changes_applied'])} changes: "
                 f"Sharpe {current_metrics['sharpe']:.2f} → {best_metrics['sharpe']:.2f}")
    else:
        log.info(f"Auto-tuner: no improvements found "
                 f"(current Sharpe={current_metrics['sharpe']:.2f})")

    return summary


def _get_recent_trades(days=30):
    """Get closed trades from the last N days."""
    conn = None
    try:
        conn = get_db_connection()
        is_pg = isinstance(conn, psycopg2.extensions.connection)

        if is_pg:
            date_filter = f"exit_timestamp >= NOW() - INTERVAL '{days} days'"
        else:
            date_filter = f"exit_timestamp >= datetime('now', '-{days} days')"

        query = f"""
            SELECT symbol, entry_price, exit_price, pnl, quantity,
                   entry_timestamp, exit_timestamp
            FROM trades
            WHERE status = 'CLOSED' AND {date_filter}
            ORDER BY exit_timestamp DESC
        """
        with _cursor(conn) as cur:
            cur.execute(query)
            rows = cur.fetchall()

        if is_pg:
            return [dict(r) for r in rows]
        cols = [d[0] for d in cur.description] if hasattr(cur, 'description') and cur.description else []
        return [dict(zip(cols, r)) for r in rows] if cols else []
    except Exception as e:
        log.error(f"Failed to get recent trades: {e}")
        return []
    finally:
        if conn:
            release_db_connection(conn)


def _evaluate_params(trades, params):
    """Evaluate a parameter set against historical trades.

    Simulates SL/TP outcomes and computes Sharpe ratio + win rate.
    """
    if not trades:
        return {'sharpe': 0.0, 'win_rate': 0.0, 'total_return': 0.0}

    sl_pct = params.get('stop_loss_percentage', 0.035)
    tp_pct = params.get('take_profit_percentage', 0.08)

    returns = []
    for trade in trades:
        entry = trade.get('entry_price') or 0
        exit_p = trade.get('exit_price') or 0
        if entry <= 0:
            continue

        pnl_pct = (exit_p - entry) / entry

        # Simulate: would this trade have hit SL or TP under these params?
        # Since we don't have intraday candles, use the actual outcome but
        # cap it at our SL/TP levels for a rough approximation
        simulated_pnl = max(-sl_pct, min(tp_pct, pnl_pct))
        returns.append(simulated_pnl)

    if not returns:
        return {'sharpe': 0.0, 'win_rate': 0.0, 'total_return': 0.0}

    wins = sum(1 for r in returns if r > 0)
    avg_return = sum(returns) / len(returns)
    total_return = sum(returns)

    # Sharpe ratio (annualized with standard 252 trading days)
    if len(returns) > 1:
        std = _std(returns)
        if std > 0:
            sharpe = (avg_return / std) * math.sqrt(252)
        else:
            sharpe = 0.0
    else:
        sharpe = 0.0

    return {
        'sharpe': round(sharpe, 3),
        'win_rate': round(wins / len(returns), 3),
        'total_return': round(total_return, 4),
    }


def _std(values):
    """Standard deviation."""
    if len(values) < 2:
        return 0.0
    mean = sum(values) / len(values)
    variance = sum((v - mean) ** 2 for v in values) / (len(values) - 1)
    return math.sqrt(variance)


def _apply_param_change(param_name, old_value, new_value, sample_trades,
                        old_sharpe, new_sharpe, old_win_rate, new_win_rate):
    """Record a parameter change in tuning_history and update runtime config.

    Returns True if applied successfully.
    """
    from src.analysis.feedback_loop import _log_experiment

    conn = None
    try:
        conn = get_db_connection()
        is_pg = isinstance(conn, psycopg2.extensions.connection)
        ph = _ph(is_pg)

        query = f"""
            INSERT INTO tuning_history
                (parameter_name, old_value, new_value, sample_trades,
                 old_sharpe, new_sharpe, old_win_rate, new_win_rate, applied)
            VALUES ({ph}, {ph}, {ph}, {ph}, {ph}, {ph}, {ph}, {ph},
                    {"TRUE" if is_pg else "1"})
        """
        with _cursor(conn) as cur:
            cur.execute(query, (
                param_name, old_value, new_value, sample_trades,
                old_sharpe, new_sharpe, old_win_rate, new_win_rate,
            ))
        conn.commit()

        # Update runtime config
        settings = app_config.get('settings', {})
        if param_name in settings:
            settings[param_name] = new_value
            log.info(f"Auto-tuner: {param_name} changed from {old_value} to {new_value} "
                     f"(Sharpe: {old_sharpe:.2f} → {new_sharpe:.2f})")

        # Log to experiment log
        _log_experiment(
            'param_change',
            f"Auto-tuner changed {param_name}: {old_value} → {new_value}",
            old_value=str(old_value),
            new_value=str(new_value),
            reason=f"Sharpe improvement: {old_sharpe:.2f} → {new_sharpe:.2f}, "
                   f"Win rate: {old_win_rate:.1%} → {new_win_rate:.1%}",
            impact_metric='sharpe_ratio',
        )
        return True
    except Exception as e:
        log.error(f"Failed to apply param change {param_name}: {e}")
        if conn:
            try:
                conn.rollback()
            except Exception:
                pass
        return False
    finally:
        if conn:
            release_db_connection(conn)


def check_and_revert():
    """Check recent param changes for performance degradation and auto-revert.

    If performance degrades >10% in 7 days after a change, revert it.
    Returns list of reverted changes.
    """
    config = app_config.get('settings', {}).get('autonomous_bot', {})
    tune_config = config.get('auto_tuner', {})

    revert_days = tune_config.get('auto_revert_days', 7)
    degradation_pct = tune_config.get('auto_revert_degradation_pct', 0.10)

    conn = None
    reverted = []
    try:
        conn = get_db_connection()
        is_pg = isinstance(conn, psycopg2.extensions.connection)

        if is_pg:
            date_filter = f"tuning_run_at >= NOW() - INTERVAL '{revert_days} days'"
        else:
            date_filter = f"tuning_run_at >= datetime('now', '-{revert_days} days')"

        is_true = "TRUE" if is_pg else "1"
        is_false = "FALSE" if is_pg else "0"

        with _cursor(conn) as cur:
            cur.execute(
                f"SELECT * FROM tuning_history "
                f"WHERE applied = {is_true} AND reverted = {is_false} AND {date_filter}")
            rows = cur.fetchall()

        if is_pg:
            changes = [dict(r) for r in rows]
        else:
            cols = [d[0] for d in cur.description] if hasattr(cur, 'description') and cur.description else []
            changes = [dict(zip(cols, r)) for r in rows] if cols else []

        if not changes:
            return reverted

        # Check current performance
        recent_trades = _get_recent_trades(days=revert_days)
        if len(recent_trades) < 5:
            return reverted  # Not enough data to evaluate

        for change in changes:
            old_sharpe = change.get('old_sharpe') or 0
            # Evaluate current performance with current params
            settings = app_config.get('settings', {})
            current_params = {
                'stop_loss_percentage': settings.get('stop_loss_percentage', 0.035),
                'take_profit_percentage': settings.get('take_profit_percentage', 0.08),
            }
            current_metrics = _evaluate_params(recent_trades, current_params)

            # Check if performance degraded
            if old_sharpe > 0 and current_metrics['sharpe'] < old_sharpe * (1 - degradation_pct):
                _revert_change(change)
                reverted.append(change)

        return reverted
    except Exception as e:
        log.error(f"Failed to check for reverts: {e}")
        return reverted
    finally:
        if conn:
            release_db_connection(conn)


def _revert_change(change):
    """Revert a parameter change."""
    from src.analysis.feedback_loop import _log_experiment

    param_name = change.get('parameter_name')
    old_value = change.get('old_value')
    change_id = change.get('id')

    conn = None
    try:
        conn = get_db_connection()
        is_pg = isinstance(conn, psycopg2.extensions.connection)
        ph = _ph(is_pg)
        now = datetime.now(timezone.utc).isoformat()
        is_true = "TRUE" if is_pg else "1"

        with _cursor(conn) as cur:
            cur.execute(
                f"UPDATE tuning_history SET reverted = {is_true}, "
                f"reverted_at = {ph}, revert_reason = 'performance_degradation' "
                f"WHERE id = {ph}",
                (now, change_id))
        conn.commit()

        # Revert runtime config
        settings = app_config.get('settings', {})
        if param_name in settings:
            settings[param_name] = old_value
            log.info(f"Auto-tuner REVERTED: {param_name} back to {old_value}")

        _log_experiment(
            'param_revert',
            f"Reverted {param_name} back to {old_value} due to performance degradation",
            old_value=str(change.get('new_value')),
            new_value=str(old_value),
            reason='performance_degradation',
        )
    except Exception as e:
        log.error(f"Failed to revert change {change_id}: {e}")
        if conn:
            try:
                conn.rollback()
            except Exception:
                pass
    finally:
        if conn:
            release_db_connection(conn)


def get_tuning_history(limit=20):
    """Get recent tuning history for display."""
    conn = None
    try:
        conn = get_db_connection()
        is_pg = isinstance(conn, psycopg2.extensions.connection)
        ph = _ph(is_pg)
        with _cursor(conn) as cur:
            cur.execute(
                f"SELECT * FROM tuning_history ORDER BY tuning_run_at DESC LIMIT {ph}",
                (limit,))
            rows = cur.fetchall()
        if is_pg:
            return [dict(r) for r in rows]
        cols = [d[0] for d in cur.description] if hasattr(cur, 'description') and cur.description else []
        return [dict(zip(cols, r)) for r in rows] if cols else []
    except Exception as e:
        log.error(f"Failed to get tuning history: {e}")
        return []
    finally:
        if conn:
            release_db_connection(conn)


def get_current_vs_suggested():
    """Get current parameters vs what the tuner would suggest.

    Returns dict with current values and suggested values.
    """
    settings = app_config.get('settings', {})
    current = {
        'stop_loss_percentage': settings.get('stop_loss_percentage', 0.035),
        'take_profit_percentage': settings.get('take_profit_percentage', 0.08),
        'signal_cooldown_hours': settings.get('signal_cooldown_hours', 4),
    }

    trades = _get_recent_trades(days=30)
    current_metrics = _evaluate_params(trades, current)

    return {
        'current_params': current,
        'current_metrics': current_metrics,
        'trade_count': len(trades),
    }
