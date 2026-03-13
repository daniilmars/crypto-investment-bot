"""Auto post-mortem analysis — surfaces loss patterns in auto trades.

Joins trades + signal_attribution for rich context. Provides breakdown by
exit reason, confidence bucket, and symbol, plus actionable recommendations.
"""

from src.database import get_db_connection, release_db_connection, _cursor
from src.logger import log

import psycopg2
import sqlite3


def generate_auto_postmortem(days=30) -> dict:
    """Analyze closed auto trades and surface loss patterns.

    Returns:
      summary: {total, wins, losses, win_rate, total_pnl, avg_win, avg_loss}
      by_exit_reason: {stop_loss: {count, pnl}, ...}
      by_confidence_bucket: {low: {...}, med: {...}, high: {...}}
      by_symbol: {BTC: {count, win_rate, pnl}, ...}
      worst_trades: [top 5 worst]
      recommendations: [actionable text suggestions]
    """
    conn = None
    try:
        conn = get_db_connection()
        is_pg = isinstance(conn, psycopg2.extensions.connection)

        with _cursor(conn) as cursor:
            if is_pg:
                query = """
                    SELECT t.symbol, t.pnl, t.exit_reason, t.entry_price,
                           t.exit_price, t.entry_timestamp, t.exit_timestamp,
                           sa.signal_confidence, sa.catalyst_type, sa.source_names
                    FROM trades t
                    LEFT JOIN signal_attribution sa
                        ON t.order_id = sa.trade_order_id
                    WHERE t.trading_strategy = 'auto' AND t.status = 'CLOSED'
                      AND t.exit_timestamp >= NOW() - INTERVAL '%s days'
                    ORDER BY t.exit_timestamp DESC
                """
            else:
                query = """
                    SELECT t.symbol, t.pnl, t.exit_reason, t.entry_price,
                           t.exit_price, t.entry_timestamp, t.exit_timestamp,
                           sa.signal_confidence, sa.catalyst_type, sa.source_names
                    FROM trades t
                    LEFT JOIN signal_attribution sa
                        ON t.order_id = sa.trade_order_id
                    WHERE t.trading_strategy = 'auto' AND t.status = 'CLOSED'
                      AND t.exit_timestamp >= datetime('now', ? || ' days')
                    ORDER BY t.exit_timestamp DESC
                """

            param = days if is_pg else f'-{days}'
            cursor.execute(query, (param,))
            rows = cursor.fetchall()

            if rows and hasattr(rows[0], 'keys'):
                trades = [dict(r) for r in rows]
            elif rows:
                cols = [d[0] for d in cursor.description]
                trades = [dict(zip(cols, r)) for r in rows]
            else:
                trades = []

    except (sqlite3.Error, psycopg2.Error) as e:
        log.error(f"Database error in generate_auto_postmortem: {e}")
        trades = []
    finally:
        release_db_connection(conn)

    if not trades:
        return {
            'summary': {'total': 0, 'wins': 0, 'losses': 0, 'win_rate': 0,
                        'total_pnl': 0, 'avg_win': 0, 'avg_loss': 0},
            'by_exit_reason': {},
            'by_confidence_bucket': {},
            'by_symbol': {},
            'worst_trades': [],
            'recommendations': [],
        }

    # --- Summary ---
    wins = [t for t in trades if (t.get('pnl') or 0) > 0]
    losses = [t for t in trades if (t.get('pnl') or 0) <= 0]
    total_pnl = sum(t.get('pnl') or 0 for t in trades)
    avg_win = (sum(t['pnl'] for t in wins) / len(wins)) if wins else 0
    avg_loss = (sum(t['pnl'] for t in losses) / len(losses)) if losses else 0
    summary = {
        'total': len(trades),
        'wins': len(wins),
        'losses': len(losses),
        'win_rate': len(wins) / len(trades) if trades else 0,
        'total_pnl': total_pnl,
        'avg_win': avg_win,
        'avg_loss': avg_loss,
    }

    # --- By exit reason ---
    by_exit_reason = {}
    for t in trades:
        reason = t.get('exit_reason') or 'unknown'
        bucket = by_exit_reason.setdefault(reason, {'count': 0, 'pnl': 0, 'wins': 0})
        bucket['count'] += 1
        bucket['pnl'] += t.get('pnl') or 0
        if (t.get('pnl') or 0) > 0:
            bucket['wins'] += 1

    # --- By confidence bucket ---
    by_confidence_bucket = {
        'low': {'count': 0, 'pnl': 0, 'wins': 0, 'range': '<0.5'},
        'med': {'count': 0, 'pnl': 0, 'wins': 0, 'range': '0.5-0.7'},
        'high': {'count': 0, 'pnl': 0, 'wins': 0, 'range': '>0.7'},
    }
    for t in trades:
        conf = t.get('signal_confidence')
        if conf is None:
            continue
        if conf < 0.5:
            bucket = 'low'
        elif conf <= 0.7:
            bucket = 'med'
        else:
            bucket = 'high'
        by_confidence_bucket[bucket]['count'] += 1
        by_confidence_bucket[bucket]['pnl'] += t.get('pnl') or 0
        if (t.get('pnl') or 0) > 0:
            by_confidence_bucket[bucket]['wins'] += 1

    # --- By symbol ---
    by_symbol = {}
    for t in trades:
        sym = t.get('symbol', '?')
        bucket = by_symbol.setdefault(sym, {'count': 0, 'pnl': 0, 'wins': 0})
        bucket['count'] += 1
        bucket['pnl'] += t.get('pnl') or 0
        if (t.get('pnl') or 0) > 0:
            bucket['wins'] += 1
    for sym, data in by_symbol.items():
        data['win_rate'] = data['wins'] / data['count'] if data['count'] else 0

    # --- Worst trades ---
    worst_trades = sorted(trades, key=lambda t: t.get('pnl') or 0)[:5]

    # --- Recommendations ---
    recommendations = _generate_recommendations(
        summary, by_exit_reason, by_confidence_bucket, by_symbol)

    return {
        'summary': summary,
        'by_exit_reason': by_exit_reason,
        'by_confidence_bucket': by_confidence_bucket,
        'by_symbol': by_symbol,
        'worst_trades': worst_trades,
        'recommendations': recommendations,
    }


