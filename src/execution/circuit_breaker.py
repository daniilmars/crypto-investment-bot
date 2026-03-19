import sqlite3

import psycopg2
from src.config import app_config
from src.database import get_db_connection, release_db_connection, _cursor
from src.logger import log


_session_peaks: dict[str, float] = {}  # {asset_type: peak_balance}


def _get_live_config():
    """Returns the live_trading config section with defaults."""
    return app_config.get('settings', {}).get('live_trading', {})


def check_circuit_breaker(balance, daily_pnl, recent_trades, asset_type='crypto',
                          current_prices=None, cb_config=None):
    """
    Checks all circuit breaker conditions for a specific asset type or strategy.

    Args:
        balance: Current USDT balance (float).
        daily_pnl: Sum of today's closed-trade PnL (float), should include unrealized.
        recent_trades: List of recent closed trades (dicts with 'pnl' key),
                       ordered newest-first.
        asset_type: Free-text identifier for DB isolation (e.g. 'crypto', 'stock',
                    'auto', 'momentum', 'conservative').
        current_prices: {symbol: float} — current market prices for unrealized PnL.
        cb_config: Optional per-strategy circuit breaker config dict. When provided,
                   overrides thresholds from the global live_trading config. Expected
                   keys: initial_capital, cooldown_hours, balance_floor_usd,
                   daily_loss_limit_pct, max_drawdown_pct, max_consecutive_losses.

    Returns:
        (is_tripped, reason): Tuple of (bool, str). If tripped, reason explains why.
    """
    config = _get_live_config()

    # Determine initial capital — cb_config overrides asset_type-based lookup
    if cb_config and 'initial_capital' in cb_config:
        initial_capital = cb_config['initial_capital']
    else:
        settings = app_config.get('settings', {})
        if asset_type == 'stock':
            stock_cfg = settings.get('stock_trading', {})
            initial_capital = stock_cfg.get('paper_trading_initial_capital',
                                            settings.get('paper_trading_initial_capital', 10000.0))
        elif asset_type == 'auto':
            auto_cfg = settings.get('auto_trading', {})
            initial_capital = auto_cfg.get('paper_trading_initial_capital', 10000.0)
        else:
            mode = config.get('mode', 'live')
            if settings.get('paper_trading', True) or mode == 'testnet':
                initial_capital = settings.get('paper_trading_initial_capital', 10000.0)
            else:
                initial_capital = config.get('initial_capital', 100.0)

    # Helper to read threshold: cb_config first, then global config
    def _threshold(key, default):
        if cb_config and key in cb_config:
            return cb_config[key]
        return config.get(key, default)

    # 1. Cooldown check (must be first — overrides everything during cooldown)
    cooldown_hours = _threshold('cooldown_hours', 24)
    if is_in_cooldown(cooldown_hours, asset_type=asset_type):
        return True, f"Cooldown active (waiting {cooldown_hours}h after last {asset_type} circuit breaker event)"

    # 2. Balance floor — absolute minimum
    balance_floor = _threshold('balance_floor_usd', 70.0)
    if balance < balance_floor:
        reason = f"Balance ${balance:.2f} below floor ${balance_floor:.2f}"
        record_circuit_breaker_event('balance_floor', reason, asset_type=asset_type)
        return True, reason

    # 3. Daily loss limit
    daily_loss_limit_pct = _threshold('daily_loss_limit_pct', 0.10)
    daily_loss_limit = initial_capital * daily_loss_limit_pct
    if daily_pnl <= -daily_loss_limit:
        reason = f"Daily loss ${daily_pnl:.2f} exceeds limit -${daily_loss_limit:.2f} ({daily_loss_limit_pct*100:.0f}%)"
        record_circuit_breaker_event('daily_loss', reason, asset_type=asset_type)
        return True, reason

    # 4. Max drawdown from peak (include unrealized PnL in effective balance)
    max_drawdown_pct = _threshold('max_drawdown_pct', 0.25)
    peak_balance = _get_peak_balance(initial_capital, asset_type=asset_type)
    drawdown_threshold = peak_balance * (1 - max_drawdown_pct)
    unrealized = 0.0
    if current_prices:
        unrealized = get_unrealized_pnl(current_prices, asset_type=asset_type)
    effective_balance = balance + unrealized
    if effective_balance <= drawdown_threshold:
        reason = (f"Effective balance ${effective_balance:.2f} (realized ${balance:.2f} + "
                  f"unrealized ${unrealized:.2f}) hit max drawdown {max_drawdown_pct*100:.0f}% "
                  f"from peak ${peak_balance:.2f} (threshold ${drawdown_threshold:.2f})")
        record_circuit_breaker_event('max_drawdown', reason, asset_type=asset_type)
        return True, reason

    # 5. Consecutive losses
    # Only trigger if the latest trade is newer than the last consecutive_losses event,
    # otherwise we'd re-trigger on the same stale trades after every cooldown expiry.
    max_consecutive = _threshold('max_consecutive_losses', 3)
    if len(recent_trades) >= max_consecutive:
        last_n = recent_trades[:max_consecutive]
        if all(t.get('pnl', 0) < 0 for t in last_n):
            latest_trade_ts = recent_trades[0].get('exit_timestamp', '')
            last_event_ts = _get_last_event_timestamp('consecutive_losses', asset_type)
            if last_event_ts and str(latest_trade_ts) <= str(last_event_ts):
                # Same trades already triggered a cooldown — don't re-trigger
                pass
            else:
                reason = f"Last {max_consecutive} trades were all losses"
                record_circuit_breaker_event('consecutive_losses', reason, asset_type=asset_type)
                return True, reason

    return False, ""


