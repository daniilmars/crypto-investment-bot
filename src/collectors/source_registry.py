"""DB-driven source registry — replaces hardcoded RSS_FEEDS and ALL_SCRAPERS.

Sources are stored in the source_registry table and can be modified at runtime.
The seed_registry() function migrates existing hardcoded feeds on first run.
"""

import json
from datetime import datetime, timezone

import psycopg2
import sqlite3

from src.config import app_config
from src.database import get_db_connection, release_db_connection, _cursor
from src.logger import log


def _ph(is_pg):
    """Return the placeholder for the current DB backend."""
    return "%s" if is_pg else "?"


def load_active_sources(source_type=None, category=None, tier_max=None):
    """Load active sources from the registry.

    Args:
        source_type: filter by 'rss', 'web_scraper', or None for all.
        category: filter by category (e.g. 'crypto', 'financial') or None.
        tier_max: max tier to include (e.g. 2 = standard+premium only).

    Returns:
        list of dicts with source fields.
    """
    conn = None
    try:
        conn = get_db_connection()
        is_pg = isinstance(conn, psycopg2.extensions.connection)
        ph = _ph(is_pg)

        conditions = ["is_active = " + ("TRUE" if is_pg else "1")]
        params = []

        if source_type:
            conditions.append(f"source_type = {ph}")
            params.append(source_type)
        if category:
            conditions.append(f"category = {ph}")
            params.append(category)
        if tier_max is not None:
            conditions.append(f"tier <= {ph}")
            params.append(tier_max)

        where = " AND ".join(conditions)
        query = f"SELECT * FROM source_registry WHERE {where} ORDER BY tier, reliability_score DESC"

        with _cursor(conn) as cur:
            cur.execute(query, params)
            rows = cur.fetchall()

        if is_pg:
            return [dict(r) for r in rows]
        else:
            cols = [d[0] for d in cur.description] if hasattr(cur, 'description') and cur.description else []
            if not cols:
                return []
            return [dict(zip(cols, r)) for r in rows]
    except Exception as e:
        log.error(f"Failed to load active sources: {e}")
        return []
    finally:
        if conn:
            release_db_connection(conn)


def get_source_by_name(source_name):
    """Get a single source by name."""
    conn = None
    try:
        conn = get_db_connection()
        is_pg = isinstance(conn, psycopg2.extensions.connection)
        ph = _ph(is_pg)
        with _cursor(conn) as cur:
            cur.execute(f"SELECT * FROM source_registry WHERE source_name = {ph}", (source_name,))
            row = cur.fetchone()
            if not row:
                return None
            if is_pg:
                return dict(row)
            cols = [d[0] for d in cur.description]
            return dict(zip(cols, row))
    except Exception as e:
        log.error(f"Failed to get source '{source_name}': {e}")
        return None
    finally:
        if conn:
            release_db_connection(conn)


def get_source_by_id(source_id):
    """Get a single source by ID."""
    conn = None
    try:
        conn = get_db_connection()
        is_pg = isinstance(conn, psycopg2.extensions.connection)
        ph = _ph(is_pg)
        with _cursor(conn) as cur:
            cur.execute(f"SELECT * FROM source_registry WHERE id = {ph}", (source_id,))
            row = cur.fetchone()
            if not row:
                return None
            if is_pg:
                return dict(row)
            cols = [d[0] for d in cur.description]
            return dict(zip(cols, row))
    except Exception as e:
        log.error(f"Failed to get source id={source_id}: {e}")
        return None
    finally:
        if conn:
            release_db_connection(conn)


def add_source(source_type, source_name, source_url, category=None,
               tier=2, added_by='manual', metadata=None):
    """Add a new source to the registry. Returns the new source ID or None."""
    conn = None
    try:
        conn = get_db_connection()
        is_pg = isinstance(conn, psycopg2.extensions.connection)
        ph = _ph(is_pg)
        meta_json = json.dumps(metadata) if metadata else None

        query = f"""
            INSERT INTO source_registry
                (source_type, source_name, source_url, category, tier, added_by, metadata_json)
            VALUES ({ph}, {ph}, {ph}, {ph}, {ph}, {ph}, {ph})
        """
        with _cursor(conn) as cur:
            cur.execute(query, (source_type, source_name, source_url,
                                category, tier, added_by, meta_json))
        conn.commit()
        log.info(f"Added source '{source_name}' ({source_type}, tier={tier})")

        # Get the ID of the inserted row
        with _cursor(conn) as cur:
            cur.execute(f"SELECT id FROM source_registry WHERE source_name = {ph}",
                        (source_name,))
            row = cur.fetchone()
            return row[0] if row and not is_pg else (row['id'] if row else None)
    except (psycopg2.errors.UniqueViolation, sqlite3.IntegrityError):
        log.warning(f"Source '{source_name}' already exists in registry.")
        if conn:
            try:
                conn.rollback()
            except Exception:
                pass
        return None
    except Exception as e:
        log.error(f"Failed to add source '{source_name}': {e}")
        if conn:
            try:
                conn.rollback()
            except Exception:
                pass
        return None
    finally:
        if conn:
            release_db_connection(conn)


