import os
import sqlite3
from contextlib import contextmanager

import psycopg2
import psycopg2.pool
import pandas as pd
from psycopg2.extras import RealDictCursor
from src.config import app_config
from src.logger import log


@contextmanager
def _cursor(conn):
    """Returns a context-manager-compatible cursor for both PostgreSQL and SQLite."""
    is_pg = isinstance(conn, psycopg2.extensions.connection)
    cursor = conn.cursor(cursor_factory=RealDictCursor) if is_pg else conn.cursor()
    try:
        yield cursor
    finally:
        cursor.close()

# --- Connection Pool (for PostgreSQL) ---
_pg_pool = None

ALLOWED_TABLES = frozenset({"market_prices", "whale_transactions", "signals", "trades", "optimization_results", "news_sentiment", "circuit_breaker_events"})

# --- Database Connection Management ---

def _get_pg_dsn():
    """Determines the PostgreSQL DSN or connection kwargs based on config."""
    instance_connection_name = app_config.get('DB_INSTANCE_CONNECTION_NAME')
    db_config = app_config.get('db', {})
    config_db_url = app_config.get('DATABASE_URL')

    if instance_connection_name and db_config.get('user'):
        socket_path = f"/cloudsql/{instance_connection_name}"
        return None, dict(
            host=socket_path,
            user=db_config.get('user'),
            password=db_config.get('password'),
            dbname=db_config.get('name')
        )
    elif config_db_url:
        return config_db_url, None
    return None, None


def _get_pg_pool():
    """Returns a PostgreSQL connection pool, creating it on first use."""
    global _pg_pool
    if _pg_pool is not None:
        return _pg_pool

    dsn, kwargs = _get_pg_dsn()
    if dsn:
        _pg_pool = psycopg2.pool.SimpleConnectionPool(1, 10, dsn)
        log.info("Created PostgreSQL connection pool using DSN.")
    elif kwargs:
        _pg_pool = psycopg2.pool.SimpleConnectionPool(1, 10, **kwargs)
        log.info("Created PostgreSQL connection pool using Cloud SQL socket.")
    return _pg_pool