def _get_peak_balance(initial_capital, asset_type='crypto'):
    """
    Returns the highest balance ever observed for a specific asset type.
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
                        AND asset_type = %s
                    ) sub
                """
            else:
                query = """
                    SELECT COALESCE(MAX(running_pnl), 0) FROM (
                        SELECT SUM(pnl) OVER (ORDER BY exit_timestamp) AS running_pnl
                        FROM trades WHERE status = 'CLOSED' AND pnl IS NOT NULL
                        AND asset_type = ?
                    )
                """
            cursor.execute(query, (asset_type,))
            max_cumulative_pnl = float(cursor.fetchone()[0])
        historical_peak = initial_capital + max(0, max_cumulative_pnl)
        session_peak = _session_peaks.get(asset_type, 0)
        return max(historical_peak, session_peak)
    except Exception as e:
        log.warning(f"Could not compute peak balance: {e}")
        return initial_capital
    finally:
        release_db_connection(conn)


def is_in_cooldown(cooldown_hours=None, asset_type=None):
    """Checks if a circuit breaker cooldown is currently active for the given asset type."""
    if cooldown_hours is None:
        cooldown_hours = _get_live_config().get('cooldown_hours', 24)
    conn = None
    try:
        conn = get_db_connection()
        is_pg = isinstance(conn, psycopg2.extensions.connection)
        with _cursor(conn) as cursor:
            if is_pg:
                if asset_type:
                    query = """
                        SELECT COUNT(*) FROM circuit_breaker_events
                        WHERE triggered_at >= NOW() - INTERVAL '%s hours'
                        AND resolved_at IS NULL AND asset_type = %s
                    """
                    cursor.execute(query, (cooldown_hours, asset_type))
                else:
                    query = """
                        SELECT COUNT(*) FROM circuit_breaker_events
                        WHERE triggered_at >= NOW() - INTERVAL '%s hours'
                        AND resolved_at IS NULL
                    """
                    cursor.execute(query, (cooldown_hours,))
            else:
                if asset_type:
                    query = """
                        SELECT COUNT(*) FROM circuit_breaker_events
                        WHERE triggered_at >= datetime('now', ? || ' hours')
                        AND resolved_at IS NULL AND asset_type = ?
                    """
                    cursor.execute(query, (f'-{cooldown_hours}', asset_type))
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


def _get_last_event_timestamp(event_type, asset_type=None):
    """Returns the triggered_at timestamp of the most recent event of this type."""
    conn = None
    try:
        conn = get_db_connection()
        is_pg = isinstance(conn, psycopg2.extensions.connection)
        with _cursor(conn) as cursor:
            if asset_type:
                query = (
                    "SELECT triggered_at FROM circuit_breaker_events "
                    "WHERE event_type = %s AND asset_type = %s ORDER BY triggered_at DESC LIMIT 1"
                    if is_pg else
                    "SELECT triggered_at FROM circuit_breaker_events "
                    "WHERE event_type = ? AND asset_type = ? ORDER BY triggered_at DESC LIMIT 1"
                )
                cursor.execute(query, (event_type, asset_type))
            else:
                query = (
                    "SELECT triggered_at FROM circuit_breaker_events "
                    "WHERE event_type = %s ORDER BY triggered_at DESC LIMIT 1"
                    if is_pg else
                    "SELECT triggered_at FROM circuit_breaker_events "
                    "WHERE event_type = ? ORDER BY triggered_at DESC LIMIT 1"
                )
                cursor.execute(query, (event_type,))
            row = cursor.fetchone()
            return row[0] if row else None
    except Exception:
        return None
    finally:
        release_db_connection(conn)


def record_circuit_breaker_event(event_type, details, asset_type='crypto'):
    """Records a circuit breaker event to the database."""
    conn = None
    try:
        conn = get_db_connection()
        is_pg = isinstance(conn, psycopg2.extensions.connection)
        with _cursor(conn) as cursor:
            query = (
                "INSERT INTO circuit_breaker_events (event_type, details, asset_type) VALUES (%s, %s, %s)"
                if is_pg else
                "INSERT INTO circuit_breaker_events (event_type, details, asset_type) VALUES (?, ?, ?)"
            )
            cursor.execute(query, (event_type, details, asset_type))
        conn.commit()
        log.warning(f"Circuit breaker triggered [{asset_type}]: [{event_type}] {details}")
    except (sqlite3.Error, psycopg2.Error) as e:
        log.error(f"Failed to record circuit breaker event: {e}")
        if conn:
            conn.rollback()
    finally:
        release_db_connection(conn)


