import sqlite3
from datetime import datetime, timedelta, timezone

import psycopg2
from src.config import app_config
from src.database import get_db_connection, release_db_connection, _cursor
from src.logger import log


def _get_live_config():
    """Returns the live_trading config section with defaults."""
    return app_config.get('settings', {}).get('live_trading', {})


def check_circuit_breaker(balance, daily_pnl, recent_trades):
    """
    Checks all circuit breaker conditions.

    Args:
        balance: Current USDT balance (float).
        daily_pnl: Sum of today's closed-trade PnL (float).
        recent_trades: List of recent closed trades (dicts with 'pnl' key),
                       ordered newest-first.

    Returns:
        (is_tripped, reason): Tuple of (bool, str). If tripped, reason explains why.
    """
    config = _get_live_config()
    initial_capital = config.get('initial_capital', 100.0)

    # 1. Cooldown check (must be first — overrides everything during cooldown)
    cooldown_hours = config.get('cooldown_hours', 24)
    if is_in_cooldown(cooldown_hours):
        return True, f"Cooldown active (waiting {cooldown_hours}h after last circuit breaker event)"

    # 2. Balance floor — absolute minimum
    balance_floor = config.get('balance_floor_usd', 70.0)
    if balance < balance_floor:
        reason = f"Balance ${balance:.2f} below floor ${balance_floor:.2f}"
        record_circuit_breaker_event('balance_floor', reason)
        return True, reason

    # 3. Daily loss limit
    daily_loss_limit_pct = config.get('daily_loss_limit_pct', 0.10)
    daily_loss_limit = initial_capital * daily_loss_limit_pct
    if daily_pnl <= -daily_loss_limit:
        reason = f"Daily loss ${daily_pnl:.2f} exceeds limit -${daily_loss_limit:.2f} ({daily_loss_limit_pct*100:.0f}%)"
        record_circuit_breaker_event('daily_loss', reason)
        return True, reason

    # 4. Max drawdown from peak
    max_drawdown_pct = config.get('max_drawdown_pct', 0.25)
    peak_balance = _get_peak_balance(initial_capital)
    drawdown_threshold = peak_balance * (1 - max_drawdown_pct)
    if balance <= drawdown_threshold:
        reason = (f"Balance ${balance:.2f} hit max drawdown {max_drawdown_pct*100:.0f}% "
                  f"from peak ${peak_balance:.2f} (threshold ${drawdown_threshold:.2f})")
        record_circuit_breaker_event('max_drawdown', reason)
        return True, reason

    # 5. Consecutive losses
    max_consecutive = config.get('max_consecutive_losses', 3)
    if len(recent_trades) >= max_consecutive:
        last_n = recent_trades[:max_consecutive]
        if all(t.get('pnl', 0) < 0 for t in last_n):
            reason = f"Last {max_consecutive} trades were all losses"
            record_circuit_breaker_event('consecutive_losses', reason)
            return True, reason

    return False, ""


def _get_peak_balance(initial_capital):
    """
    Returns the highest balance ever observed.
    Approximated as initial_capital + max cumulative PnL from trade history.
    """
    conn = None
    try:
        conn = get_db_connection()
        is_pg = isinstance(conn, psycopg2.extensions.connection)
        with _cursor(conn) as cursor:
            # Get running total of PnL ordered by exit time, find the max cumulative
            if is_pg:
                query = """
                    SELECT COALESCE(MAX(running_pnl), 0) FROM (
                        SELECT SUM(pnl) OVER (ORDER BY exit_timestamp) AS running_pnl
                        FROM trades WHERE status = 'CLOSED' AND pnl IS NOT NULL
                    ) sub
                """
            else:
                # SQLite doesn't support window functions in older versions,
                # but modern SQLite (3.25+) does
                query = """
                    SELECT COALESCE(MAX(running_pnl), 0) FROM (
                        SELECT SUM(pnl) OVER (ORDER BY exit_timestamp) AS running_pnl
                        FROM trades WHERE status = 'CLOSED' AND pnl IS NOT NULL
                    )
                """
            cursor.execute(query)
            max_cumulative_pnl = float(cursor.fetchone()[0])
        return initial_capital + max(0, max_cumulative_pnl)
    except Exception as e:
        log.warning(f"Could not compute peak balance: {e}")
        return initial_capital
    finally:
        release_db_connection(conn)


def is_in_cooldown(cooldown_hours=None):
    """Checks if a circuit breaker cooldown is currently active."""
    if cooldown_hours is None:
        cooldown_hours = _get_live_config().get('cooldown_hours', 24)
    conn = None
    try:
        conn = get_db_connection()
        is_pg = isinstance(conn, psycopg2.extensions.connection)
        with _cursor(conn) as cursor:
            if is_pg:
                query = """
                    SELECT COUNT(*) FROM circuit_breaker_events
                    WHERE triggered_at >= NOW() - INTERVAL '%s hours'
                    AND resolved_at IS NULL
                """
                cursor.execute(query, (cooldown_hours,))
            else:
                query = """
                    SELECT COUNT(*) FROM circuit_breaker_events
                    WHERE triggered_at >= datetime('now', ? || ' hours')
                    AND resolved_at IS NULL
                """
                cursor.execute(query, (f'-{cooldown_hours}',))
            count = cursor.fetchone()[0]
        return count > 0
    except Exception as e:
        log.warning(f"Could not check cooldown status: {e}")
        # Fail safe: assume cooldown is active if we can't check
        return True
    finally:
        release_db_connection(conn)


