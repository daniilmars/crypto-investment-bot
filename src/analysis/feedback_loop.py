"""Feedback loop — closes the loop from trade outcomes back to source scores.

When a trade closes, we:
1. Resolve signal attributions with PnL data
2. Update source reliability scores
3. Check promotion/demotion thresholds
4. Log all changes to experiment_log
"""

import psycopg2

from src.config import app_config
from src.database import get_db_connection, release_db_connection, _cursor
from src.logger import log


def _ph(is_pg):
    return "%s" if is_pg else "?"


def process_closed_trade(order_id, pnl, pnl_pct=None, duration_hours=None,
                         exit_reason=None):
    """Process a closed trade: resolve attribution and update source scores.

    Called when any trade closes (SL/TP/trailing/signal_sell).

    Args:
        order_id: the trade's order_id
        pnl: realized PnL in USD
        pnl_pct: PnL percentage
        duration_hours: time position was held
        exit_reason: 'take_profit', 'stop_loss', 'trailing_stop', 'signal_sell'
    """
    from src.analysis.signal_attribution import resolve_attribution
    from src.collectors.source_registry import (
        get_source_by_name, update_signal_stats,
    )

    # 1. Resolve the attribution record
    updated = resolve_attribution(
        order_id, pnl, pnl_pct=pnl_pct,
        duration_hours=duration_hours, exit_reason=exit_reason)

    if not updated:
        log.debug(f"No attribution to resolve for order {order_id}")
        return

    # 2. Get the source names from the resolved attribution
    source_names = _get_attribution_sources(order_id)
    if not source_names:
        return

    profitable = pnl > 0

    # 3. Update source signal stats
    for src_name in source_names:
        source = get_source_by_name(src_name)
        if source:
            update_signal_stats(source['id'], profitable, pnl)
            log.debug(f"Updated signal stats for source '{src_name}': "
                      f"pnl={pnl:.2f}, profitable={profitable}")


def _get_attribution_sources(order_id):
    """Get source names from signal_attribution for a given order_id."""
    conn = None
    try:
        conn = get_db_connection()
        is_pg = isinstance(conn, psycopg2.extensions.connection)
        ph = _ph(is_pg)
        with _cursor(conn) as cur:
            cur.execute(
                f"SELECT source_names FROM signal_attribution "
                f"WHERE trade_order_id = {ph}", (order_id,))
            row = cur.fetchone()
        if not row:
            return []
        raw = row['source_names'] if is_pg else row[0]
        if not raw:
            return []
        return [s.strip() for s in raw.split(',') if s.strip()]
    except Exception as e:
        log.error(f"Failed to get attribution sources for {order_id}: {e}")
        return []
    finally:
        if conn:
            release_db_connection(conn)


def run_daily_source_review():
    """Daily review: recalculate source scores, promote/demote, deactivate.

    Returns a summary dict of actions taken.
    """
    from src.collectors.source_registry import (
        get_all_sources, update_reliability_score,
        deactivate_source, promote_source,
    )

    config = app_config.get('settings', {}).get('autonomous_bot', {})
    fb_config = config.get('feedback_loop', {})
    disc_config = config.get('source_discovery', {})

    if not fb_config.get('enabled', False):
        return {'skipped': True, 'reason': 'feedback_loop disabled'}

    min_trades = fb_config.get('min_trades_for_scoring', 5)
    promotion_threshold = disc_config.get('promotion_threshold', 0.6)
    demotion_threshold = disc_config.get('demotion_threshold', 0.2)

    sources = get_all_sources(include_inactive=False)
    actions = {'promoted': [], 'demoted': [], 'deactivated': [], 'scores_updated': 0}

    for source in sources:
        source_id = source['id']
        total_signals = source.get('articles_with_signals') or 0
        total_articles = source.get('articles_total') or 0
        consecutive_errors = source.get('consecutive_errors') or 0
        tier = source.get('tier', 2)

        # Calculate reliability score
        score = _calculate_reliability_score(source)
        update_reliability_score(source_id, score)
        actions['scores_updated'] += 1

        # Auto-deactivate: too many errors or zero articles in 14 days
        if consecutive_errors > 50:
            deactivate_source(source_id, f'consecutive_errors={consecutive_errors}')
            _log_experiment('source_demotion', f"Deactivated '{source['source_name']}': "
                           f"{consecutive_errors} consecutive errors",
                           old_value=str(tier), new_value='inactive')
            actions['deactivated'].append(source['source_name'])
            continue

        if score < demotion_threshold and total_articles > 0:
            # Deactivate unreliable sources
            deactivate_source(source_id, f'reliability_score={score:.2f}')
            _log_experiment('source_demotion', f"Deactivated '{source['source_name']}': "
                           f"reliability={score:.2f} < {demotion_threshold}",
                           old_value=str(tier), new_value='inactive')
            actions['deactivated'].append(source['source_name'])
            continue

        # Promotion: trial (tier 3) → standard (tier 2)
        if tier == 3 and score > promotion_threshold and total_signals >= min_trades:
            promote_source(source_id, 2)
            _log_experiment('source_promotion',
                           f"Promoted '{source['source_name']}' from trial to standard: "
                           f"reliability={score:.2f}",
                           old_value='3', new_value='2')
            actions['promoted'].append(source['source_name'])

        # Promotion: standard (tier 2) → premium (tier 1)
        profitable_ratio = source.get('profitable_signal_ratio') or 0
        if tier == 2 and profitable_ratio > 0.55 and total_signals >= 10:
            promote_source(source_id, 1)
            _log_experiment('source_promotion',
                           f"Promoted '{source['source_name']}' from standard to premium: "
                           f"profitable_ratio={profitable_ratio:.2f}",
                           old_value='2', new_value='1')
            actions['promoted'].append(source['source_name'])

    log.info(f"Daily source review: {actions['scores_updated']} scores updated, "
             f"{len(actions['promoted'])} promoted, {len(actions['deactivated'])} deactivated")
    return actions