def get_circuit_breaker_status(asset_type=None):
    """Returns the current circuit breaker status for Telegram display."""
    config = _get_live_config()
    cooldown_hours = config.get('cooldown_hours', 24)
    in_cooldown = is_in_cooldown(cooldown_hours, asset_type=asset_type)

    conn = None
    last_event = None
    try:
        conn = get_db_connection()
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


def update_session_peak(balance: float, asset_type: str = 'crypto') -> float:
    """Update session peak if balance exceeds current peak. Persists to DB.

    Returns the current peak balance for this asset type.
    """
    current_peak = _session_peaks.get(asset_type, 0)
    if balance > current_peak:
        _session_peaks[asset_type] = balance
        _persist_session_peak(asset_type, balance)
        return balance
    return current_peak


def load_session_peaks() -> None:
    """Load session peaks from DB into _session_peaks. Called at startup."""
    conn = None
    try:
        conn = get_db_connection()
        is_pg = isinstance(conn, psycopg2.extensions.connection)
        with _cursor(conn) as cursor:
            cursor.execute("SELECT asset_type, peak_balance FROM session_peaks")
            for row in cursor.fetchall():
                if is_pg or hasattr(row, 'keys'):
                    _session_peaks[row['asset_type']] = float(row['peak_balance'])
                else:
                    _session_peaks[row[0]] = float(row[1])
        log.info(f"Loaded session peaks: {_session_peaks}")
    except Exception as e:
        log.warning(f"Could not load session peaks: {e}")
    finally:
        release_db_connection(conn)


def _persist_session_peak(asset_type: str, peak: float) -> None:
    """Upsert session peak to DB."""
    conn = None
    try:
        conn = get_db_connection()
        is_pg = isinstance(conn, psycopg2.extensions.connection)
        with _cursor(conn) as cursor:
            if is_pg:
                cursor.execute(
                    "INSERT INTO session_peaks (asset_type, peak_balance, observed_at) "
                    "VALUES (%s, %s, NOW()) "
                    "ON CONFLICT (asset_type) DO UPDATE SET peak_balance = %s, observed_at = NOW()",
                    (asset_type, peak, peak))
            else:
                cursor.execute(
                    "INSERT OR REPLACE INTO session_peaks (asset_type, peak_balance, observed_at) "
                    "VALUES (?, ?, datetime('now'))",
                    (asset_type, peak))
        conn.commit()
    except Exception as e:
        log.warning(f"Could not persist session peak: {e}")
        if conn:
            conn.rollback()
    finally:
        release_db_connection(conn)


def get_unrealized_pnl(current_prices: dict, asset_type: str | None = None) -> float:
    """Calculates total unrealized P&L across open positions.

    Args:
        current_prices: {symbol: float} — current market prices.
        asset_type: Optional filter — 'crypto' or 'stock'. None = all.

    Returns:
        Total unrealized P&L in USD. Returns 0.0 on any error (fail-safe).
    """
    try:
        from src.execution.binance_trader import get_open_positions
        positions = get_open_positions(asset_type=asset_type)
        total = 0.0
        for pos in positions:
            if pos.get('status') != 'OPEN':
                continue
            symbol = pos.get('symbol')
            entry_price = pos.get('entry_price', 0)
            quantity = pos.get('quantity', 0)
            current_price = current_prices.get(symbol)
            if current_price is None or entry_price <= 0:
                continue
            total += (current_price - entry_price) * quantity
        return total
    except Exception as e:
        log.warning(f"Could not compute unrealized PnL: {e}")
        return 0.0


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


def resolve_stale_circuit_breaker_events():
    """Resolves circuit breaker events older than the cooldown period that are still unresolved.

    This prevents stale events from permanently blocking trading after a restart.
    """
    config = _get_live_config()
    cooldown_hours = config.get('cooldown_hours', 24)
    conn = None
    try:
        conn = get_db_connection()
        is_pg = isinstance(conn, psycopg2.extensions.connection)
        with _cursor(conn) as cursor:
            if is_pg:
                query = """
                    UPDATE circuit_breaker_events
                    SET resolved_at = NOW()
                    WHERE resolved_at IS NULL
                    AND triggered_at < NOW() - INTERVAL '%s hours'
                """
            else:
                query = """
                    UPDATE circuit_breaker_events
                    SET resolved_at = datetime('now')
                    WHERE resolved_at IS NULL
                    AND triggered_at < datetime('now', ? || ' hours')
                """
                cooldown_hours = f'-{cooldown_hours}'
            cursor.execute(query, (cooldown_hours,))
            resolved_count = cursor.rowcount
        conn.commit()
        if resolved_count > 0:
            log.info(f"Resolved {resolved_count} stale circuit breaker event(s) at startup.")
        return resolved_count
    except Exception as e:
        log.warning(f"Could not resolve stale circuit breaker events: {e}")
        if conn:
            conn.rollback()
        return 0
    finally:
        release_db_connection(conn)