def update_source_stats(source_id, articles_fetched=0, errors=0):
    """Update fetch stats after a collection cycle."""
    conn = None
    try:
        conn = get_db_connection()
        is_pg = isinstance(conn, psycopg2.extensions.connection)
        ph = _ph(is_pg)
        now = datetime.now(timezone.utc).isoformat()

        if errors > 0:
            query = f"""
                UPDATE source_registry
                SET error_count = error_count + {ph},
                    consecutive_errors = consecutive_errors + {ph},
                    last_fetched_at = {ph}
                WHERE id = {ph}
            """
            params = (errors, errors, now, source_id)
        else:
            query = f"""
                UPDATE source_registry
                SET articles_total = articles_total + {ph},
                    consecutive_errors = 0,
                    last_fetched_at = {ph},
                    last_article_at = CASE WHEN {ph} > 0 THEN {ph} ELSE last_article_at END
                WHERE id = {ph}
            """
            params = (articles_fetched, now, articles_fetched, now, source_id)

        with _cursor(conn) as cur:
            cur.execute(query, params)
        conn.commit()
    except Exception as e:
        log.error(f"Failed to update source stats for id={source_id}: {e}")
        if conn:
            try:
                conn.rollback()
            except Exception:
                pass
    finally:
        if conn:
            release_db_connection(conn)


def update_reliability_score(source_id, score):
    """Update the reliability score for a source."""
    conn = None
    try:
        conn = get_db_connection()
        is_pg = isinstance(conn, psycopg2.extensions.connection)
        ph = _ph(is_pg)
        with _cursor(conn) as cur:
            cur.execute(
                f"UPDATE source_registry SET reliability_score = {ph} WHERE id = {ph}",
                (score, source_id))
        conn.commit()
    except Exception as e:
        log.error(f"Failed to update reliability score for id={source_id}: {e}")
        if conn:
            try:
                conn.rollback()
            except Exception:
                pass
    finally:
        if conn:
            release_db_connection(conn)


def update_signal_stats(source_id, profitable, pnl):
    """Update signal performance stats for a source after a trade resolves."""
    conn = None
    try:
        conn = get_db_connection()
        is_pg = isinstance(conn, psycopg2.extensions.connection)
        ph = _ph(is_pg)

        # Fetch current stats
        with _cursor(conn) as cur:
            cur.execute(
                f"SELECT articles_with_signals, profitable_signal_ratio, avg_signal_pnl "
                f"FROM source_registry WHERE id = {ph}", (source_id,))
            row = cur.fetchone()
            if not row:
                return

        if is_pg:
            total_signals = (row.get('articles_with_signals') or 0)
            old_ratio = row.get('profitable_signal_ratio') or 0.0
            old_avg_pnl = row.get('avg_signal_pnl') or 0.0
        else:
            total_signals = row[0] or 0
            old_ratio = row[1] or 0.0
            old_avg_pnl = row[2] or 0.0

        new_total = total_signals + 1
        # Running average for profitable ratio
        profitable_count = round(old_ratio * total_signals) + (1 if profitable else 0)
        new_ratio = profitable_count / new_total if new_total > 0 else 0.0
        # Running average for PnL
        new_avg_pnl = ((old_avg_pnl * total_signals) + pnl) / new_total

        with _cursor(conn) as cur:
            cur.execute(
                f"UPDATE source_registry SET articles_with_signals = {ph}, "
                f"profitable_signal_ratio = {ph}, avg_signal_pnl = {ph} "
                f"WHERE id = {ph}",
                (new_total, new_ratio, new_avg_pnl, source_id))
        conn.commit()
    except Exception as e:
        log.error(f"Failed to update signal stats for id={source_id}: {e}")
        if conn:
            try:
                conn.rollback()
            except Exception:
                pass
    finally:
        if conn:
            release_db_connection(conn)