def _generate_recommendations(summary, by_exit_reason, by_confidence_bucket, by_symbol):
    """Generate actionable recommendations from trade data."""
    recs = []

    # Stop-loss exits > 50% of losses
    total_losses = summary['losses']
    if total_losses > 0:
        sl_data = by_exit_reason.get('stop_loss', {})
        sl_losses = sl_data.get('count', 0) - sl_data.get('wins', 0)
        if sl_losses / total_losses > 0.5:
            recs.append(
                f"Stop-loss exits account for {sl_losses}/{total_losses} losses "
                f"({sl_losses/total_losses:.0%}). Consider widening SL or "
                f"tightening entry criteria.")

    # Low confidence bucket with high loss rate
    low = by_confidence_bucket.get('low', {})
    if low.get('count', 0) >= 3:
        low_loss_rate = 1 - (low.get('wins', 0) / low['count'])
        if low_loss_rate > 0.6:
            recs.append(
                f"Low-confidence signals (<0.5) have {low_loss_rate:.0%} loss rate "
                f"across {low['count']} trades. Consider raising min_signal_strength.")

    # Symbol with 0% win rate and 3+ trades
    for sym, data in by_symbol.items():
        if data['count'] >= 3 and data['wins'] == 0:
            recs.append(
                f"{sym} has 0% win rate across {data['count']} trades "
                f"(PnL: ${data['pnl']:.2f}). Consider removing from watchlist.")

    # Overall poor performance
    if summary['total'] >= 5 and summary['win_rate'] < 0.3:
        recs.append(
            f"Overall win rate is {summary['win_rate']:.0%} across "
            f"{summary['total']} trades. The quality gate may need to be stricter.")

    return recs


def format_postmortem_message(report: dict, days: int = 30) -> str:
    """Format the post-mortem report for Telegram display."""
    s = report['summary']
    if s['total'] == 0:
        return f"No auto trades closed in the last {days} days."

    lines = [
        f"*Auto Trading Post-Mortem ({days}d)*\n",
        f"*Trades:* {s['total']} | *Win Rate:* {s['win_rate']:.0%}",
        f"*Total PnL:* ${s['total_pnl']:.2f}",
        f"*Avg Win:* ${s['avg_win']:.2f} | *Avg Loss:* ${s['avg_loss']:.2f}\n",
    ]

    # Exit reason breakdown
    if report['by_exit_reason']:
        lines.append("*By Exit Reason:*")
        for reason, data in sorted(report['by_exit_reason'].items(),
                                   key=lambda x: x[1]['pnl']):
            wr = data['wins'] / data['count'] if data['count'] else 0
            lines.append(f"  {reason}: {data['count']} trades, "
                        f"${data['pnl']:.2f}, {wr:.0%} WR")
        lines.append("")

    # Confidence breakdown
    conf = report['by_confidence_bucket']
    has_conf_data = any(conf[b]['count'] > 0 for b in conf)
    if has_conf_data:
        lines.append("*By Confidence:*")
        for bucket in ('low', 'med', 'high'):
            d = conf[bucket]
            if d['count'] > 0:
                wr = d['wins'] / d['count']
                lines.append(f"  {bucket} ({d['range']}): {d['count']} trades, "
                            f"${d['pnl']:.2f}, {wr:.0%} WR")
        lines.append("")

    # Worst symbols
    worst_syms = sorted(report['by_symbol'].items(),
                       key=lambda x: x[1]['pnl'])[:5]
    if worst_syms:
        lines.append("*Worst Symbols:*")
        for sym, data in worst_syms:
            lines.append(f"  {sym}: {data['count']} trades, "
                        f"${data['pnl']:.2f}, {data['win_rate']:.0%} WR")
        lines.append("")

    # Recommendations
    if report['recommendations']:
        lines.append("*Recommendations:*")
        for rec in report['recommendations']:
            lines.append(f"  • {rec}")

    return "\n".join(lines)
