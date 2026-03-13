"""News velocity module — measures article flow and sentiment trends per symbol.

Pure DB queries against scraped_articles. No Gemini calls.
Used to gate the expensive Position Analyst Gemini call — skip when nothing is happening.
"""

import sqlite3

import psycopg2

from src.database import get_db_connection, release_db_connection, _cursor
from src.logger import log


def compute_news_velocity(symbol: str) -> dict:
    """Computes article counts and sentiment trends for a symbol across time windows.

    Returns:
        dict with keys:
        - articles_last_1h, articles_last_4h, articles_last_24h
        - avg_sentiment_1h, avg_sentiment_4h, avg_sentiment_24h
        - sentiment_trend: "improving" / "deteriorating" / "stable"
        - velocity_status: "accelerating" / "normal" / "quiet"
        - breaking_detected: True when velocity is "accelerating"
    """
    conn = None
    try:
        conn = get_db_connection()
        is_pg = isinstance(conn, psycopg2.extensions.connection)

        windows = [1, 4, 24]
        counts = {}
        sentiments = {}

        with _cursor(conn) as cursor:
            for hours in windows:
                if is_pg:
                    count_q = (
                        "SELECT COUNT(*) FROM scraped_articles "
                        "WHERE symbol = %s AND collected_at >= NOW() - INTERVAL '%s hours'"
                    )
                    cursor.execute(count_q, (symbol, hours))
                else:
                    count_q = (
                        "SELECT COUNT(*) FROM scraped_articles "
                        "WHERE symbol = ? AND collected_at >= datetime('now', ? || ' hours')"
                    )
                    cursor.execute(count_q, (symbol, f'-{hours}'))
                counts[hours] = cursor.fetchone()[0]

                # Average sentiment from Gemini scores only
                if is_pg:
                    sent_q = (
                        "SELECT AVG(gemini_score) FROM scraped_articles "
                        "WHERE symbol = %s AND collected_at >= NOW() - INTERVAL '%s hours' "
                        "AND gemini_score IS NOT NULL"
                    )
                    cursor.execute(sent_q, (symbol, hours))
                else:
                    sent_q = (
                        "SELECT AVG(gemini_score) FROM scraped_articles "
                        "WHERE symbol = ? AND collected_at >= datetime('now', ? || ' hours') "
                        "AND gemini_score IS NOT NULL"
                    )
                    cursor.execute(sent_q, (symbol, f'-{hours}'))
                row = cursor.fetchone()
                sentiments[hours] = row[0] if row and row[0] is not None else 0.0

        avg_1h = sentiments[1]
        avg_24h = sentiments[24]
        diff = avg_1h - avg_24h
        if diff > 0.15:
            sentiment_trend = "improving"
        elif diff < -0.15:
            sentiment_trend = "deteriorating"
        else:
            sentiment_trend = "stable"

        # Velocity: compare 1h rate to 24h average rate
        hourly_avg_24h = counts[24] / 24.0 if counts[24] > 0 else 0
        if counts[4] == 0:
            velocity_status = "quiet"
        elif counts[1] > 3 * hourly_avg_24h and counts[1] >= 2:
            velocity_status = "accelerating"
        else:
            velocity_status = "normal"

        return {
            'articles_last_1h': counts[1],
            'articles_last_4h': counts[4],
            'articles_last_24h': counts[24],
            'avg_sentiment_1h': round(avg_1h, 4),
            'avg_sentiment_4h': round(sentiments[4], 4),
            'avg_sentiment_24h': round(avg_24h, 4),
            'sentiment_trend': sentiment_trend,
            'velocity_status': velocity_status,
            'breaking_detected': velocity_status == "accelerating",
        }

    except (sqlite3.Error, psycopg2.Error) as e:
        log.error(f"Database error in compute_news_velocity for {symbol}: {e}", exc_info=True)
        return {
            'articles_last_1h': 0, 'articles_last_4h': 0, 'articles_last_24h': 0,
            'avg_sentiment_1h': 0.0, 'avg_sentiment_4h': 0.0, 'avg_sentiment_24h': 0.0,
            'sentiment_trend': 'stable', 'velocity_status': 'quiet',
            'breaking_detected': False,
        }
    finally:
        release_db_connection(conn)
