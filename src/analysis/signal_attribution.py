"""Signal attribution tracker — links source → article → signal → trade → PnL.

Records which articles and sources contributed to each trading signal,
and resolves the attribution when the trade closes with a known PnL.
"""

from datetime import datetime, timezone

import psycopg2

from src.database import get_db_connection, release_db_connection, _cursor
from src.logger import log


def _ph(is_pg):
    return "%s" if is_pg else "?"


def build_attribution_articles(symbol, hours=24, limit=20):
    """Fetch recent articles with title_hash+source for attribution linkage.

    Returns [{'title_hash': str, 'source': str}] — the only fields attribution needs.
    Safe: returns [] on any error.
    """
    try:
        from src.database import get_recent_articles
        # get_recent_articles is @async_db-wrapped; call .sync for sync context.
        fetch = getattr(get_recent_articles, 'sync', get_recent_articles)
        rows = fetch(symbol, hours=hours, limit=limit)
        return [
            {'title_hash': r.get('title_hash', ''), 'source': r.get('source', '')}
            for r in rows
            if r.get('title_hash') and r.get('source')
        ]
    except Exception as e:
        log.debug(f"build_attribution_articles failed for {symbol}: {e}")
        return []


def build_grounding_attribution_articles(symbol, hours=2, limit=20):
    """Fallback: derive attribution from Gemini grounding URLs.

    When `scraped_articles` doesn't have rows tagged with this symbol
    (the common case for stocks — 67% empty source_names today), we look
    at the most recent `gemini_assessments` row for the symbol and pull
    `grounding_urls` (the URLs the grounded-search call actually cited).

    Returns [{'title_hash': sha256(url), 'source': 'gemini:<host>'}, ...].
    Safe: returns [] on any error.
    """
    import hashlib
    import json
    from urllib.parse import urlparse
    try:
        from src.database import get_db_connection, release_db_connection, _cursor
        conn = get_db_connection()
        try:
            is_pg = isinstance(conn, psycopg2.extensions.connection)
            ph = _ph(is_pg)
            since_clause = (
                "created_at >= NOW() - INTERVAL '%s hours'" if is_pg
                else "created_at >= datetime('now', ? || ' hours')"
            )
            params = (symbol, hours) if is_pg else (symbol, f'-{hours}')
            sql = (
                "SELECT grounding_urls FROM gemini_assessments "
                f"WHERE symbol = {ph} AND grounding_urls IS NOT NULL "
                f"AND {since_clause} "
                "ORDER BY created_at DESC LIMIT 1"
            )
            with _cursor(conn) as cur:
                cur.execute(sql, params)
                row = cur.fetchone()
            if not row:
                return []
            raw = row['grounding_urls'] if is_pg else row[0]
        finally:
            release_db_connection(conn)
        if not raw:
            return []
        urls = json.loads(raw)
        if not isinstance(urls, list):
            return []
        out = []
        seen = set()
        for url in urls[:limit]:
            try:
                host = urlparse(url).netloc.removeprefix('www.') or 'unknown'
            except Exception:
                host = 'unknown'
            h = hashlib.sha256(str(url).encode()).hexdigest()
            if h in seen:
                continue
            seen.add(h)
            out.append({'title_hash': h, 'source': f'gemini:{host}'})
        return out
    except Exception as e:
        log.debug(f"build_grounding_attribution_articles failed for {symbol}: {e}")
        return []


