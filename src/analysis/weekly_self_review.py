"""Weekly self-review — analyzes trade outcomes by dimension and sends Telegram summary.

Runs every Sunday 4AM UTC (configurable). Queries 7 days of resolved signal
attributions and computes win rates by confidence bucket, catalyst freshness,
catalyst type, and strategy. No Gemini API calls — pure Python analysis.
"""

from collections import defaultdict

from src.database import get_db_connection, release_db_connection, _cursor
from src.logger import log

try:
    import psycopg2
except ImportError:
    psycopg2 = None


def run_weekly_self_review(days=7, min_trades=5) -> dict | None:
    """Query recent trades and compute performance by dimension.

    Returns summary dict, or None if insufficient data.
    """
    conn = None
    try:
        conn = get_db_connection()
        is_pg = psycopg2 and isinstance(conn, psycopg2.extensions.connection)

        if is_pg:
            date_filter = f"resolved_at >= NOW() - INTERVAL '{days} days'"
        else:
            date_filter = f"resolved_at >= datetime('now', '-{days} days')"

        query = f"""
            SELECT symbol, gemini_confidence, catalyst_type,
                   trade_pnl, trade_pnl_pct, exit_reason
            FROM signal_attribution
            WHERE resolved_at IS NOT NULL AND trade_pnl IS NOT NULL
              AND {date_filter}
        """
        with _cursor(conn) as cur:
            cur.execute(query)
            if is_pg:
                rows = [dict(r) for r in cur.fetchall()]
            else:
                cols = [d[0] for d in cur.description]
                rows = [dict(zip(cols, r)) for r in cur.fetchall()]

        if len(rows) < min_trades:
            return {'status': 'insufficient_data', 'trade_count': len(rows),
                    'min_required': min_trades}

        # Overall stats
        total = len(rows)
        wins = sum(1 for r in rows if (r.get('trade_pnl') or 0) > 0)
        total_pnl = sum(r.get('trade_pnl') or 0 for r in rows)

        # By confidence bucket
        by_confidence = _bucket_by(rows, _confidence_bucket)

        # By catalyst freshness (parse from catalyst_type or use directly)
        by_freshness = _bucket_by(rows, lambda r: r.get('catalyst_type', 'unknown'))

        # By exit reason
        by_exit = _bucket_by(rows, lambda r: r.get('exit_reason', 'unknown'))

        return {
            'status': 'ok',
            'days': days,
            'trade_count': total,
            'wins': wins,
            'losses': total - wins,
            'win_rate': wins / total if total else 0,
            'total_pnl': total_pnl,
            'by_confidence': by_confidence,
            'by_catalyst': by_freshness,
            'by_exit': by_exit,
        }

    except Exception as e:
        log.error(f"Weekly self-review failed: {e}")
        return None
    finally:
        release_db_connection(conn)


def _confidence_bucket(row):
    """Map gemini_confidence to a human-readable bucket."""
    conf = row.get('gemini_confidence')
    if conf is None:
        return 'unknown'
    if conf >= 0.8:
        return '0.80+'
    if conf >= 0.7:
        return '0.70-0.79'
    if conf >= 0.6:
        return '0.60-0.69'
    if conf >= 0.5:
        return '0.50-0.59'
    return '<0.50'


def _bucket_by(rows, key_fn):
    """Group rows by key function, compute wins/losses/pnl per bucket."""
    buckets = defaultdict(lambda: {'total': 0, 'wins': 0, 'pnl': 0})
    for r in rows:
        key = key_fn(r)
        buckets[key]['total'] += 1
        if (r.get('trade_pnl') or 0) > 0:
            buckets[key]['wins'] += 1
        buckets[key]['pnl'] += r.get('trade_pnl') or 0
    # Add win_rate
    for b in buckets.values():
        b['win_rate'] = b['wins'] / b['total'] if b['total'] else 0
        b['losses'] = b['total'] - b['wins']
    return dict(buckets)


def format_weekly_review_telegram(summary: dict) -> str:
    """Format weekly review as a Telegram-friendly message."""
    if not summary:
        return "Weekly review failed."

    if summary.get('status') == 'insufficient_data':
        return (f"Weekly Review: Not enough data "
                f"({summary['trade_count']}/{summary['min_required']} trades needed).")

    days = summary.get('days', 7)
    total = summary['trade_count']
    wins = summary['wins']
    losses = summary['losses']
    wr = summary['win_rate']
    pnl = summary['total_pnl']

    lines = [
        f"Weekly Self-Review ({days}d)",
        "",
        f"Trades: {total} | {wins}W/{losses}L | WR: {wr:.0%} | PnL: ${pnl:.2f}",
    ]

    # Confidence breakdown
    by_conf = summary.get('by_confidence', {})
    if by_conf:
        lines.append("")
        lines.append("By Confidence:")
        for bucket in ['0.80+', '0.70-0.79', '0.60-0.69', '0.50-0.59', '<0.50', 'unknown']:
            if bucket in by_conf:
                b = by_conf[bucket]
                best = ' <- best' if b['win_rate'] == max(
                    d['win_rate'] for d in by_conf.values() if d['total'] >= 2) else ''
                lines.append(
                    f"  {bucket}: {b['wins']}/{b['total']} "
                    f"({b['win_rate']:.0%}) ${b['pnl']:+.2f}{best}")

    # Catalyst breakdown
    by_cat = summary.get('by_catalyst', {})
    if by_cat:
        lines.append("")
        lines.append("By Catalyst:")
        for cat, b in sorted(by_cat.items(), key=lambda x: -x[1]['total']):
            if b['total'] >= 2:
                lines.append(
                    f"  {cat}: {b['wins']}/{b['total']} "
                    f"({b['win_rate']:.0%}) ${b['pnl']:+.2f}")

    # Insights
    lines.append("")
    insights = _derive_insights(summary)
    for insight in insights[:3]:
        lines.append(f"* {insight}")

    return '\n'.join(lines)


def _derive_insights(summary: dict) -> list[str]:
    """Generate actionable insights from the weekly data."""
    insights = []

    by_conf = summary.get('by_confidence', {})
    by_cat = summary.get('by_catalyst', {})

    # Find best/worst confidence bracket
    conf_ranked = [(k, v) for k, v in by_conf.items() if v['total'] >= 2]
    if conf_ranked:
        best = max(conf_ranked, key=lambda x: x[1]['win_rate'])
        worst = min(conf_ranked, key=lambda x: x[1]['win_rate'])
        if best[1]['win_rate'] > worst[1]['win_rate'] + 0.15:
            insights.append(
                f"Confidence {best[0]} outperforms {worst[0]} "
                f"({best[1]['win_rate']:.0%} vs {worst[1]['win_rate']:.0%})")

    # Find worst catalyst type
    cat_ranked = [(k, v) for k, v in by_cat.items() if v['total'] >= 2]
    if cat_ranked:
        worst_cat = min(cat_ranked, key=lambda x: x[1]['win_rate'])
        if worst_cat[1]['win_rate'] < 0.35:
            insights.append(
                f"'{worst_cat[0]}' catalyst underperforming "
                f"({worst_cat[1]['win_rate']:.0%} WR) — consider raising threshold")

    # Overall assessment
    wr = summary.get('win_rate', 0)
    if wr >= 0.60:
        insights.append("Strong week. Current parameters working well.")
    elif wr >= 0.45:
        insights.append("Average week. Monitor for emerging patterns.")
    else:
        insights.append("Weak week. Review losing trades for common patterns.")

    return insights
