"""Lookup helper: most recent bearish Gemini assessment for a symbol.

Used by `position_monitor` when a trailing stop fires — if a recent
bearish assessment exists, the exit gets tagged
`trailing_stop_analyst_concur` so attribution can later distinguish
"trailing did its job catching the same news the analyst saw" from
"trailing cut a winner short on noise".

Sync function (sqlite call); callers in async paths should wrap with
`asyncio.to_thread`.
"""

from datetime import datetime, timedelta, timezone

from src.database import _cursor, get_db_connection, release_db_connection
from src.logger import log


def get_recent_bearish_assessment(symbol: str, hours: int = 8) -> dict | None:
    """Return the most recent bearish assessment within the window, or None.

    Returns None on any DB error so the caller falls back to plain
    'trailing_stop' tagging — never lets a transient DB failure block
    the trailing stop SELL itself.
    """
    if not symbol:
        return None
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    cutoff_str = cutoff.strftime("%Y-%m-%d %H:%M:%S")
    conn = None
    try:
        conn = get_db_connection()
        with _cursor(conn) as cur:
            cur.execute(
                "SELECT id, symbol, direction, confidence, created_at "
                "FROM gemini_assessments "
                "WHERE symbol = ? AND direction = 'bearish' AND created_at >= ? "
                "ORDER BY created_at DESC LIMIT 1",
                (symbol, cutoff_str),
            )
            row = cur.fetchone()
            if row is None:
                return None
            # Row may be sqlite3.Row, tuple, or dict depending on cursor.
            try:
                return dict(row)
            except (TypeError, ValueError):
                return {
                    "id": row[0], "symbol": row[1], "direction": row[2],
                    "confidence": row[3], "created_at": row[4],
                }
    except Exception as e:
        log.debug(f"get_recent_bearish_assessment({symbol}) failed: {e}")
        return None
    finally:
        if conn is not None:
            try:
                release_db_connection(conn)
            except Exception:
                pass