def deactivate_source(source_id, reason='auto'):
    """Deactivate a source and record the reason."""
    conn = None
    try:
        conn = get_db_connection()
        is_pg = isinstance(conn, psycopg2.extensions.connection)
        ph = _ph(is_pg)
        now = datetime.now(timezone.utc).isoformat()
        is_active = "FALSE" if is_pg else "0"
        with _cursor(conn) as cur:
            cur.execute(
                f"UPDATE source_registry SET is_active = {is_active}, "
                f"deactivated_at = {ph}, deactivation_reason = {ph} WHERE id = {ph}",
                (now, reason, source_id))
        conn.commit()
        log.info(f"Deactivated source id={source_id}: {reason}")
    except Exception as e:
        log.error(f"Failed to deactivate source id={source_id}: {e}")
        if conn:
            try:
                conn.rollback()
            except Exception:
                pass
    finally:
        if conn:
            release_db_connection(conn)


def activate_source(source_id):
    """Reactivate a deactivated source."""
    conn = None
    try:
        conn = get_db_connection()
        is_pg = isinstance(conn, psycopg2.extensions.connection)
        ph = _ph(is_pg)
        is_active = "TRUE" if is_pg else "1"
        with _cursor(conn) as cur:
            cur.execute(
                f"UPDATE source_registry SET is_active = {is_active}, "
                f"deactivated_at = NULL, deactivation_reason = NULL, "
                f"consecutive_errors = 0 WHERE id = {ph}",
                (source_id,))
        conn.commit()
        log.info(f"Activated source id={source_id}")
    except Exception as e:
        log.error(f"Failed to activate source id={source_id}: {e}")
        if conn:
            try:
                conn.rollback()
            except Exception:
                pass
    finally:
        if conn:
            release_db_connection(conn)


def promote_source(source_id, new_tier):
    """Change the tier of a source (promotion or demotion)."""
    conn = None
    try:
        conn = get_db_connection()
        is_pg = isinstance(conn, psycopg2.extensions.connection)
        ph = _ph(is_pg)
        with _cursor(conn) as cur:
            cur.execute(
                f"UPDATE source_registry SET tier = {ph} WHERE id = {ph}",
                (new_tier, source_id))
        conn.commit()
        log.info(f"Changed tier for source id={source_id} to tier={new_tier}")
    except Exception as e:
        log.error(f"Failed to promote source id={source_id}: {e}")
        if conn:
            try:
                conn.rollback()
            except Exception:
                pass
    finally:
        if conn:
            release_db_connection(conn)


def get_source_count():
    """Get total count of active sources."""
    conn = None
    try:
        conn = get_db_connection()
        is_pg = isinstance(conn, psycopg2.extensions.connection)
        is_active = "TRUE" if is_pg else "1"
        with _cursor(conn) as cur:
            cur.execute(f"SELECT COUNT(*) FROM source_registry WHERE is_active = {is_active}")
            row = cur.fetchone()
            return row[0] if not is_pg else list(row.values())[0]
    except Exception:
        return 0
    finally:
        if conn:
            release_db_connection(conn)


def get_all_sources(include_inactive=False):
    """Get all sources (for admin/display)."""
    conn = None
    try:
        conn = get_db_connection()
        is_pg = isinstance(conn, psycopg2.extensions.connection)
        where = "" if include_inactive else " WHERE is_active = " + ("TRUE" if is_pg else "1")
        with _cursor(conn) as cur:
            cur.execute(f"SELECT * FROM source_registry{where} ORDER BY tier, source_name")
            rows = cur.fetchall()
        if is_pg:
            return [dict(r) for r in rows]
        cols = [d[0] for d in cur.description] if hasattr(cur, 'description') and cur.description else []
        if not cols:
            return []
        return [dict(zip(cols, r)) for r in rows]
    except Exception as e:
        log.error(f"Failed to get all sources: {e}")
        return []
    finally:
        if conn:
            release_db_connection(conn)