def _calculate_reliability_score(source):
    """Calculate reliability score for a source.

    Formula:
        0.3 * availability_rate +
        0.2 * relevance_rate +
        0.2 * uniqueness_proxy +
        0.3 * signal_contribution_rate
    """
    total_articles = source.get('articles_total') or 0
    error_count = source.get('error_count') or 0
    consecutive_errors = source.get('consecutive_errors') or 0
    total_signals = source.get('articles_with_signals') or 0
    profitable_ratio = source.get('profitable_signal_ratio') or 0.0

    # Availability: fewer errors = higher score
    total_fetches = total_articles + error_count
    if total_fetches > 0:
        availability = total_articles / total_fetches
    else:
        availability = 0.5  # default for new sources

    # Penalize recent consecutive errors
    if consecutive_errors > 10:
        availability *= 0.5

    # Relevance proxy: articles that led to signals / total articles
    relevance = total_signals / total_articles if total_articles > 10 else 0.5

    # Uniqueness proxy: we don't track dedup per-source yet, use a flat 0.5
    uniqueness = 0.5

    # Signal contribution: profitable ratio
    signal_contribution = profitable_ratio if total_signals >= 5 else 0.5

    score = (0.3 * availability + 0.2 * relevance +
             0.2 * uniqueness + 0.3 * signal_contribution)
    return round(min(1.0, max(0.0, score)), 3)


def _log_experiment(experiment_type, description, old_value=None, new_value=None,
                    reason=None, impact_metric=None):
    """Log an autonomous decision to the experiment_log table."""
    conn = None
    try:
        conn = get_db_connection()
        is_pg = isinstance(conn, psycopg2.extensions.connection)
        ph = _ph(is_pg)
        query = f"""
            INSERT INTO experiment_log
                (experiment_type, description, old_value, new_value, reason, impact_metric)
            VALUES ({ph}, {ph}, {ph}, {ph}, {ph}, {ph})
        """
        with _cursor(conn) as cur:
            cur.execute(query, (experiment_type, description, old_value,
                                new_value, reason, impact_metric))
        conn.commit()
    except Exception as e:
        log.error(f"Failed to log experiment: {e}")
        if conn:
            try:
                conn.rollback()
            except Exception:
                pass
    finally:
        if conn:
            release_db_connection(conn)


def get_recent_experiments(limit=20):
    """Get recent experiment log entries for display."""
    conn = None
    try:
        conn = get_db_connection()
        is_pg = isinstance(conn, psycopg2.extensions.connection)
        ph = _ph(is_pg)
        with _cursor(conn) as cur:
            cur.execute(
                f"SELECT * FROM experiment_log ORDER BY created_at DESC LIMIT {ph}",
                (limit,))
            rows = cur.fetchall()
        if is_pg:
            return [dict(r) for r in rows]
        cols = [d[0] for d in cur.description] if hasattr(cur, 'description') and cur.description else []
        return [dict(zip(cols, r)) for r in rows] if cols else []
    except Exception as e:
        log.error(f"Failed to get recent experiments: {e}")
        return []
    finally:
        if conn:
            release_db_connection(conn)