def record_circuit_breaker_event(event_type, details):
    """Records a circuit breaker event to the database."""
    conn = None
    try:
        conn = get_db_connection()
        is_pg = isinstance(conn, psycopg2.extensions.connection)
        with _cursor(conn) as cursor:
            query = (
                "INSERT INTO circuit_breaker_events (event_type, details) VALUES (%s, %s)"
                if is_pg else
                "INSERT INTO circuit_breaker_events (event_type, details) VALUES (?, ?)"
            )
            cursor.execute(query, (event_type, details))
        conn.commit()
        log.warning(f"Circuit breaker triggered: [{event_type}] {details}")
    except (sqlite3.Error, psycopg2.Error) as e:
        log.error(f"Failed to record circuit breaker event: {e}")
        if conn:
            conn.rollback()
    finally:
        release_db_connection(conn)


def get_circuit_breaker_status():
    """Returns the current circuit breaker status for Telegram display."""
    config = _get_live_config()
    cooldown_hours = config.get('cooldown_hours', 24)
    in_cooldown = is_in_cooldown(cooldown_hours)

    conn = None
    last_event = None
    try:
        conn = get_db_connection()
        is_pg = isinstance(conn, psycopg2.extensions.connection)
        with _cursor(conn) as cursor:
            query = (
                "SELECT event_type, details, triggered_at FROM circuit_breaker_events "
                "ORDER BY triggered_at DESC LIMIT 1"
            )
            cursor.execute(query)
            row = cursor.fetchone()
            if row:
                last_event = dict(row)
    except Exception as e:
        log.warning(f"Could not fetch circuit breaker status: {e}")
    finally:
        release_db_connection(conn)

    return {
        'in_cooldown': in_cooldown,
        'cooldown_hours': cooldown_hours,
        'last_event': last_event,
        'balance_floor': config.get('balance_floor_usd', 70.0),
        'daily_loss_limit_pct': config.get('daily_loss_limit_pct', 0.10),
        'max_drawdown_pct': config.get('max_drawdown_pct', 0.25),
        'max_consecutive_losses': config.get('max_consecutive_losses', 3),
    }


def get_daily_pnl(asset_type=None):
    """Returns the sum of PnL from trades closed today (UTC), optionally filtered by asset_type."""
    conn = None
    try:
        conn = get_db_connection()
        is_pg = isinstance(conn, psycopg2.extensions.connection)
        with _cursor(conn) as cursor:
            if asset_type:
                if is_pg:
                    query = """
                        SELECT COALESCE(SUM(pnl), 0) FROM trades
                        WHERE status = 'CLOSED' AND pnl IS NOT NULL
                        AND exit_timestamp >= CURRENT_DATE AND asset_type = %s
                    """
                else:
                    query = """
                        SELECT COALESCE(SUM(pnl), 0) FROM trades
                        WHERE status = 'CLOSED' AND pnl IS NOT NULL
                        AND exit_timestamp >= date('now') AND asset_type = ?
                    """
                cursor.execute(query, (asset_type,))
            else:
                if is_pg:
                    query = """
                        SELECT COALESCE(SUM(pnl), 0) FROM trades
                        WHERE status = 'CLOSED' AND pnl IS NOT NULL
                        AND exit_timestamp >= CURRENT_DATE
                    """
                else:
                    query = """
                        SELECT COALESCE(SUM(pnl), 0) FROM trades
                        WHERE status = 'CLOSED' AND pnl IS NOT NULL
                        AND exit_timestamp >= date('now')
                    """
                cursor.execute(query)
            return float(cursor.fetchone()[0])
    except Exception as e:
        log.warning(f"Could not compute daily PnL: {e}")
        return 0.0
    finally:
        release_db_connection(conn)


def get_recent_closed_trades(limit=5, asset_type=None):
    """Returns the most recent closed trades, newest first, optionally filtered by asset_type."""
    conn = None
    try:
        conn = get_db_connection()
        is_pg = isinstance(conn, psycopg2.extensions.connection)
        with _cursor(conn) as cursor:
            if asset_type:
                if is_pg:
                    query = """
                        SELECT * FROM trades WHERE status = 'CLOSED' AND pnl IS NOT NULL
                        AND asset_type = %s ORDER BY exit_timestamp DESC LIMIT %s
                    """
                else:
                    query = """
                        SELECT * FROM trades WHERE status = 'CLOSED' AND pnl IS NOT NULL
                        AND asset_type = ? ORDER BY exit_timestamp DESC LIMIT ?
                    """
                cursor.execute(query, (asset_type, limit))
            else:
                if is_pg:
                    query = """
                        SELECT * FROM trades WHERE status = 'CLOSED' AND pnl IS NOT NULL
                        ORDER BY exit_timestamp DESC LIMIT %s
                    """
                else:
                    query = """
                        SELECT * FROM trades WHERE status = 'CLOSED' AND pnl IS NOT NULL
                        ORDER BY exit_timestamp DESC LIMIT ?
                    """
                cursor.execute(query, (limit,))
            return [dict(row) for row in cursor.fetchall()]
    except Exception as e:
        log.warning(f"Could not fetch recent trades: {e}")
        return []
    finally:
        release_db_connection(conn)