def record_signal_attribution(signal, articles=None, gemini_assessment=None):
    """Record which articles/sources contributed to a signal.

    Args:
        signal: dict with 'symbol', 'signal' (BUY/SELL), 'current_price', 'reason'.
        articles: list of article dicts that contributed (with 'title_hash', 'source').
        gemini_assessment: dict with 'direction', 'confidence', optionally 'catalyst_type'.

    Returns:
        attribution_id or None.
    """
    conn = None
    try:
        conn = get_db_connection()
        is_pg = isinstance(conn, psycopg2.extensions.connection)
        ph = _ph(is_pg)

        symbol = signal.get('symbol', '')
        signal_type = signal.get('signal', signal.get('signal_type', ''))
        now = datetime.now(timezone.utc).isoformat()

        # Extract article hashes and source names
        article_hashes = ''
        source_names = ''
        if articles:
            hashes = [a.get('title_hash', '') for a in articles if a.get('title_hash')]
            sources = list(dict.fromkeys(
                a.get('source', '') for a in articles if a.get('source')
            ))
            article_hashes = ','.join(hashes[:50])  # cap at 50
            source_names = ','.join(sources[:20])

        # Extract Gemini assessment
        gemini_direction = None
        gemini_confidence = None
        catalyst_type = None
        signal_confidence = None
        if gemini_assessment:
            gemini_direction = gemini_assessment.get('direction')
            gemini_confidence = gemini_assessment.get('confidence')
            catalyst_type = gemini_assessment.get('catalyst_type')
            signal_confidence = gemini_confidence

        query = f"""
            INSERT INTO signal_attribution
                (symbol, signal_type, signal_timestamp, signal_confidence,
                 article_hashes, source_names,
                 gemini_direction, gemini_confidence, catalyst_type)
            VALUES ({ph}, {ph}, {ph}, {ph}, {ph}, {ph}, {ph}, {ph}, {ph})
        """
        with _cursor(conn) as cur:
            cur.execute(query, (
                symbol, signal_type, now, signal_confidence,
                article_hashes, source_names,
                gemini_direction, gemini_confidence, catalyst_type,
            ))

            # Get the inserted ID
            if is_pg:
                cur.execute("SELECT lastval()")
            else:
                cur.execute("SELECT last_insert_rowid()")
            row = cur.fetchone()
            attr_id = list(row.values())[0] if is_pg else row[0]

        conn.commit()
        log.info(f"Recorded signal attribution #{attr_id} for {signal_type} {symbol} "
                 f"(sources: {source_names[:60]})")
        return attr_id
    except Exception as e:
        log.error(f"Failed to record signal attribution: {e}")
        if conn:
            try:
                conn.rollback()
            except Exception:
                pass
        return None
    finally:
        if conn:
            release_db_connection(conn)


def link_attribution_to_order(attribution_id, order_id):
    """Link a signal attribution record to the trade order_id after execution."""
    if not attribution_id or not order_id:
        return
    conn = None
    try:
        conn = get_db_connection()
        is_pg = isinstance(conn, psycopg2.extensions.connection)
        ph = _ph(is_pg)
        with _cursor(conn) as cur:
            cur.execute(
                f"UPDATE signal_attribution SET trade_order_id = {ph} WHERE id = {ph}",
                (order_id, attribution_id))
        conn.commit()
    except Exception as e:
        log.error(f"Failed to link attribution #{attribution_id} to order {order_id}: {e}")
        if conn:
            try:
                conn.rollback()
            except Exception:
                pass
    finally:
        if conn:
            release_db_connection(conn)


def resolve_attribution(order_id, pnl, pnl_pct=None, duration_hours=None,
                        exit_reason=None):
    """Resolve attribution when a trade closes — fill PnL fields.

    Args:
        order_id: the trade's order_id.
        pnl: realized PnL in USD.
        pnl_pct: PnL as percentage (e.g. 0.05 = 5%).
        duration_hours: how long the trade was open.
        exit_reason: 'take_profit', 'stop_loss', 'trailing_stop', 'signal_sell'.
    """
    conn = None
    try:
        conn = get_db_connection()
        is_pg = isinstance(conn, psycopg2.extensions.connection)
        ph = _ph(is_pg)
        now = datetime.now(timezone.utc).isoformat()

        query = f"""
            UPDATE signal_attribution
            SET trade_pnl = {ph},
                trade_pnl_pct = {ph},
                trade_duration_hours = {ph},
                exit_reason = {ph},
                resolved_at = {ph}
            WHERE trade_order_id = {ph}
              AND resolved_at IS NULL
        """
        with _cursor(conn) as cur:
            cur.execute(query, (pnl, pnl_pct, duration_hours, exit_reason,
                                now, order_id))
            updated = cur.rowcount
        conn.commit()

        if updated:
            log.info(f"Resolved attribution for order {order_id}: "
                     f"PnL=${pnl:.2f} ({exit_reason})")
        return updated
    except Exception as e:
        log.error(f"Failed to resolve attribution for order {order_id}: {e}")
        if conn:
            try:
                conn.rollback()
            except Exception:
                pass
        return 0
    finally:
        if conn:
            release_db_connection(conn)


def get_unresolved_attributions(symbol=None):
    """Get signal attributions that haven't been resolved yet (trade still open)."""
    conn = None
    try:
        conn = get_db_connection()
        is_pg = isinstance(conn, psycopg2.extensions.connection)
        ph = _ph(is_pg)

        conditions = ["resolved_at IS NULL", "trade_order_id IS NOT NULL"]
        params = []
        if symbol:
            conditions.append(f"symbol = {ph}")
            params.append(symbol)

        where = " AND ".join(conditions)
        with _cursor(conn) as cur:
            cur.execute(f"SELECT * FROM signal_attribution WHERE {where}", params)
            rows = cur.fetchall()

        if is_pg:
            return [dict(r) for r in rows]
        cols = [d[0] for d in cur.description] if hasattr(cur, 'description') and cur.description else []
        return [dict(zip(cols, r)) for r in rows] if cols else []
    except Exception as e:
        log.error(f"Failed to get unresolved attributions: {e}")
        return []
    finally:
        if conn:
            release_db_connection(conn)


