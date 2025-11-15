import os
import sqlite3
import psycopg2
import pandas as pd
from psycopg2.extras import RealDictCursor
from src.config import app_config
from src.logger import log

# --- Database Connection Management ---

def get_db_connection(db_url=None):
    """
    Establishes a connection to the database.
    - If a db_url is provided, it connects directly to that PostgreSQL instance.
    - In Google Cloud Run, connects to PostgreSQL via a Unix socket.
    - Locally, connects to PostgreSQL using the DATABASE_URL from config.
    - Falls back to SQLite if no PostgreSQL configuration is found.
    """
    # If a specific URL is provided, use it first.
    if db_url:
        try:
            log.info("Connecting to PostgreSQL using provided DATABASE_URL.")
            conn = psycopg2.connect(db_url)
            log.info("Successfully connected to PostgreSQL.")
            return conn
        except psycopg2.OperationalError as e:
            log.error(f"Could not connect to PostgreSQL via provided DATABASE_URL: {e}", exc_info=True)
            raise

    # --- Standard connection logic ---
    instance_connection_name = app_config.get('DB_INSTANCE_CONNECTION_NAME')
    db_config = app_config.get('db', {})
    config_db_url = app_config.get('DATABASE_URL')

    # Prioritize Unix socket connection for Cloud Run
    if instance_connection_name and db_config.get('user'):
        try:
            socket_path = f"/cloudsql/{instance_connection_name}"
            log.info(f"Connecting to Cloud SQL via Unix socket: {socket_path}")
            conn = psycopg2.connect(
                host=socket_path,
                user=db_config.get('user'),
                password=db_config.get('password'),
                dbname=db_config.get('name')
            )
            log.info("Successfully connected to Cloud SQL.")
            return conn
        except psycopg2.OperationalError as e:
            log.error(f"Could not connect to PostgreSQL via socket: {e}", exc_info=True)
            raise

    # Fallback to DATABASE_URL from config for local PostgreSQL
    elif config_db_url:
        try:
            log.info("Connecting to PostgreSQL using DATABASE_URL from config.")
            conn = psycopg2.connect(config_db_url)
            log.info("Successfully connected to PostgreSQL.")
            return conn
        except psycopg2.OperationalError as e:
            log.error(f"Could not connect to PostgreSQL via DATABASE_URL from config: {e}", exc_info=True)
            raise
    # Fallback to SQLite for local development without PostgreSQL
    else:
        log.info("No PostgreSQL config found, falling back to SQLite.")
        db_dir = os.path.join(os.path.dirname(__file__), '..', 'data')
        db_path = os.path.join(db_dir, 'crypto_data.db')
        os.makedirs(db_dir, exist_ok=True)
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        return conn

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

        conn.commit()
    except (sqlite3.Error, psycopg2.Error) as e:
        log.error(f"Error during database initialization: {e}", exc_info=True)
        if conn:
            conn.rollback()
    finally:
        if conn:
            conn.close()
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
        if conn:
            conn.close()

def get_db_stats() -> dict:
    """Retrieves statistics from the database."""
    stats = {}
    conn = get_db_connection()
    cursor = conn.cursor()
    tables = ["market_prices", "whale_transactions", "signals", "trades"]
    for table in tables:
        try:
            cursor.execute(f"SELECT COUNT(*) FROM {table}")
            stats[table] = cursor.fetchone()[0]
        except Exception as e:
            stats[table] = f"Error: {e}"
            log.error(f"Could not get stats for table {table}: {e}")

    cursor.close()
    conn.close()
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
        with conn.cursor() as cursor:
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
        if conn:
            conn.close()

def get_last_signal():
    """Retrieves the last generated signal from the database."""
    conn = get_db_connection()
    is_postgres_conn = isinstance(conn, psycopg2.extensions.connection)
    cursor_factory = RealDictCursor if is_postgres_conn else None
    with conn.cursor(cursor_factory=cursor_factory) as cursor:
        query = 'SELECT symbol, signal_type AS signal, reason, price AS current_price, timestamp FROM signals ORDER BY timestamp DESC LIMIT 1'
        cursor.execute(query)
        last_signal = cursor.fetchone()
    conn.close()
    if last_signal:
        return dict(last_signal)
    return {"signal": "HOLD", "reason": "No signals recorded yet."}