def get_db_connection(db_url=None):
    """
    Returns a database connection.
    - If a db_url is provided, connects directly (no pooling).
    - Otherwise uses connection pooling for PostgreSQL.
    - Falls back to SQLite if no PostgreSQL configuration is found.
    """
    if db_url:
        try:
            log.info("Connecting to PostgreSQL using provided DATABASE_URL.")
            conn = psycopg2.connect(db_url)
            log.info("Successfully connected to PostgreSQL.")
            return conn
        except psycopg2.OperationalError as e:
            log.error(f"Could not connect to PostgreSQL via provided DATABASE_URL: {e}", exc_info=True)
            raise

    pool = _get_pg_pool()
    if pool:
        try:
            conn = pool.getconn()
            log.debug("Acquired connection from pool.")
            return conn
        except psycopg2.OperationalError as e:
            log.error(f"Could not get connection from pool: {e}", exc_info=True)
            raise

    # Fallback to SQLite for local development without PostgreSQL
    log.info("No PostgreSQL config found, falling back to SQLite.")
    db_dir = os.path.join(os.path.dirname(__file__), '..', 'data')
    db_path = os.path.join(db_dir, 'crypto_data.db')
    os.makedirs(db_dir, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def release_db_connection(conn):
    """Returns a connection to the pool (PostgreSQL) or closes it (SQLite)."""
    if conn is None:
        return
    if isinstance(conn, psycopg2.extensions.connection):
        pool = _get_pg_pool()
        if pool:
            try:
                pool.putconn(conn)
                log.debug("Returned connection to pool.")
                return
            except Exception as e:
                log.warning(f"Failed to return connection to pool: {e}")
    try:
        conn.close()
    except Exception as e:
        log.warning(f"Failed to close connection: {e}")

def initialize_database(db_url=None):
    """
    Creates the necessary database tables if they don't already exist.
    Dynamically uses PostgreSQL or SQLite syntax based on the connection type.
    """
    log.info("Initializing database...")
    conn = None
    try:
        conn = get_db_connection(db_url)
        cursor = conn.cursor()

        # Runtime detection of the database type
        is_postgres_conn = isinstance(conn, psycopg2.extensions.connection)
        log.info(f"Connection type detected: {'PostgreSQL' if is_postgres_conn else 'SQLite'}")

        # --- Create Tables with dialect-specific SQL ---
        # Market Prices
        market_prices_sql = '''
            CREATE TABLE IF NOT EXISTS market_prices (
                id SERIAL PRIMARY KEY, symbol TEXT NOT NULL, price REAL NOT NULL,
                timestamp TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP
            )''' if is_postgres_conn else '''
            CREATE TABLE IF NOT EXISTS market_prices (
                id INTEGER PRIMARY KEY AUTOINCREMENT, symbol TEXT NOT NULL, price REAL NOT NULL,
                timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )'''
        cursor.execute(market_prices_sql)

        # Whale Transactions
        whale_transactions_sql = '''
            CREATE TABLE IF NOT EXISTS whale_transactions (
                id TEXT PRIMARY KEY, symbol TEXT NOT NULL, timestamp BIGINT NOT NULL, amount_usd REAL NOT NULL,
                from_owner TEXT, from_owner_type TEXT, to_owner TEXT, to_owner_type TEXT,
                recorded_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP
            )''' if is_postgres_conn else '''
            CREATE TABLE IF NOT EXISTS whale_transactions (
                id TEXT PRIMARY KEY, symbol TEXT NOT NULL, timestamp INTEGER NOT NULL, amount_usd REAL NOT NULL,
                from_owner TEXT, from_owner_type TEXT, to_owner TEXT, to_owner_type TEXT,
                recorded_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )'''
        cursor.execute(whale_transactions_sql)

        # Signals
        signals_sql = '''
            CREATE TABLE IF NOT EXISTS signals (
                id SERIAL PRIMARY KEY, symbol TEXT NOT NULL, signal_type TEXT NOT NULL, reason TEXT,
                price REAL, timestamp TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP
            )''' if is_postgres_conn else '''
            CREATE TABLE IF NOT EXISTS signals (
                id INTEGER PRIMARY KEY AUTOINCREMENT, symbol TEXT NOT NULL, signal_type TEXT NOT NULL, reason TEXT,
                price REAL, timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )'''
        cursor.execute(signals_sql)

        # Trades
        trades_sql = '''
            CREATE TABLE IF NOT EXISTS trades (
                id SERIAL PRIMARY KEY, symbol TEXT NOT NULL, order_id TEXT UNIQUE, side TEXT NOT NULL,
                entry_price REAL NOT NULL, quantity REAL NOT NULL, status TEXT NOT NULL, pnl REAL,
                exit_price REAL, entry_timestamp TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP, exit_timestamp TIMESTAMPTZ
            )''' if is_postgres_conn else '''
            CREATE TABLE IF NOT EXISTS trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT, symbol TEXT NOT NULL, order_id TEXT UNIQUE, side TEXT NOT NULL,
                entry_price REAL NOT NULL, quantity REAL NOT NULL, status TEXT NOT NULL, pnl REAL,
                exit_price REAL, entry_timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP, exit_timestamp TIMESTAMP
            )'''
        cursor.execute(trades_sql)

        # Optimization Results
        optimization_results_sql = '''
            CREATE TABLE IF NOT EXISTS optimization_results (
                id SERIAL PRIMARY KEY, sma_period INTEGER, stop_loss_percentage REAL,
                take_profit_percentage REAL, pnl REAL,
                timestamp TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP
            )''' if is_postgres_conn else '''
            CREATE TABLE IF NOT EXISTS optimization_results (
                id INTEGER PRIMARY KEY AUTOINCREMENT, sma_period INTEGER, stop_loss_percentage REAL,
                take_profit_percentage REAL, pnl REAL,
                timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )'''
        cursor.execute(optimization_results_sql)

        # News Sentiment
        news_sentiment_sql = '''
            CREATE TABLE IF NOT EXISTS news_sentiment (
                timestamp TIMESTAMPTZ NOT NULL,
                symbol TEXT NOT NULL,
                avg_sentiment_score REAL,
                news_volume INTEGER,
                sentiment_volatility REAL,
                positive_buzz_ratio REAL,
                negative_buzz_ratio REAL,
                PRIMARY KEY (timestamp, symbol)
            )''' if is_postgres_conn else '''
            CREATE TABLE IF NOT EXISTS news_sentiment (
                timestamp DATETIME NOT NULL,
                symbol TEXT NOT NULL,
                avg_sentiment_score REAL,
                news_volume INTEGER,
                sentiment_volatility REAL,
                positive_buzz_ratio REAL,
                negative_buzz_ratio REAL,
                PRIMARY KEY (timestamp, symbol)
            )'''
        cursor.execute(news_sentiment_sql)

        # Circuit Breaker Events
        circuit_breaker_sql = '''
            CREATE TABLE IF NOT EXISTS circuit_breaker_events (
                id SERIAL PRIMARY KEY,
                event_type TEXT NOT NULL,
                details TEXT,
                triggered_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
                resolved_at TIMESTAMPTZ
            )''' if is_postgres_conn else '''
            CREATE TABLE IF NOT EXISTS circuit_breaker_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_type TEXT NOT NULL,
                details TEXT,
                triggered_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                resolved_at TIMESTAMP
            )'''
        cursor.execute(circuit_breaker_sql)

        # --- Migrate trades table: add live trading columns if missing ---
        new_trade_columns = [
            ("trading_mode", "TEXT DEFAULT 'paper'"),
            ("exchange_order_id", "TEXT"),
            ("fees", "REAL DEFAULT 0"),
            ("fill_price", "REAL"),
            ("fill_quantity", "REAL"),
            ("asset_type", "TEXT DEFAULT 'crypto'"),
        ]
        for col_name, col_type in new_trade_columns:
            try:
                cursor.execute(f"ALTER TABLE trades ADD COLUMN {col_name} {col_type}")
                log.info(f"Added column '{col_name}' to trades table.")
            except (sqlite3.OperationalError, psycopg2.errors.DuplicateColumn):
                # Column already exists — safe to ignore
                if is_postgres_conn:
                    conn.rollback()
            except Exception as e:
                log.warning(f"Could not add column '{col_name}' to trades: {e}")
                if is_postgres_conn:
                    conn.rollback()

        conn.commit()
    except (sqlite3.Error, psycopg2.Error) as e:
        log.error(f"Error during database initialization: {e}", exc_info=True)
        if conn:
            conn.rollback()
    finally:
        if conn:
            release_db_connection(conn)
    log.info("Database initialization process completed.")

def save_optimization_result(params: dict, pnl: float):
    """Saves the result of a backtest optimization run to the database."""
    conn = None
    cursor = None
    try:
        conn = get_db_connection()
        is_postgres_conn = isinstance(conn, psycopg2.extensions.connection)
        query = '''
            INSERT INTO optimization_results (sma_period, stop_loss_percentage, take_profit_percentage, pnl)
            VALUES (%s, %s, %s, %s)
        ''' if is_postgres_conn else '''
            INSERT INTO optimization_results (sma_period, stop_loss_percentage, take_profit_percentage, pnl)
            VALUES (?, ?, ?, ?)
        '''
        
        cursor = conn.cursor()
        cursor.execute(query, (
            params.get('--sma-period'),
            params.get('--stop-loss-percentage'),
            params.get('--take-profit-percentage'),
            pnl
        ))
        conn.commit()
        log.info(f"Saved optimization result: PnL={pnl:.2f}, Params={params}")
    except (sqlite3.Error, psycopg2.Error) as e:
        log.error(f"Database error in save_optimization_result: {e}", exc_info=True)
    finally:
        if cursor:
            cursor.close()
        release_db_connection(conn)

def _safe_table_query(table: str) -> str:
    """Returns a safe COUNT query for a validated table name.

    Raises ValueError if the table is not in ALLOWED_TABLES.
    """
    if table not in ALLOWED_TABLES:
        raise ValueError(f"Table '{table}' is not in the allowed list")
    # table is now guaranteed to be one of the hardcoded ALLOWED_TABLES values
    return f"SELECT COUNT(*) FROM {table}"


def get_db_stats() -> dict:
    """Retrieves statistics from the database."""
    stats = {}
    conn = None
    cursor = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        tables = ["market_prices", "whale_transactions", "signals", "trades"]
        for table in tables:
            if table not in ALLOWED_TABLES:
                continue
            try:
                cursor.execute(_safe_table_query(table))
                stats[table] = cursor.fetchone()[0]
            except Exception as e:
                stats[table] = f"Error: {e}"
                log.error(f"Could not get stats for table {table}: {e}")
    except Exception as e:
        log.error(f"Error in get_db_stats: {e}", exc_info=True)
    finally:
        if cursor:
            cursor.close()
        release_db_connection(conn)
    return stats

# --- Data Access Functions ---

def save_signal(signal_data: dict):
    """Saves a generated signal to the database."""
    conn = None
    try:
        conn = get_db_connection()
        is_postgres_conn = isinstance(conn, psycopg2.extensions.connection)
        query = 'INSERT INTO signals (symbol, signal_type, reason, price) VALUES (%s, %s, %s, %s)' if is_postgres_conn else \
                'INSERT INTO signals (symbol, signal_type, reason, price) VALUES (?, ?, ?, ?)'
        with _cursor(conn) as cursor:
            cursor.execute(query, (
                signal_data.get('symbol'), signal_data.get('signal'),
                signal_data.get('reason'), signal_data.get('current_price')
            ))
        conn.commit()
        log.info(f"Saved signal for {signal_data.get('symbol')}: {signal_data.get('signal')}")
    except (sqlite3.Error, psycopg2.Error) as e:
        log.error(f"Database error in save_signal: {e}", exc_info=True)
    except Exception as e:
        log.error(f"An unexpected error occurred in save_signal: {e}", exc_info=True)
    finally:
        release_db_connection(conn)

def get_last_signal():
    """Retrieves the last generated signal from the database."""
    conn = None
    try:
        conn = get_db_connection()
        is_postgres_conn = isinstance(conn, psycopg2.extensions.connection)
        with _cursor(conn) as cursor:
            query = 'SELECT symbol, signal_type AS signal, reason, price AS current_price, timestamp FROM signals ORDER BY timestamp DESC LIMIT 1'
            cursor.execute(query)
            last_signal = cursor.fetchone()
        if last_signal:
            return dict(last_signal)
        return {"signal": "HOLD", "reason": "No signals recorded yet."}
    except (sqlite3.Error, psycopg2.Error) as e:
        log.error(f"Database error in get_last_signal: {e}", exc_info=True)
        return {"signal": "HOLD", "reason": "No signals recorded yet."}
    finally:
        release_db_connection(conn)

def get_historical_prices(symbol: str, limit: int = 5):
    """Retrieves the most recent 'limit' number of prices for a given symbol."""
    conn = None
    try:
        conn = get_db_connection()
        is_postgres_conn = isinstance(conn, psycopg2.extensions.connection)
        query = 'SELECT price FROM market_prices WHERE symbol = %s ORDER BY timestamp DESC LIMIT %s' if is_postgres_conn else \
                'SELECT price FROM market_prices WHERE symbol = ? ORDER BY timestamp DESC LIMIT ?'
        with _cursor(conn) as cursor:
            cursor.execute(query, (symbol, limit))
            prices = [row[0] for row in cursor.fetchall()]
        prices.reverse()
        return prices
    except (sqlite3.Error, psycopg2.Error) as e:
        log.error(f"Database error in get_historical_prices: {e}", exc_info=True)
        return []
    finally:
        release_db_connection(conn)

def get_trade_summary(hours_ago: int = 24) -> dict:
    """Calculates and returns a summary of trade performance over a given period."""
    conn = None
    try:
        conn = get_db_connection()
        is_postgres_conn = isinstance(conn, psycopg2.extensions.connection)
        with _cursor(conn) as cursor:
            if is_postgres_conn:
                query = "SELECT * FROM trades WHERE status = 'CLOSED' AND exit_timestamp >= NOW() - INTERVAL '%s hours'"
                cursor.execute(query, (hours_ago,))
            else:
                query = "SELECT * FROM trades WHERE status = 'CLOSED' AND exit_timestamp >= datetime('now', ? || ' hours')"
                cursor.execute(query, (f'-{hours_ago}',))
            closed_trades = [dict(row) for row in cursor.fetchall()]

        total_trades = len(closed_trades)
        wins = sum(1 for trade in closed_trades if trade.get('pnl', 0) > 0)
        total_pnl = sum(trade.get('pnl', 0) for trade in closed_trades)
        return {
            "total_closed": total_trades, "wins": wins, "losses": total_trades - wins,
            "total_pnl": total_pnl, "win_rate": (wins / total_trades * 100) if total_trades > 0 else 0
        }
    except (sqlite3.Error, psycopg2.Error) as e:
        log.error(f"Database error in get_trade_summary: {e}", exc_info=True)
        return {"total_closed": 0, "wins": 0, "losses": 0, "total_pnl": 0, "win_rate": 0}
    finally:
        release_db_connection(conn)

def get_whale_transactions_since(hours_ago: int = 24) -> list:
    """Retrieves all whale transactions recorded in the last N hours."""
    conn = None
    try:
        conn = get_db_connection()
        is_postgres_conn = isinstance(conn, psycopg2.extensions.connection)
        with _cursor(conn) as cursor:
            if is_postgres_conn:
                query = "SELECT * FROM whale_transactions WHERE recorded_at >= NOW() - INTERVAL '%s hours' ORDER BY timestamp DESC"
                cursor.execute(query, (hours_ago,))
            else:
                query = "SELECT * FROM whale_transactions WHERE recorded_at >= datetime('now', ? || ' hours') ORDER BY timestamp DESC"
                cursor.execute(query, (f'-{hours_ago}',))
            transactions = [dict(row) for row in cursor.fetchall()]
        log.info(f"Retrieved {len(transactions)} whale transactions from the last {hours_ago} hours.")
        return transactions
    except (sqlite3.Error, psycopg2.Error) as e:
        log.error(f"Database error in get_whale_transactions_since: {e}", exc_info=True)
        return []
    finally:
        release_db_connection(conn)

def get_price_history_since(hours_ago: int = 24) -> list:
    """Retrieves all price history recorded in the last N hours."""
    conn = None
    try:
        conn = get_db_connection()
        is_postgres_conn = isinstance(conn, psycopg2.extensions.connection)
        with _cursor(conn) as cursor:
            if is_postgres_conn:
                query = "SELECT * FROM market_prices WHERE timestamp >= NOW() - INTERVAL '%s hours' ORDER BY timestamp ASC"
                cursor.execute(query, (hours_ago,))
            else:
                query = "SELECT * FROM market_prices WHERE timestamp >= datetime('now', ? || ' hours') ORDER BY timestamp ASC"
                cursor.execute(query, (f'-{hours_ago}',))
            prices = [dict(row) for row in cursor.fetchall()]
        log.info(f"Retrieved {len(prices)} price points from the last {hours_ago} hours.")
        return prices
    except (sqlite3.Error, psycopg2.Error) as e:
        log.error(f"Database error in get_price_history_since: {e}", exc_info=True)
        return []
    finally:
        release_db_connection(conn)

def get_transaction_timestamps_since(symbol: str, hours_ago: int) -> list:
    """Retrieves the timestamps of all whale transactions for a specific symbol in the last N hours."""
    conn = None
    try:
        conn = get_db_connection()
        is_postgres_conn = isinstance(conn, psycopg2.extensions.connection)

        with _cursor(conn) as cursor:
            if is_postgres_conn:
                query = "SELECT timestamp FROM whale_transactions WHERE symbol = %s AND recorded_at >= NOW() - INTERVAL '%s hours'"
                cursor.execute(query, (symbol, hours_ago))
            else:
                query = "SELECT timestamp FROM whale_transactions WHERE symbol = ? AND recorded_at >= datetime('now', ? || ' hours')"
                cursor.execute(query, (symbol, f'-{hours_ago}'))
            timestamps = [row[0] for row in cursor.fetchall()]
        return timestamps
    except (sqlite3.Error, psycopg2.Error) as e:
        log.error(f"Database error in get_transaction_timestamps_since: {e}", exc_info=True)
        return []
    finally:
        release_db_connection(conn)

def get_table_counts() -> dict:
    """Retrieves the row count for the main tables in the database."""
    conn = None
    tables = ["whale_transactions", "market_prices", "signals", "trades"]
    counts = {}
    try:
        conn = get_db_connection()
        with _cursor(conn) as cursor:
            for table in tables:
                if table not in ALLOWED_TABLES:
                    continue
                try:
                    cursor.execute(_safe_table_query(table))
                    counts[table] = cursor.fetchone()[0]
                except (sqlite3.OperationalError, psycopg2.errors.UndefinedTable):
                    counts[table] = 0
                    log.warning(f"Table '{table}' not found while getting counts.")
        log.info(f"Retrieved table counts: {counts}")
    except Exception as e:
        log.error(f"Error in get_table_counts: {e}", exc_info=True)
    finally:
        release_db_connection(conn)
    return counts

def get_database_schema() -> list:
    """Retrieves the names of all tables in the public schema."""
    conn = None
    tables = []
    try:
        conn = get_db_connection()
        is_postgres_conn = isinstance(conn, psycopg2.extensions.connection)

        with _cursor(conn) as cursor:
            if is_postgres_conn:
                cursor.execute("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")
                tables = [row[0] for row in cursor.fetchall()]
            else:
                cursor.execute("SELECT name FROM sqlite_master WHERE type='table'")
                tables = [row[0] for row in cursor.fetchall()]
        log.info(f"Retrieved database schema. Tables: {tables}")
    except (sqlite3.Error, psycopg2.Error) as e:
        log.error(f"Database error in get_database_schema: {e}", exc_info=True)
    finally:
        release_db_connection(conn)
    return tables

def get_all_trades(db_url=None) -> pd.DataFrame:
    """
    Retrieves all trade records from the database and returns them as a pandas DataFrame.
    """
    conn = None
    try:
        conn = get_db_connection(db_url)
        # The SQL query is simple and works for both PostgreSQL and SQLite
        query = "SELECT * FROM trades ORDER BY entry_timestamp DESC"
        df = pd.read_sql_query(query, conn)
        log.info(f"Successfully retrieved {len(df)} trades from the database.")
        return df
    except Exception as e:
        log.error(f"Error retrieving all trades: {e}", exc_info=True)
        return pd.DataFrame()
    finally:
        release_db_connection(conn)

def get_stop_loss_signals(db_url=None) -> list:
    """
    Retrieves all 'Stop-loss hit' signals from the database.
    """
    conn = None
    try:
        conn = get_db_connection(db_url)
        is_postgres_conn = isinstance(conn, psycopg2.extensions.connection)
        with _cursor(conn) as cursor:
            query = "SELECT * FROM signals WHERE reason LIKE 'Stop-loss hit%%' ORDER BY timestamp DESC"
            cursor.execute(query)
            signals = [dict(row) for row in cursor.fetchall()]
        log.info(f"Retrieved {len(signals)} stop-loss signals.")
        return signals
    except Exception as e:
        log.error(f"Error retrieving stop-loss signals: {e}", exc_info=True)
        return []
    finally:
        release_db_connection(conn)

def get_price_history_for_trade(symbol: str, start_time, db_url=None) -> list:
    """
    Retrieves all price history for a symbol from a specific start time.
    """
    conn = None
    try:
        conn = get_db_connection(db_url)
        is_postgres_conn = isinstance(conn, psycopg2.extensions.connection)
        with _cursor(conn) as cursor:
            if is_postgres_conn:
                query = "SELECT price, timestamp FROM market_prices WHERE symbol = %s AND timestamp >= %s ORDER BY timestamp ASC"
                cursor.execute(query, (symbol, start_time))
            else:
                # SQLite version for compatibility
                query = "SELECT price, timestamp FROM market_prices WHERE symbol = ? AND timestamp >= ? ORDER BY timestamp ASC"
                cursor.execute(query, (symbol, start_time))
            
            prices = [dict(row) for row in cursor.fetchall()]
        return prices
    except Exception as e:
        log.error(f"Error retrieving price history for trade: {e}", exc_info=True)
        return []
    finally:
        release_db_connection(conn)

def save_news_sentiment_batch(rows: list):
    """Saves a batch of news sentiment records to the database using UPSERT."""
    if not rows:
        return
    conn = None
    try:
        conn = get_db_connection()
        is_postgres_conn = isinstance(conn, psycopg2.extensions.connection)
        with _cursor(conn) as cursor:
            for row in rows:
                if is_postgres_conn:
                    query = '''
                        INSERT INTO news_sentiment (timestamp, symbol, avg_sentiment_score, news_volume,
                            sentiment_volatility, positive_buzz_ratio, negative_buzz_ratio)
                        VALUES (NOW(), %s, %s, %s, %s, %s, %s)
                        ON CONFLICT (timestamp, symbol) DO UPDATE SET
                            avg_sentiment_score = EXCLUDED.avg_sentiment_score,
                            news_volume = EXCLUDED.news_volume,
                            sentiment_volatility = EXCLUDED.sentiment_volatility,
                            positive_buzz_ratio = EXCLUDED.positive_buzz_ratio,
                            negative_buzz_ratio = EXCLUDED.negative_buzz_ratio
                    '''
                else:
                    query = '''
                        INSERT OR REPLACE INTO news_sentiment (timestamp, symbol, avg_sentiment_score,
                            news_volume, sentiment_volatility, positive_buzz_ratio, negative_buzz_ratio)
                        VALUES (datetime('now'), ?, ?, ?, ?, ?, ?)
                    '''
                cursor.execute(query, (
                    row['symbol'], row['avg_sentiment_score'], row['news_volume'],
                    row['sentiment_volatility'], row['positive_buzz_ratio'], row['negative_buzz_ratio']
                ))
        conn.commit()
        log.info(f"Saved {len(rows)} news sentiment records.")
    except (sqlite3.Error, psycopg2.Error) as e:
        log.error(f"Database error in save_news_sentiment_batch: {e}", exc_info=True)
        if conn:
            conn.rollback()
    finally:
        release_db_connection(conn)

def get_trade_history_stats() -> dict:
    """
    Calculates win rate and average win/loss ratio from all closed trades.
    Used by the Kelly Criterion position sizing algorithm.

    Returns:
        dict with keys:
        - total_trades: int
        - wins: int
        - losses: int
        - win_rate: float (0-1)
        - avg_win: float (average PnL of winning trades)
        - avg_loss: float (average absolute PnL of losing trades)
        - kelly_fraction: float (recommended risk fraction, capped at 0.25)
    """
    conn = None
    default = {
        "total_trades": 0, "wins": 0, "losses": 0,
        "win_rate": 0.0, "avg_win": 0.0, "avg_loss": 0.0,
        "kelly_fraction": 0.0,
    }
    try:
        conn = get_db_connection()
        with _cursor(conn) as cursor:
            query = "SELECT pnl FROM trades WHERE status = 'CLOSED' AND pnl IS NOT NULL"
            cursor.execute(query)
            rows = cursor.fetchall()

        if not rows:
            return default

        pnls = [float(row[0]) for row in rows]
        wins = [p for p in pnls if p > 0]
        losses = [p for p in pnls if p < 0]
        total = len(pnls)

        win_rate = len(wins) / total if total > 0 else 0.0
        avg_win = sum(wins) / len(wins) if wins else 0.0
        avg_loss = abs(sum(losses) / len(losses)) if losses else 0.0

        # Kelly Criterion: f* = W - (1-W)/R
        # where W = win probability, R = win/loss ratio
        # Use half-Kelly for safety, cap at 25%
        kelly = 0.0
        if avg_loss > 0 and total >= 10:
            win_loss_ratio = avg_win / avg_loss
            kelly = win_rate - (1 - win_rate) / win_loss_ratio
            kelly = max(0.0, min(kelly * 0.5, 0.25))

        log.info(f"Trade history stats: {total} trades, {len(wins)} wins, "
                 f"win_rate={win_rate:.2%}, kelly={kelly:.4f}")
        return {
            "total_trades": total,
            "wins": len(wins),
            "losses": len(losses),
            "win_rate": win_rate,
            "avg_win": avg_win,
            "avg_loss": avg_loss,
            "kelly_fraction": kelly,
        }
    except (sqlite3.Error, psycopg2.Error) as e:
        log.error(f"Database error in get_trade_history_stats: {e}", exc_info=True)
        return default
    finally:
        release_db_connection(conn)


def get_latest_news_sentiment(symbols: list) -> dict:
    """Retrieves the most recent news sentiment record per symbol in a single query."""
    if not symbols:
        return {}
    conn = None
    try:
        conn = get_db_connection()
        is_postgres_conn = isinstance(conn, psycopg2.extensions.connection)
        result = {}
        with _cursor(conn) as cursor:
            if is_postgres_conn:
                query = '''
                    SELECT DISTINCT ON (symbol)
                        symbol, avg_sentiment_score, news_volume, sentiment_volatility,
                        positive_buzz_ratio, negative_buzz_ratio, timestamp
                    FROM news_sentiment
                    WHERE symbol = ANY(%s)
                    ORDER BY symbol, timestamp DESC
                '''
                cursor.execute(query, (symbols,))
            else:
                placeholders = ','.join('?' for _ in symbols)
                query = f'''
                    SELECT ns.symbol, ns.avg_sentiment_score, ns.news_volume,
                        ns.sentiment_volatility, ns.positive_buzz_ratio,
                        ns.negative_buzz_ratio, ns.timestamp
                    FROM news_sentiment ns
                    INNER JOIN (
                        SELECT symbol, MAX(timestamp) AS max_ts
                        FROM news_sentiment
                        WHERE symbol IN ({placeholders})
                        GROUP BY symbol
                    ) latest ON ns.symbol = latest.symbol AND ns.timestamp = latest.max_ts
                '''
                cursor.execute(query, symbols)

            for row in cursor.fetchall():
                row_dict = dict(row)
                result[row_dict['symbol']] = row_dict

        log.info(f"Retrieved latest news sentiment for {len(result)} symbols.")
        return result
    except (sqlite3.Error, psycopg2.Error) as e:
        log.error(f"Database error in get_latest_news_sentiment: {e}", exc_info=True)
        return {}
    finally:
        release_db_connection(conn)