def get_source_performance(source_name=None, days=30):
    """Aggregate PnL stats by source over the given period.

    Returns list of dicts: [{source_name, total_signals, wins, losses,
                             total_pnl, avg_pnl, win_rate}]
    """
    conn = None
    try:
        conn = get_db_connection()
        is_pg = isinstance(conn, psycopg2.extensions.connection)
        ph = _ph(is_pg)

        if is_pg:
            date_filter = f"created_at >= NOW() - INTERVAL '{days} days'"
        else:
            date_filter = f"created_at >= datetime('now', '-{days} days')"

        conditions = [date_filter, "resolved_at IS NOT NULL"]
        params = []

        if source_name:
            conditions.append(f"source_names LIKE {ph}")
            params.append(f"%{source_name}%")

        where = " AND ".join(conditions)

        query = f"""
            SELECT source_names, trade_pnl, trade_pnl_pct
            FROM signal_attribution
            WHERE {where}
            ORDER BY resolved_at DESC
        """
        with _cursor(conn) as cur:
            cur.execute(query, params)
            rows = cur.fetchall()

        if is_pg:
            rows = [dict(r) for r in rows]
        else:
            cols = [d[0] for d in cur.description] if hasattr(cur, 'description') and cur.description else []
            rows = [dict(zip(cols, r)) for r in rows] if cols else []

        # Aggregate per source
        source_stats = {}
        for row in rows:
            sources = (row.get('source_names') or '').split(',')
            pnl = row.get('trade_pnl') or 0
            for src in sources:
                src = src.strip()
                if not src:
                    continue
                if src not in source_stats:
                    source_stats[src] = {'wins': 0, 'losses': 0, 'total_pnl': 0.0, 'pnls': []}
                source_stats[src]['total_pnl'] += pnl
                source_stats[src]['pnls'].append(pnl)
                if pnl > 0:
                    source_stats[src]['wins'] += 1
                else:
                    source_stats[src]['losses'] += 1

        result = []
        for name, stats in sorted(source_stats.items(), key=lambda x: x[1]['total_pnl'], reverse=True):
            total = stats['wins'] + stats['losses']
            result.append({
                'source_name': name,
                'total_signals': total,
                'wins': stats['wins'],
                'losses': stats['losses'],
                'total_pnl': stats['total_pnl'],
                'avg_pnl': stats['total_pnl'] / total if total > 0 else 0,
                'win_rate': stats['wins'] / total if total > 0 else 0,
            })
        return result
    except Exception as e:
        log.error(f"Failed to get source performance: {e}")
        return []
    finally:
        if conn:
            release_db_connection(conn)


def get_signal_accuracy(symbol=None, days=30):
    """Get signal win rate and average PnL, optionally filtered by symbol."""
    conn = None
    try:
        conn = get_db_connection()
        is_pg = isinstance(conn, psycopg2.extensions.connection)
        ph = _ph(is_pg)

        if is_pg:
            date_filter = f"created_at >= NOW() - INTERVAL '{days} days'"
        else:
            date_filter = f"created_at >= datetime('now', '-{days} days')"

        conditions = [date_filter, "resolved_at IS NOT NULL"]
        params = []
        if symbol:
            conditions.append(f"symbol = {ph}")
            params.append(symbol)

        where = " AND ".join(conditions)
        query = f"""
            SELECT symbol, signal_type, trade_pnl, trade_pnl_pct, exit_reason
            FROM signal_attribution WHERE {where}
        """
        with _cursor(conn) as cur:
            cur.execute(query, params)
            rows = cur.fetchall()

        if is_pg:
            rows = [dict(r) for r in rows]
        else:
            cols = [d[0] for d in cur.description] if hasattr(cur, 'description') and cur.description else []
            rows = [dict(zip(cols, r)) for r in rows] if cols else []

        if not rows:
            return {'total': 0, 'wins': 0, 'losses': 0, 'win_rate': 0, 'avg_pnl': 0}

        wins = sum(1 for r in rows if (r.get('trade_pnl') or 0) > 0)
        losses = len(rows) - wins
        total_pnl = sum(r.get('trade_pnl') or 0 for r in rows)
        return {
            'total': len(rows),
            'wins': wins,
            'losses': losses,
            'win_rate': wins / len(rows) if rows else 0,
            'avg_pnl': total_pnl / len(rows) if rows else 0,
        }
    except Exception as e:
        log.error(f"Failed to get signal accuracy: {e}")
        return {'total': 0, 'wins': 0, 'losses': 0, 'win_rate': 0, 'avg_pnl': 0}
    finally:
        if conn:
            release_db_connection(conn)


