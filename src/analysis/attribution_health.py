"""Attribution coverage health — daily snapshots for trajectory tracking.

Mirrors the L8 metrics /check-news surfaces, but persists them to
`attribution_coverage_history` so we can see the coverage curve recover
over time (not just a point-in-time diagnostic).

Called by a daily background loop in main.py. Idempotent: multiple
inserts per day are allowed — drilldowns pick the most recent per day.
"""

import psycopg2

from src.database import _cursor, get_db_connection, release_db_connection
from src.logger import log


DEFAULT_WINDOWS = (7, 30)


def _ph(is_pg: bool) -> str:
    return "%s" if is_pg else "?"


def compute_coverage(conn, window_days: int) -> dict:
    """Compute L8 coverage metrics for a single window. Returns a dict
    with raw counts + pct, suitable for persistence."""
    is_pg = isinstance(conn, psycopg2.extensions.connection)
    if is_pg:
        window_clause = "created_at > NOW() - INTERVAL '%s days'"
        params = (window_days,)
    else:
        window_clause = f"created_at > datetime('now', '-{int(window_days)} days')"
        params = ()

    sql = (
        "SELECT COUNT(*) AS total, "
        "  SUM(CASE WHEN source_names IS NOT NULL AND source_names!='' "
        "        AND source_names!='[]' THEN 1 ELSE 0 END) AS with_sources, "
        "  SUM(CASE WHEN article_hashes IS NOT NULL AND article_hashes!='' "
        "        AND article_hashes!='[]' THEN 1 ELSE 0 END) AS with_hashes, "
        "  SUM(CASE WHEN trade_order_id IS NOT NULL THEN 1 ELSE 0 END) AS with_trade, "
        "  SUM(CASE WHEN resolved_at IS NOT NULL THEN 1 ELSE 0 END) AS with_resolution "
        f"FROM signal_attribution WHERE {window_clause}"
    )
    with _cursor(conn) as cur:
        cur.execute(sql, params)
        row = cur.fetchone()
    # Row may be sqlite3.Row, tuple, or dict — normalize
    if hasattr(row, "keys"):
        d = dict(row)
    else:
        d = {"total": row[0], "with_sources": row[1],
             "with_hashes": row[2], "with_trade": row[3],
             "with_resolution": row[4]}
    total = int(d.get("total") or 0)
    with_sources = int(d.get("with_sources") or 0)
    coverage_pct = (with_sources / total * 100.0) if total else 0.0
    return {
        "window_days": window_days,
        "total_attributions": total,
        "with_sources": with_sources,
        "with_hashes": int(d.get("with_hashes") or 0),
        "with_trade": int(d.get("with_trade") or 0),
        "with_resolution": int(d.get("with_resolution") or 0),
        "coverage_pct_sources": round(coverage_pct, 2),
    }


def save_snapshot(conn, snap: dict) -> None:
    """Insert a single coverage snapshot into attribution_coverage_history."""
    is_pg = isinstance(conn, psycopg2.extensions.connection)
    ph = _ph(is_pg)
    sql = (
        f"INSERT INTO attribution_coverage_history "
        f"(window_days, total_attributions, with_sources, with_hashes, "
        f"with_trade, with_resolution, coverage_pct_sources) "
        f"VALUES ({ph}, {ph}, {ph}, {ph}, {ph}, {ph}, {ph})"
    )
    with _cursor(conn) as cur:
        cur.execute(sql, (
            snap["window_days"], snap["total_attributions"],
            snap["with_sources"], snap["with_hashes"],
            snap["with_trade"], snap["with_resolution"],
            snap["coverage_pct_sources"],
        ))
    conn.commit()


def compute_and_save_coverage(windows=DEFAULT_WINDOWS) -> dict:
    """Compute snapshots for each window and persist. Never raises —
    returns {'error': '...'} on failure so the background loop can
    log and continue.

    Returns:
        {'snapshots': [{...}, {...}], 'persisted': N}
    """
    conn = get_db_connection()
    if not conn:
        return {"error": "no_db_connection", "persisted": 0, "snapshots": []}
    try:
        snapshots = []
        for w in windows:
            try:
                snap = compute_coverage(conn, w)
                save_snapshot(conn, snap)
                snapshots.append(snap)
            except Exception as e:
                log.warning(f"attribution coverage snapshot (w={w}d) failed: {e}")
                try:
                    conn.rollback()
                except Exception:
                    pass
        return {"persisted": len(snapshots), "snapshots": snapshots}
    finally:
        release_db_connection(conn)


def get_recent_trajectory(window_days: int = 7, limit: int = 14) -> list[dict]:
    """Read the last N snapshots for a given window_days (newest-first).

    Used by /check-news L8 drilldown to show the recovery curve.
    """
    conn = get_db_connection()
    if not conn:
        return []
    try:
        is_pg = isinstance(conn, psycopg2.extensions.connection)
        ph = _ph(is_pg)
        sql = (
            "SELECT computed_at, total_attributions, with_sources, "
            "       coverage_pct_sources "
            "FROM attribution_coverage_history "
            f"WHERE window_days = {ph} "
            f"ORDER BY computed_at DESC LIMIT {ph}"
        )
        with _cursor(conn) as cur:
            cur.execute(sql, (window_days, limit))
            rows = cur.fetchall()
        out = []
        for r in rows:
            if hasattr(r, "keys"):
                out.append(dict(r))
            else:
                out.append({
                    "computed_at": r[0],
                    "total_attributions": r[1],
                    "with_sources": r[2],
                    "coverage_pct_sources": r[3],
                })
        return out
    except Exception as e:
        log.debug(f"get_recent_trajectory failed: {e}")
        return []
    finally:
        release_db_connection(conn)