def seed_registry():
    """One-time migration: populate source_registry from hardcoded feeds and scrapers.

    Safe to call multiple times — uses INSERT OR IGNORE / ON CONFLICT DO NOTHING.
    Returns the number of sources inserted.
    """
    from src.collectors.news_data import RSS_FEEDS
    from src.collectors.web_news_scraper import ALL_SCRAPERS

    conn = None
    inserted = 0
    try:
        conn = get_db_connection()
        is_pg = isinstance(conn, psycopg2.extensions.connection)
        ph = _ph(is_pg)

        if is_pg:
            insert_sql = f"""
                INSERT INTO source_registry
                    (source_type, source_name, source_url, category, tier, added_by)
                VALUES ({ph}, {ph}, {ph}, {ph}, {ph}, {ph})
                ON CONFLICT (source_name) DO NOTHING
            """
        else:
            insert_sql = f"""
                INSERT OR IGNORE INTO source_registry
                    (source_type, source_name, source_url, category, tier, added_by)
                VALUES ({ph}, {ph}, {ph}, {ph}, {ph}, {ph})
            """

        with _cursor(conn) as cur:
            # Seed RSS feeds
            for feed in RSS_FEEDS:
                url = feed['url']
                category = feed.get('category', 'unknown')
                # Derive a human-readable name from the URL
                name = _derive_feed_name(url, category)
                tier = _derive_tier(category)
                cur.execute(insert_sql, ('rss', name, url, category, tier, 'seed'))
                if cur.rowcount and cur.rowcount > 0:
                    inserted += 1

            # Seed web scrapers
            for scraper_name, _fn in ALL_SCRAPERS:
                # Web scrapers don't have a URL per se, use a placeholder
                scraper_url = f"scraper://{scraper_name.lower().replace(' ', '_')}"
                cur.execute(insert_sql, ('web_scraper', scraper_name, scraper_url,
                                         'mixed', 2, 'seed'))
                if cur.rowcount and cur.rowcount > 0:
                    inserted += 1

        conn.commit()
        log.info(f"Seeded source registry: {inserted} new sources inserted.")
    except Exception as e:
        log.error(f"Failed to seed source registry: {e}")
        if conn:
            try:
                conn.rollback()
            except Exception:
                pass
    finally:
        if conn:
            release_db_connection(conn)
    return inserted


def _derive_feed_name(url, category):
    """Derive a human-readable name from a feed URL."""
    from urllib.parse import urlparse
    parsed = urlparse(url)
    domain = parsed.hostname or ''

    # Remove www prefix
    if domain.startswith('www.'):
        domain = domain[4:]

    # Known mappings for cleaner names
    name_map = {
        'feeds.reuters.com': 'Reuters Business',
        'feeds.bloomberg.com': 'Bloomberg Markets',
        'search.cnbc.com': 'CNBC Top Stories',
        'feeds.a.dj.com': 'Dow Jones Markets',
        'feeds.bbci.co.uk': 'BBC Business',
        'feeds.arstechnica.com': 'Ars Technica',
        'feeds.marketwatch.com': 'MarketWatch',
        'news.google.com': f'Google News ({category})',
        'apnews.com': 'AP News Business',
    }
    if domain in name_map:
        return name_map[domain]

    # Strip common TLDs and capitalize
    for suffix in ['.com', '.co', '.org', '.net', '.xml', '.io']:
        domain = domain.replace(suffix, '')
    parts = domain.split('.')
    name = ' '.join(p.capitalize() for p in parts)

    # Append category hint for disambiguation
    path_hint = parsed.path.strip('/').split('/')[-1] if parsed.path.strip('/') else ''
    if path_hint and path_hint not in ('rss', 'feed', 'rss.xml', 'feed.xml', 'atom.xml'):
        path_clean = path_hint.replace('.xml', '').replace('.rss', '').replace('-', ' ')
        if len(path_clean) < 30:
            name = f"{name} ({path_clean})"

    return name


def _derive_tier(category):
    """Derive a default tier from feed category."""
    premium = {'regulatory', 'crypto'}
    standard = {'financial', 'wire', 'european', 'press_release', 'asia'}
    # experimental: 'google_news', 'sector', 'kol', 'ipo', 'ai', 'ai_research', 'tech'
    if category in premium:
        return 1
    if category in standard:
        return 2
    return 3


def load_rss_feeds_from_registry():
    """Load RSS feeds in the format expected by news_data._fetch_rss_feeds().

    Returns list of dicts: [{'url': str, 'category': str, 'source_id': int, 'source_name': str}]
    Falls back to hardcoded RSS_FEEDS if registry is empty.
    """
    sources = load_active_sources(source_type='rss')
    if not sources:
        return None  # Signal to caller to use hardcoded fallback

    feeds = []
    for src in sources:
        feeds.append({
            'url': src['source_url'],
            'category': src.get('category', 'unknown'),
            'source_id': src['id'],
            'source_name': src['source_name'],
        })
    return feeds


def load_web_scrapers_from_registry():
    """Load web scraper names from the registry.

    Returns list of source_name strings, or None if registry is empty.
    """
    sources = load_active_sources(source_type='web_scraper')
    if not sources:
        return None
    return [src['source_name'] for src in sources]