def get_symbol_win_rates(days=30, min_trades=3):
    """Get per-symbol win rates from resolved attributions.

    Returns dict: {symbol: {'wins': int, 'losses': int, 'win_rate': float, 'total': int}}
    Only includes symbols with >= min_trades resolved trades.
    """
    conn = None
    try:
        conn = get_db_connection()
        is_pg = isinstance(conn, psycopg2.extensions.connection)

        if is_pg:
            date_filter = f"resolved_at >= NOW() - INTERVAL '{days} days'"
        else:
            date_filter = f"resolved_at >= datetime('now', '-{days} days')"

        query = f"""
            SELECT symbol,
                   COUNT(*) AS total,
                   SUM(CASE WHEN trade_pnl > 0 THEN 1 ELSE 0 END) AS wins
            FROM signal_attribution
            WHERE resolved_at IS NOT NULL AND trade_pnl IS NOT NULL
              AND {date_filter}
            GROUP BY symbol
            HAVING COUNT(*) >= {min_trades}
        """
        with _cursor(conn) as cur:
            cur.execute(query)
            rows = cur.fetchall()

        result = {}
        for row in rows:
            if is_pg:
                sym, total, wins = row['symbol'], row['total'], row['wins']
            else:
                sym, total, wins = row[0], row[1], row[2]
            wins = wins or 0
            result[sym] = {
                'total': total,
                'wins': wins,
                'losses': total - wins,
                'win_rate': wins / total if total else 0,
            }
        return result
    except Exception as e:
        log.warning(f"Failed to get symbol win rates: {e}")
        return {}
    finally:
        if conn:
            release_db_connection(conn)


def get_recent_trade_outcomes(days=14, limit=30):
    """Get recent resolved trade outcomes for prompt feedback.

    Returns list of dicts with symbol, confidence, catalyst, exit reason, and PnL.
    Used to inject trade history context into the Gemini scoring prompt.
    """
    conn = None
    try:
        conn = get_db_connection()
        is_pg = isinstance(conn, psycopg2.extensions.connection)
        ph = _ph(is_pg)

        if is_pg:
            query = f"""
                SELECT symbol, signal_type, gemini_confidence, catalyst_type,
                       exit_reason, trade_pnl, trade_pnl_pct
                FROM signal_attribution
                WHERE resolved_at IS NOT NULL
                  AND trade_pnl IS NOT NULL
                  AND resolved_at >= NOW() - INTERVAL '{ph} days'
                ORDER BY resolved_at DESC
                LIMIT {ph}
            """
        else:
            query = f"""
                SELECT symbol, signal_type, gemini_confidence, catalyst_type,
                       exit_reason, trade_pnl, trade_pnl_pct
                FROM signal_attribution
                WHERE resolved_at IS NOT NULL
                  AND trade_pnl IS NOT NULL
                  AND resolved_at >= datetime('now', {ph} || ' days')
                ORDER BY resolved_at DESC
                LIMIT {ph}
            """

        with _cursor(conn) as cur:
            if is_pg:
                cur.execute(query, (days, limit))
            else:
                cur.execute(query, (f'-{days}', limit))
            cols = [d[0] for d in cur.description]
            return [dict(zip(cols, r)) for r in cur.fetchall()]
    except Exception as e:
        log.warning(f"Failed to get trade outcomes for feedback: {e}")
        return []
    finally:
        if conn:
            release_db_connection(conn)


def get_recent_attributions(symbol=None, limit=20):
    """Get recent attribution records for display."""
    conn = None
    try:
        conn = get_db_connection()
        is_pg = isinstance(conn, psycopg2.extensions.connection)
        ph = _ph(is_pg)

        conditions = []
        params = []
        if symbol:
            conditions.append(f"symbol = {ph}")
            params.append(symbol)

        where = " WHERE " + " AND ".join(conditions) if conditions else ""
        params.append(limit)
        query = f"""
            SELECT * FROM signal_attribution{where}
            ORDER BY created_at DESC LIMIT {ph}
        """
        with _cursor(conn) as cur:
            cur.execute(query, params)
            rows = cur.fetchall()

        if is_pg:
            return [dict(r) for r in rows]
        cols = [d[0] for d in cur.description] if hasattr(cur, 'description') and cur.description else []
        return [dict(zip(cols, r)) for r in rows] if cols else []
    except Exception as e:
        log.error(f"Failed to get recent attributions: {e}")
        return []
    finally:
        if conn:
            release_db_connection(conn)