def get_historical_prices(symbol: str, limit: int = 5):
    """Retrieves the most recent 'limit' number of prices for a given symbol."""
    conn = get_db_connection()
    is_postgres_conn = isinstance(conn, psycopg2.extensions.connection)
    query = 'SELECT price FROM market_prices WHERE symbol = %s ORDER BY timestamp DESC LIMIT %s' if is_postgres_conn else \
            'SELECT price FROM market_prices WHERE symbol = ? ORDER BY timestamp DESC LIMIT ?'
    with conn.cursor() as cursor:
        cursor.execute(query, (symbol, limit))
        prices = [row[0] for row in cursor.fetchall()]
    conn.close()
    prices.reverse()
    return prices

def get_trade_summary(hours_ago: int = 24) -> dict:
    """Calculates and returns a summary of trade performance over a given period."""
    conn = get_db_connection()
    is_postgres_conn = isinstance(conn, psycopg2.extensions.connection)
    cursor_factory = RealDictCursor if is_postgres_conn else None

    with conn.cursor(cursor_factory=cursor_factory) as cursor:
        if is_postgres_conn:
            query = "SELECT * FROM trades WHERE status = 'CLOSED' AND exit_timestamp >= NOW() - INTERVAL '%s hours'"
            cursor.execute(query, (hours_ago,))
        else:
            query = "SELECT * FROM trades WHERE status = 'CLOSED' AND exit_timestamp >= datetime('now', ? || ' hours')"
            cursor.execute(query, (f'-{hours_ago}',))
        closed_trades = [dict(row) for row in cursor.fetchall()]
    conn.close()

    total_trades = len(closed_trades)
    wins = sum(1 for trade in closed_trades if trade.get('pnl', 0) > 0)
    total_pnl = sum(trade.get('pnl', 0) for trade in closed_trades)
    return {
        "total_closed": total_trades, "wins": wins, "losses": total_trades - wins,
        "total_pnl": total_pnl, "win_rate": (wins / total_trades * 100) if total_trades > 0 else 0
    }

def get_whale_transactions_since(hours_ago: int = 24) -> list:
    """Retrieves all whale transactions recorded in the last N hours."""
    conn = get_db_connection()
    is_postgres_conn = isinstance(conn, psycopg2.extensions.connection)
    cursor_factory = RealDictCursor if is_postgres_conn else None

    with conn.cursor(cursor_factory=cursor_factory) as cursor:
        if is_postgres_conn:
            query = "SELECT * FROM whale_transactions WHERE recorded_at >= NOW() - INTERVAL '%s hours' ORDER BY timestamp DESC"
            cursor.execute(query, (hours_ago,))
        else:
            query = "SELECT * FROM whale_transactions WHERE recorded_at >= datetime('now', ? || ' hours') ORDER BY timestamp DESC"
            cursor.execute(query, (f'-{hours_ago}',))
        transactions = [dict(row) for row in cursor.fetchall()]
    conn.close()
    log.info(f"Retrieved {len(transactions)} whale transactions from the last {hours_ago} hours.")
    return transactions

def get_price_history_since(hours_ago: int = 24) -> list:
    """Retrieves all price history recorded in the last N hours."""
    conn = get_db_connection()
    is_postgres_conn = isinstance(conn, psycopg2.extensions.connection)
    cursor_factory = RealDictCursor if is_postgres_conn else None

    with conn.cursor(cursor_factory=cursor_factory) as cursor:
        if is_postgres_conn:
            query = "SELECT * FROM market_prices WHERE timestamp >= NOW() - INTERVAL '%s hours' ORDER BY timestamp ASC"
            cursor.execute(query, (hours_ago,))
        else:
            query = "SELECT * FROM market_prices WHERE timestamp >= datetime('now', ? || ' hours') ORDER BY timestamp ASC"
            cursor.execute(query, (f'-{hours_ago}',))
        prices = [dict(row) for row in cursor.fetchall()]
    conn.close()
    log.info(f"Retrieved {len(prices)} price points from the last {hours_ago} hours.")
    return prices

