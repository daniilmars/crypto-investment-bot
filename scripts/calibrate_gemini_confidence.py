#!/usr/bin/env python3
"""
Run from inside the bot container:
    sudo docker exec -w /app crypto-bot python3 scripts/calibrate_gemini_confidence.py

Joins closed trades to their Gemini confidence via a 3-tier fallback chain
(signal_attribution → signals → gemini_assessments by symbol+timestamp window).
Bins by confidence bucket, computes Wilson CI, prints stratified tables, and
persists the results to gemini_calibration for drift detection over time.
"""

import argparse
import sys
from datetime import datetime, timezone

# Allow running from /app inside the container OR from repo root locally
sys.path.insert(0, '/app')
sys.path.insert(0, '.')

from src.analysis.gemini_calibration import (
    bucketize, render_table, stats_to_db_rows,
)
from src.database import get_db_connection, release_db_connection, _cursor


def fetch_calibration_rows(conn) -> list[dict]:
    """Pull closed trades + best-effort joined Gemini confidence.

    SQLite path uses correlated subqueries (LATERAL not supported).
    """
    sql = """
    SELECT
        t.order_id,
        t.symbol,
        t.pnl,
        t.entry_timestamp,
        t.exit_timestamp,
        t.exit_reason,
        t.trading_strategy,
        sa.gemini_confidence AS conf_primary,
        sa.gemini_direction  AS dir_primary,
        (SELECT confidence FROM gemini_assessments g
         WHERE g.symbol = t.symbol
           AND g.created_at <= t.entry_timestamp
           AND g.created_at >= datetime(t.entry_timestamp, '-30 minutes')
         ORDER BY g.created_at DESC LIMIT 1) AS conf_fallback,
        (SELECT direction FROM gemini_assessments g
         WHERE g.symbol = t.symbol
           AND g.created_at <= t.entry_timestamp
           AND g.created_at >= datetime(t.entry_timestamp, '-30 minutes')
         ORDER BY g.created_at DESC LIMIT 1) AS dir_fallback
    FROM trades t
    LEFT JOIN signal_attribution sa ON sa.trade_order_id = t.order_id
    WHERE t.status = 'CLOSED' AND t.pnl IS NOT NULL
    """
    rows: list[dict] = []
    with _cursor(conn) as cur:
        cur.execute(sql)
        cols = [d[0] for d in cur.description]
        for r in cur.fetchall():
            row = dict(zip(cols, r)) if not isinstance(r, dict) else dict(r)
            # Pick the best available confidence (primary > fallback)
            conf = row.get("conf_primary")
            direction = row.get("dir_primary")
            if conf is None:
                conf = row.get("conf_fallback")
                direction = row.get("dir_fallback")
            row["conf"] = conf
            row["direction"] = direction
            rows.append(row)
    return rows


def persist_results(conn, all_results: list[tuple[str, dict]]) -> int:
    """Write each (stratify_label, stats_by_group) into gemini_calibration."""
    import psycopg2
    is_pg = isinstance(conn, psycopg2.extensions.connection)
    ph = "%s" if is_pg else "?"
    sql = (
        f"INSERT INTO gemini_calibration "
        f"(stratify_by, stratify_value, conf_bucket, n, wins, win_rate, "
        f"avg_pnl, ci_low, ci_high) VALUES "
        f"({ph},{ph},{ph},{ph},{ph},{ph},{ph},{ph},{ph})"
    )
    n_written = 0
    with _cursor(conn) as cur:
        for label, stats_by_group in all_results:
            rows = stats_to_db_rows(stats_by_group, label)
            for row in rows:
                cur.execute(sql, row)
                n_written += 1
    conn.commit()
    return n_written


def run_calibration(persist: bool = True, small_n: int = 10,
                    print_tables: bool = False) -> dict:
    """Compute calibration buckets and optionally persist to gemini_calibration.

    Extracted from main() so the background loop in main.py can call it
    directly (see calibration_loop). Returns a summary dict for logging:
    {'rows': N, 'with_conf': N, 'persisted': N, 'skipped': bool}.
    """
    conn = get_db_connection()
    if not conn:
        return {'error': 'no_db_connection', 'rows': 0, 'persisted': 0}

    try:
        rows = fetch_calibration_rows(conn)
    finally:
        release_db_connection(conn)

    with_conf = sum(1 for r in rows if r['conf'] is not None)
    if not rows:
        return {'rows': 0, 'with_conf': 0, 'persisted': 0, 'skipped': True}

    if print_tables:
        print(f"Loaded {len(rows)} closed trades  ({with_conf} "
              f"with Gemini confidence,  {len(rows) - with_conf} without)")
        print(f"Computed at {datetime.now(timezone.utc).isoformat(timespec='seconds')}")
        print()

    all_results: list[tuple[str, dict]] = []
    for stratify in (None, "direction", "trading_strategy", "exit_reason"):
        stats = bucketize(rows, stratify_key=stratify)
        label = stratify or "overall"
        if not stats:
            continue
        if print_tables:
            print(render_table(stats, stratify_label=label,
                               small_n_threshold=small_n))
        all_results.append((label, stats))

    n_written = 0
    if persist:
        conn = get_db_connection()
        try:
            n_written = persist_results(conn, all_results)
        finally:
            release_db_connection(conn)

    return {'rows': len(rows), 'with_conf': with_conf,
            'persisted': n_written, 'skipped': False}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--no-persist", action="store_true",
                        help="Print tables only; don't write to gemini_calibration.")
    parser.add_argument("--small-n", type=int, default=10,
                        help="Flag buckets with fewer than this many trades.")
    args = parser.parse_args()

    result = run_calibration(persist=not args.no_persist,
                              small_n=args.small_n, print_tables=True)
    if result.get('error'):
        print(f"ERROR: {result['error']}", file=sys.stderr)
        return 1
    if result['skipped']:
        print("No closed trades found. Nothing to calibrate.")
        return 0
    if args.no_persist:
        print("\n(--no-persist set; skipped DB write)")
    else:
        print(f"\nPersisted {result['persisted']} bucket rows to gemini_calibration.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
