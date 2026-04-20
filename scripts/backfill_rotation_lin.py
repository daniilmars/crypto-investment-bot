#!/usr/bin/env python3
"""
One-shot backfill: populate SL/TP and attribution for the April-20 LIN
rotation entry that slipped in before the rotation-entry fix landed.

    Dry run (default):
        sudo docker exec -w /app crypto-bot python3 scripts/backfill_rotation_lin.py

    Execute:
        sudo docker exec -w /app crypto-bot python3 scripts/backfill_rotation_lin.py --execute

Idempotent — the UPDATE uses `WHERE dynamic_sl_pct IS NULL`, so a second run
is a no-op.
"""
import argparse
import sys
from datetime import datetime, timezone

sys.path.insert(0, '/app')
sys.path.insert(0, '.')

from src.config import app_config
from src.database import get_db_connection, release_db_connection, _cursor
from src.analysis.dynamic_risk import resolve_sl_tp_for_entry


TARGET_SYMBOL = 'LIN'


def _fetch_target(conn):
    """Return the LIN row we want to fix (or None if already backfilled)."""
    with _cursor(conn) as cur:
        cur.execute(
            "SELECT id, order_id, symbol, entry_price, quantity, "
            "entry_timestamp, trading_strategy "
            "FROM trades "
            "WHERE symbol = ? AND status = 'OPEN' AND dynamic_sl_pct IS NULL",
            (TARGET_SYMBOL,),
        )
        row = cur.fetchone()
    if not row:
        return None
    cols = ['id', 'order_id', 'symbol', 'entry_price', 'quantity',
            'entry_timestamp', 'trading_strategy']
    return dict(zip(cols, row)) if not isinstance(row, dict) else dict(row)


def _has_attribution(conn, order_id):
    with _cursor(conn) as cur:
        cur.execute(
            "SELECT id FROM signal_attribution WHERE trade_order_id = ? LIMIT 1",
            (order_id,))
        return cur.fetchone() is not None


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--execute', action='store_true',
                        help='Actually apply the UPDATE. Default is dry-run.')
    args = parser.parse_args()

    conn = get_db_connection()
    if not conn:
        print('ERROR: no DB connection', file=sys.stderr)
        return 1

    try:
        row = _fetch_target(conn)
        if row is None:
            print(f'No {TARGET_SYMBOL} row with NULL dynamic_sl_pct found — '
                  f'already backfilled or no matching trade.')
            return 0

        print(f'Target: id={row["id"]}  order_id={row["order_id"]}  '
              f'{row["symbol"]}  entry=${row["entry_price"]:.4f}  '
              f'qty={row["quantity"]:.4f}  opened={row["entry_timestamp"]}')

        # Resolve SL/TP via current ATR (or config fallback if no data).
        settings = app_config.get('settings', {})
        sl_pct, tp_pct, source = resolve_sl_tp_for_entry(
            row['symbol'],
            current_price=row['entry_price'],  # no cached current price — use entry
            asset_type='stock',
            settings=settings,
        )
        print(f'Resolved: SL={sl_pct:.4f} ({sl_pct*100:.2f}%)  '
              f'TP={tp_pct:.4f} ({tp_pct*100:.2f}%)  source={source}')

        already_attr = _has_attribution(conn, row['order_id'])
        print(f'Attribution row exists: {already_attr}')

        if not args.execute:
            print('\n[dry-run] Pass --execute to apply.')
            return 0

        # Apply UPDATE (guarded by IS NULL so re-runs are no-ops)
        with _cursor(conn) as cur:
            cur.execute(
                "UPDATE trades SET dynamic_sl_pct = ?, dynamic_tp_pct = ? "
                "WHERE id = ? AND dynamic_sl_pct IS NULL",
                (sl_pct, tp_pct, row['id']),
            )
            affected = cur.rowcount
        conn.commit()
        print(f'UPDATE affected {affected} row(s).')

        # Write attribution row if missing
        if not already_attr:
            try:
                from src.analysis.signal_attribution import (
                    link_attribution_to_order, record_signal_attribution,
                )
                signal = {
                    'symbol': row['symbol'],
                    'signal': 'BUY',
                    'current_price': row['entry_price'],
                    'reason': 'rotation entry — backfilled by script',
                    'trade_reason': 'rotation_pick_backfill',
                }
                gemini = {
                    'direction': None,
                    'confidence': None,
                    'catalyst_type': 'rotation_pick_backfill',
                }
                attr_id = record_signal_attribution(
                    signal, articles=[], gemini_assessment=gemini)
                if attr_id:
                    link_attribution_to_order(attr_id, row['order_id'])
                    print(f'Wrote attribution row #{attr_id} -> {row["order_id"]}')
            except Exception as e:
                print(f'WARN: attribution write failed: {e}')

        print(f'\nBackfill complete at {datetime.now(timezone.utc).isoformat(timespec="seconds")}')
        return 0
    finally:
        release_db_connection(conn)


if __name__ == '__main__':
    sys.exit(main())