def get_transaction_timestamps_since(symbol: str, hours_ago: int) -> list:
    """Retrieves the timestamps of all whale transactions for a specific symbol in the last N hours."""
    conn = get_db_connection()
    is_postgres_conn = isinstance(conn, psycopg2.extensions.connection)

    with conn.cursor() as cursor:
        if is_postgres_conn:
            query = "SELECT timestamp FROM whale_transactions WHERE symbol = %s AND recorded_at >= NOW() - INTERVAL '%s hours'"
            cursor.execute(query, (symbol, hours_ago))
        else:
            query = "SELECT timestamp FROM whale_transactions WHERE symbol = ? AND recorded_at >= datetime('now', ? || ' hours')"
            cursor.execute(query, (symbol, f'-{hours_ago}'))
        timestamps = [row[0] for row in cursor.fetchall()]
    conn.close()
    return timestamps

def get_table_counts() -> dict:
    """Retrierieves the row count for the main tables in the database."""
    conn = get_db_connection()
    tables = ["whale_transactions", "market_prices", "signals", "trades"]
    counts = {}

    with conn.cursor() as cursor:
        for table in tables:
            try:
                cursor.execute(f"SELECT COUNT(*) FROM {table}")
                counts[table] = cursor.fetchone()[0]
            except (sqlite3.OperationalError, psycopg2.errors.UndefinedTable):
                counts[table] = 0
                log.warning(f"Table '{table}' not found while getting counts.")
    conn.close()
    log.info(f"Retrieved table counts: {counts}")
    return counts

def get_database_schema() -> list:
    """Retrieves the names of all tables in the public schema."""
    conn = get_db_connection()
    is_postgres_conn = isinstance(conn, psycopg2.extensions.connection)
    tables = []

    with conn.cursor() as cursor:
        if is_postgres_conn:
            # Query for PostgreSQL
            cursor.execute("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")
            tables = [row[0] for row in cursor.fetchall()]
        else:
            # Query for SQLite
            cursor.execute("SELECT name FROM sqlite_master WHERE type='table'")
            tables = [row[0] for row in cursor.fetchall()]
    conn.close()
    log.info(f"Retrieved database schema. Tables: {tables}")
    return tables

import pandas as pd

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
        return pd.DataFrame()  # Return an empty DataFrame on error
    finally:
        if conn:
            conn.close()

def get_stop_loss_signals(db_url=None) -> list:
    """
    Retrieves all 'Stop-loss hit' signals from the database.
    """
    conn = None
    try:
        conn = get_db_connection(db_url)
        is_postgres_conn = isinstance(conn, psycopg2.extensions.connection)
        cursor_factory = RealDictCursor if is_postgres_conn else None
        
        with conn.cursor(cursor_factory=cursor_factory) as cursor:
            query = "SELECT * FROM signals WHERE reason LIKE 'Stop-loss hit%%' ORDER BY timestamp DESC"
            cursor.execute(query)
            signals = [dict(row) for row in cursor.fetchall()]
        log.info(f"Retrieved {len(signals)} stop-loss signals.")
        return signals
    except Exception as e:
        log.error(f"Error retrieving stop-loss signals: {e}", exc_info=True)
        return []
    finally:
        if conn:
            conn.close()

def get_price_history_for_trade(symbol: str, start_time, db_url=None) -> list:
    """
    Retrieves all price history for a symbol from a specific start time.
    """
    conn = None
    try:
        conn = get_db_connection(db_url)
        is_postgres_conn = isinstance(conn, psycopg2.extensions.connection)
        cursor_factory = RealDictCursor if is_postgres_conn else None

        with conn.cursor(cursor_factory=cursor_factory) as cursor:
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
        if conn:
            conn.close()
