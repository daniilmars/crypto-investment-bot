import asyncio
import hashlib
import os
import sqlite3
from contextlib import contextmanager
from functools import wraps

import psycopg2
import psycopg2.pool
import pandas as pd
from psycopg2.extras import RealDictCursor
from src.config import app_config
from src.logger import log


def async_db(func):
    """Wraps a sync DB function so it can be awaited in async code.

    Usage:
        result = await get_open_positions()     # async caller
        result = get_open_positions.sync()      # sync caller (e.g., tests)
    """
    @wraps(func)
    async def wrapper(*args, **kwargs):
        return await asyncio.to_thread(func, *args, **kwargs)
    wrapper.sync = func
    return wrapper


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

ALLOWED_TABLES = frozenset({"market_prices", "signals", "trades", "optimization_results", "news_sentiment", "circuit_breaker_events", "scraped_articles", "stoploss_cooldowns", "position_additions", "ipo_events", "macro_regime_history", "source_registry", "signal_attribution", "experiment_log", "tuning_history", "session_peaks"})

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
        _pg_pool = psycopg2.pool.ThreadedConnectionPool(1, 10, dsn)
        log.info("Created PostgreSQL threaded connection pool using DSN.")
    elif kwargs:
        _pg_pool = psycopg2.pool.ThreadedConnectionPool(1, 10, **kwargs)
        log.info("Created PostgreSQL threaded connection pool using Cloud SQL socket.")
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
    log.debug("No PostgreSQL config found, falling back to SQLite.")
    db_dir = os.path.join(os.path.dirname(__file__), '..', 'data')
    db_path = os.path.join(db_dir, 'crypto_data.db')
    os.makedirs(db_dir, exist_ok=True)
    conn = sqlite3.connect(db_path, check_same_thread=False)
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

        # Scraped Articles (article archive)
        scraped_articles_sql = '''
            CREATE TABLE IF NOT EXISTS scraped_articles (
                id SERIAL PRIMARY KEY,
                title TEXT NOT NULL,
                title_hash TEXT NOT NULL,
                source TEXT,
                source_url TEXT,
                description TEXT,
                symbol TEXT,
                vader_score REAL,
                category TEXT,
                collected_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP
            )''' if is_postgres_conn else '''
            CREATE TABLE IF NOT EXISTS scraped_articles (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                title_hash TEXT NOT NULL,
                source TEXT,
                source_url TEXT,
                description TEXT,
                symbol TEXT,
                vader_score REAL,
                category TEXT,
                collected_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )'''
        cursor.execute(scraped_articles_sql)

        # Unique index on title_hash to deduplicate articles
        scraped_articles_idx_sql = (
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_scraped_articles_title_hash "
            "ON scraped_articles (title_hash)"
        )
        cursor.execute(scraped_articles_idx_sql)

        # Stoploss Cooldowns (persists across restarts)
        stoploss_cooldowns_sql = '''
            CREATE TABLE IF NOT EXISTS stoploss_cooldowns (
                symbol TEXT PRIMARY KEY,
                cooldown_expires_at TIMESTAMPTZ NOT NULL
            )''' if is_postgres_conn else '''
            CREATE TABLE IF NOT EXISTS stoploss_cooldowns (
                symbol TEXT PRIMARY KEY,
                cooldown_expires_at TIMESTAMP NOT NULL
            )'''
        cursor.execute(stoploss_cooldowns_sql)

        # Signal Cooldowns (persists across restarts)
        signal_cooldowns_sql = '''
            CREATE TABLE IF NOT EXISTS signal_cooldowns (
                symbol_signal TEXT PRIMARY KEY,
                symbol TEXT NOT NULL,
                signal_type TEXT NOT NULL,
                cooldown_expires_at TIMESTAMPTZ NOT NULL,
                is_auto BOOLEAN NOT NULL DEFAULT FALSE
            )''' if is_postgres_conn else '''
            CREATE TABLE IF NOT EXISTS signal_cooldowns (
                symbol_signal TEXT PRIMARY KEY,
                symbol TEXT NOT NULL,
                signal_type TEXT NOT NULL,
                cooldown_expires_at TIMESTAMP NOT NULL,
                is_auto INTEGER NOT NULL DEFAULT 0
            )'''
        cursor.execute(signal_cooldowns_sql)

        # Position Additions (tracks position size increases)
        position_additions_sql = '''
            CREATE TABLE IF NOT EXISTS position_additions (
                id SERIAL PRIMARY KEY,
                parent_order_id TEXT NOT NULL,
                addition_price REAL NOT NULL,
                addition_quantity REAL NOT NULL,
                reason TEXT,
                created_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP
            )''' if is_postgres_conn else '''
            CREATE TABLE IF NOT EXISTS position_additions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                parent_order_id TEXT NOT NULL,
                addition_price REAL NOT NULL,
                addition_quantity REAL NOT NULL,
                reason TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )'''
        cursor.execute(position_additions_sql)

        cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_pos_additions_order "
            "ON position_additions (parent_order_id)"
        )

        # IPO Events (tracks detected IPOs and new listings)
        ipo_events_sql = '''
            CREATE TABLE IF NOT EXISTS ipo_events (
                id SERIAL PRIMARY KEY,
                company_name TEXT NOT NULL,
                ticker TEXT,
                status TEXT NOT NULL DEFAULT 'detected',
                event_type TEXT NOT NULL,
                event_detail TEXT,
                source_url TEXT,
                source_article_hash TEXT,
                detected_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
                auto_added_to_watchlist BOOLEAN DEFAULT FALSE
            )''' if is_postgres_conn else '''
            CREATE TABLE IF NOT EXISTS ipo_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                company_name TEXT NOT NULL,
                ticker TEXT,
                status TEXT NOT NULL DEFAULT 'detected',
                event_type TEXT NOT NULL,
                event_detail TEXT,
                source_url TEXT,
                source_article_hash TEXT,
                detected_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                auto_added_to_watchlist BOOLEAN DEFAULT FALSE
            )'''
        cursor.execute(ipo_events_sql)

        cursor.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_ipo_events_dedup "
            "ON ipo_events (company_name, event_type)"
        )

        # Macro Regime History
        macro_regime_sql = '''
            CREATE TABLE IF NOT EXISTS macro_regime_history (
                id SERIAL PRIMARY KEY,
                regime TEXT NOT NULL,
                position_size_multiplier REAL NOT NULL,
                suppress_buys BOOLEAN DEFAULT FALSE,
                vix_current REAL,
                vix_signal TEXT,
                sp500_trend TEXT,
                yield_direction TEXT,
                btc_trend TEXT,
                score INTEGER,
                recorded_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP
            )''' if is_postgres_conn else '''
            CREATE TABLE IF NOT EXISTS macro_regime_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                regime TEXT NOT NULL,
                position_size_multiplier REAL NOT NULL,
                suppress_buys BOOLEAN DEFAULT FALSE,
                vix_current REAL,
                vix_signal TEXT,
                sp500_trend TEXT,
                yield_direction TEXT,
                btc_trend TEXT,
                score INTEGER,
                recorded_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )'''
        cursor.execute(macro_regime_sql)

        cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_macro_regime_recorded_at "
            "ON macro_regime_history (recorded_at)"
        )

        # Source Registry (autonomous bot infrastructure)
        source_registry_sql = '''
            CREATE TABLE IF NOT EXISTS source_registry (
                id SERIAL PRIMARY KEY,
                source_type TEXT NOT NULL,
                source_name TEXT NOT NULL UNIQUE,
                source_url TEXT NOT NULL,
                category TEXT,
                tier INTEGER DEFAULT 2,
                is_active BOOLEAN DEFAULT TRUE,
                reliability_score REAL DEFAULT 0.5,
                articles_total INTEGER DEFAULT 0,
                articles_with_signals INTEGER DEFAULT 0,
                profitable_signal_ratio REAL,
                avg_signal_pnl REAL,
                last_fetched_at TIMESTAMP,
                last_article_at TIMESTAMP,
                error_count INTEGER DEFAULT 0,
                consecutive_errors INTEGER DEFAULT 0,
                added_by TEXT DEFAULT 'manual',
                added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                deactivated_at TIMESTAMP,
                deactivation_reason TEXT,
                metadata_json TEXT
            )''' if is_postgres_conn else '''
            CREATE TABLE IF NOT EXISTS source_registry (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source_type TEXT NOT NULL,
                source_name TEXT NOT NULL UNIQUE,
                source_url TEXT NOT NULL,
                category TEXT,
                tier INTEGER DEFAULT 2,
                is_active INTEGER DEFAULT 1,
                reliability_score REAL DEFAULT 0.5,
                articles_total INTEGER DEFAULT 0,
                articles_with_signals INTEGER DEFAULT 0,
                profitable_signal_ratio REAL,
                avg_signal_pnl REAL,
                last_fetched_at TIMESTAMP,
                last_article_at TIMESTAMP,
                error_count INTEGER DEFAULT 0,
                consecutive_errors INTEGER DEFAULT 0,
                added_by TEXT DEFAULT 'manual',
                added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                deactivated_at TIMESTAMP,
                deactivation_reason TEXT,
                metadata_json TEXT
            )'''
        cursor.execute(source_registry_sql)

        # Signal Attribution (links source → article → signal → trade → PnL)
        signal_attribution_sql = '''
            CREATE TABLE IF NOT EXISTS signal_attribution (
                id SERIAL PRIMARY KEY,
                signal_id INTEGER,
                symbol TEXT NOT NULL,
                signal_type TEXT NOT NULL,
                signal_timestamp TIMESTAMP NOT NULL,
                signal_confidence REAL,
                article_hashes TEXT,
                source_names TEXT,
                gemini_direction TEXT,
                gemini_confidence REAL,
                catalyst_type TEXT,
                trade_order_id TEXT,
                trade_pnl REAL,
                trade_pnl_pct REAL,
                trade_duration_hours REAL,
                exit_reason TEXT,
                attribution_score REAL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                resolved_at TIMESTAMP
            )''' if is_postgres_conn else '''
            CREATE TABLE IF NOT EXISTS signal_attribution (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                signal_id INTEGER,
                symbol TEXT NOT NULL,
                signal_type TEXT NOT NULL,
                signal_timestamp TIMESTAMP NOT NULL,
                signal_confidence REAL,
                article_hashes TEXT,
                source_names TEXT,
                gemini_direction TEXT,
                gemini_confidence REAL,
                catalyst_type TEXT,
                trade_order_id TEXT,
                trade_pnl REAL,
                trade_pnl_pct REAL,
                trade_duration_hours REAL,
                exit_reason TEXT,
                attribution_score REAL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                resolved_at TIMESTAMP
            )'''
        cursor.execute(signal_attribution_sql)

        cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_signal_attribution_symbol "
            "ON signal_attribution (symbol)"
        )
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_signal_attribution_order "
            "ON signal_attribution (trade_order_id)"
        )

        # Experiment Log (audit trail of autonomous decisions)
        experiment_log_sql = '''
            CREATE TABLE IF NOT EXISTS experiment_log (
                id SERIAL PRIMARY KEY,
                experiment_type TEXT NOT NULL,
                description TEXT NOT NULL,
                old_value TEXT,
                new_value TEXT,
                reason TEXT,
                impact_metric TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )''' if is_postgres_conn else '''
            CREATE TABLE IF NOT EXISTS experiment_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                experiment_type TEXT NOT NULL,
                description TEXT NOT NULL,
                old_value TEXT,
                new_value TEXT,
                reason TEXT,
                impact_metric TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )'''
        cursor.execute(experiment_log_sql)

        # Tuning History (parameter change tracking with revert)
        tuning_history_sql = '''
            CREATE TABLE IF NOT EXISTS tuning_history (
                id SERIAL PRIMARY KEY,
                tuning_run_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                parameter_name TEXT NOT NULL,
                old_value REAL NOT NULL,
                new_value REAL NOT NULL,
                sample_trades INTEGER,
                old_sharpe REAL,
                new_sharpe REAL,
                old_win_rate REAL,
                new_win_rate REAL,
                applied BOOLEAN DEFAULT FALSE,
                reverted BOOLEAN DEFAULT FALSE,
                reverted_at TIMESTAMP,
                revert_reason TEXT
            )''' if is_postgres_conn else '''
            CREATE TABLE IF NOT EXISTS tuning_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                tuning_run_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                parameter_name TEXT NOT NULL,
                old_value REAL NOT NULL,
                new_value REAL NOT NULL,
                sample_trades INTEGER,
                old_sharpe REAL,
                new_sharpe REAL,
                old_win_rate REAL,
                new_win_rate REAL,
                applied INTEGER DEFAULT 0,
                reverted INTEGER DEFAULT 0,
                reverted_at TIMESTAMP,
                revert_reason TEXT
            )'''
        cursor.execute(tuning_history_sql)

        # Session Peaks (session-level high-water mark for drawdown detection)
        session_peaks_sql = '''
            CREATE TABLE IF NOT EXISTS session_peaks (
                asset_type TEXT PRIMARY KEY,
                peak_balance REAL NOT NULL,
                observed_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP
            )''' if is_postgres_conn else '''
            CREATE TABLE IF NOT EXISTS session_peaks (
                asset_type TEXT PRIMARY KEY,
                peak_balance REAL NOT NULL,
                observed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )'''
        cursor.execute(session_peaks_sql)

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

        # --- Migrate trades table: add trailing_stop_peak column if missing ---
        try:
            cursor.execute("ALTER TABLE trades ADD COLUMN trailing_stop_peak REAL")
            log.info("Added column 'trailing_stop_peak' to trades table.")
        except (sqlite3.OperationalError, psycopg2.errors.DuplicateColumn):
            if is_postgres_conn:
                conn.rollback()
        except Exception as e:
            log.warning(f"Could not add column 'trailing_stop_peak' to trades: {e}")
            if is_postgres_conn:
                conn.rollback()

        # --- Migrate scraped_articles table: add category column if missing ---
        try:
            cursor.execute("ALTER TABLE scraped_articles ADD COLUMN category TEXT")
            log.info("Added column 'category' to scraped_articles table.")
        except (sqlite3.OperationalError, psycopg2.errors.DuplicateColumn):
            if is_postgres_conn:
                conn.rollback()
        except Exception as e:
            log.warning(f"Could not add column 'category' to scraped_articles: {e}")
            if is_postgres_conn:
                conn.rollback()

        # --- Migrate scraped_articles table: add gemini_score column if missing ---
        try:
            cursor.execute("ALTER TABLE scraped_articles ADD COLUMN gemini_score REAL")
            log.info("Added column 'gemini_score' to scraped_articles table.")
        except (sqlite3.OperationalError, psycopg2.errors.DuplicateColumn):
            if is_postgres_conn:
                conn.rollback()
        except Exception as e:
            log.warning(f"Could not add column 'gemini_score' to scraped_articles: {e}")
            if is_postgres_conn:
                conn.rollback()

        # --- Migrate trades table: add trading_strategy column (auto-trading shadow bot) ---
        try:
            cursor.execute("ALTER TABLE trades ADD COLUMN trading_strategy TEXT DEFAULT 'manual'")
            log.info("Added column 'trading_strategy' to trades table.")
        except (sqlite3.OperationalError, psycopg2.errors.DuplicateColumn):
            if is_postgres_conn:
                conn.rollback()
        except Exception as e:
            log.warning(f"Could not add column 'trading_strategy' to trades: {e}")
            if is_postgres_conn:
                conn.rollback()

        # --- Migrate trades table: add exit_reason column (position reconciliation) ---
        try:
            cursor.execute("ALTER TABLE trades ADD COLUMN exit_reason TEXT")
            log.info("Added column 'exit_reason' to trades table.")
        except (sqlite3.OperationalError, psycopg2.errors.DuplicateColumn):
            if is_postgres_conn:
                conn.rollback()
        except Exception as e:
            log.warning(f"Could not add column 'exit_reason' to trades: {e}")
            if is_postgres_conn:
                conn.rollback()

        # --- Migrate circuit_breaker_events: add asset_type column ---
        try:
            cursor.execute("ALTER TABLE circuit_breaker_events ADD COLUMN asset_type TEXT DEFAULT 'crypto'")
            log.info("Added column 'asset_type' to circuit_breaker_events table.")
        except (sqlite3.OperationalError, psycopg2.errors.DuplicateColumn):
            if is_postgres_conn:
                conn.rollback()
        except Exception as e:
            log.warning(f"Could not add column 'asset_type' to circuit_breaker_events: {e}")
            if is_postgres_conn:
                conn.rollback()

        # --- Resolve legacy circuit_breaker_events without resolved_at (one-time migration) ---
        try:
            if is_postgres_conn:
                cursor.execute(
                    "UPDATE circuit_breaker_events SET resolved_at = NOW() "
                    "WHERE resolved_at IS NULL AND triggered_at < NOW() - INTERVAL '48 hours'")
            else:
                cursor.execute(
                    "UPDATE circuit_breaker_events SET resolved_at = datetime('now') "
                    "WHERE resolved_at IS NULL AND triggered_at < datetime('now', '-48 hours')")
            updated = cursor.rowcount
            if updated:
                log.info(f"Resolved {updated} stale circuit_breaker_events (older than 48h).")
        except Exception as e:
            log.warning(f"Could not resolve stale circuit_breaker_events: {e}")
            if is_postgres_conn:
                conn.rollback()

        # --- Performance indexes ---
        perf_indexes = [
            "CREATE INDEX IF NOT EXISTS idx_market_prices_symbol_ts "
            "ON market_prices (symbol, timestamp)",
            "CREATE INDEX IF NOT EXISTS idx_trades_status_asset "
            "ON trades (status, asset_type)",
            "CREATE INDEX IF NOT EXISTS idx_signals_timestamp "
            "ON signals (timestamp)",
            "CREATE INDEX IF NOT EXISTS idx_trades_symbol_status "
            "ON trades (symbol, status)",
            "CREATE INDEX IF NOT EXISTS idx_scraped_articles_collected "
            "ON scraped_articles (collected_at)",
            "CREATE INDEX IF NOT EXISTS idx_news_sentiment_symbol_ts "
            "ON news_sentiment (symbol, timestamp)",
        ]
        for idx_sql in perf_indexes:
            try:
                cursor.execute(idx_sql)
            except Exception as e:
                log.warning(f"Could not create index: {e}")
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
        tables = ["market_prices", "signals", "trades"]
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

@async_db
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

@async_db
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

@async_db
def get_trade_summary(hours_ago: int = 24, trading_strategy: str = None) -> dict:
    """Calculates and returns a summary of trade performance over a given period."""
    conn = None
    try:
        conn = get_db_connection()
        is_postgres_conn = isinstance(conn, psycopg2.extensions.connection)
        with _cursor(conn) as cursor:
            if is_postgres_conn:
                query = "SELECT * FROM trades WHERE status = 'CLOSED' AND exit_timestamp >= NOW() - INTERVAL '%s hours'"
                params = [hours_ago]
                if trading_strategy:
                    query += " AND trading_strategy = %s"
                    params.append(trading_strategy)
                cursor.execute(query, tuple(params))
            else:
                query = "SELECT * FROM trades WHERE status = 'CLOSED' AND exit_timestamp >= datetime('now', ? || ' hours')"
                params = [f'-{hours_ago}']
                if trading_strategy:
                    query += " AND trading_strategy = ?"
                    params.append(trading_strategy)
                cursor.execute(query, tuple(params))
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

def get_table_counts() -> dict:
    """Retrieves the row count for the main tables in the database."""
    conn = None
    tables = ["market_prices", "signals", "trades"]
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

@async_db
def get_trade_history_stats(trading_strategy: str = None) -> dict:
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
        is_pg = isinstance(conn, psycopg2.extensions.connection)
        with _cursor(conn) as cursor:
            query = "SELECT pnl FROM trades WHERE status = 'CLOSED' AND pnl IS NOT NULL"
            params = []
            if trading_strategy:
                query += " AND trading_strategy = %s" if is_pg else " AND trading_strategy = ?"
                params.append(trading_strategy)
            if params:
                cursor.execute(query, tuple(params))
            else:
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


def compute_title_hash(title: str) -> str:
    """Computes a SHA-256 hash of a lowercased, stripped title for deduplication."""
    return hashlib.sha256(title.lower().strip().encode('utf-8')).hexdigest()


def save_articles_batch(articles: list):
    """
    Saves a batch of scraped articles to the database, skipping duplicates.

    Each article dict should have: title, title_hash, source, source_url,
    description, symbol, vader_score.
    """
    if not articles:
        return
    conn = None
    try:
        conn = get_db_connection()
        is_postgres_conn = isinstance(conn, psycopg2.extensions.connection)
        with _cursor(conn) as cursor:
            for article in articles:
                if is_postgres_conn:
                    query = '''
                        INSERT INTO scraped_articles
                            (title, title_hash, source, source_url, description, symbol, vader_score, category, gemini_score)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                        ON CONFLICT (title_hash) DO NOTHING
                    '''
                else:
                    query = '''
                        INSERT OR IGNORE INTO scraped_articles
                            (title, title_hash, source, source_url, description, symbol, vader_score, category, gemini_score)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    '''
                cursor.execute(query, (
                    article.get('title', ''),
                    article.get('title_hash', ''),
                    article.get('source', ''),
                    article.get('source_url', ''),
                    article.get('description', ''),
                    article.get('symbol'),
                    article.get('vader_score'),
                    article.get('category', ''),
                    article.get('gemini_score'),
                ))
        conn.commit()
        log.info(f"Saved batch of {len(articles)} articles to archive.")
    except (sqlite3.Error, psycopg2.Error) as e:
        log.error(f"Database error in save_articles_batch: {e}", exc_info=True)
        if conn:
            conn.rollback()
    finally:
        release_db_connection(conn)


def get_gemini_scores_for_hashes(title_hashes: list) -> dict:
    """Looks up cached Gemini scores for a list of title hashes.

    Returns {title_hash: gemini_score} for rows that have a non-null gemini_score.
    """
    if not title_hashes:
        return {}
    conn = None
    try:
        conn = get_db_connection()
        is_pg = isinstance(conn, psycopg2.extensions.connection)
        with _cursor(conn) as cursor:
            if is_pg:
                query = ("SELECT title_hash, gemini_score FROM scraped_articles "
                         "WHERE title_hash = ANY(%s) AND gemini_score IS NOT NULL")
                cursor.execute(query, (title_hashes,))
            else:
                placeholders = ','.join('?' for _ in title_hashes)
                query = (f"SELECT title_hash, gemini_score FROM scraped_articles "
                         f"WHERE title_hash IN ({placeholders}) AND gemini_score IS NOT NULL")
                cursor.execute(query, title_hashes)
            return {row[0]: row[1] for row in cursor.fetchall()}
    except (sqlite3.Error, psycopg2.Error) as e:
        log.error(f"Database error in get_gemini_scores_for_hashes: {e}", exc_info=True)
        return {}
    finally:
        release_db_connection(conn)


def update_gemini_scores_batch(scores: dict):
    """Persists Gemini per-article scores to the scraped_articles table.

    Args:
        scores: {title_hash: gemini_score} mapping.
    """
    if not scores:
        return
    conn = None
    try:
        conn = get_db_connection()
        is_pg = isinstance(conn, psycopg2.extensions.connection)
        with _cursor(conn) as cursor:
            for title_hash, score in scores.items():
                query = (
                    "UPDATE scraped_articles SET gemini_score = %s WHERE title_hash = %s"
                    if is_pg else
                    "UPDATE scraped_articles SET gemini_score = ? WHERE title_hash = ?"
                )
                cursor.execute(query, (score, title_hash))
        conn.commit()
        log.info(f"Updated Gemini scores for {len(scores)} articles.")
    except (sqlite3.Error, psycopg2.Error) as e:
        log.error(f"Database error in update_gemini_scores_batch: {e}", exc_info=True)
        if conn:
            conn.rollback()
    finally:
        release_db_connection(conn)


@async_db
def get_recent_articles(symbol: str, hours: int = 24, limit: int = 20) -> list:
    """
    Returns recent archived articles for a symbol, ordered by newest first.

    Returns list of dicts with keys: title, source, vader_score, collected_at.
    """
    conn = None
    try:
        conn = get_db_connection()
        is_postgres_conn = isinstance(conn, psycopg2.extensions.connection)
        with _cursor(conn) as cursor:
            if is_postgres_conn:
                query = '''
                    SELECT title, source, vader_score, collected_at,
                           source_url, description, category, gemini_score
                    FROM scraped_articles
                    WHERE symbol = %s AND collected_at >= NOW() - INTERVAL '%s hours'
                    ORDER BY collected_at DESC LIMIT %s
                '''
                cursor.execute(query, (symbol, hours, limit))
            else:
                query = '''
                    SELECT title, source, vader_score, collected_at,
                           source_url, description, category, gemini_score
                    FROM scraped_articles
                    WHERE symbol = ? AND collected_at >= datetime('now', ? || ' hours')
                    ORDER BY collected_at DESC LIMIT ?
                '''
                cursor.execute(query, (symbol, f'-{hours}', limit))
            return [dict(row) for row in cursor.fetchall()]
    except (sqlite3.Error, psycopg2.Error) as e:
        log.error(f"Database error in get_recent_articles: {e}", exc_info=True)
        return []
    finally:
        release_db_connection(conn)


@async_db
def load_trailing_stop_peaks() -> dict:
    """Loads trailing stop peaks for all open positions from the database.

    Returns a dict of {order_id: peak_price} for open trades that have a
    non-null trailing_stop_peak value.  Called once on startup to restore
    in-memory state after a restart.
    """
    conn = None
    try:
        conn = get_db_connection()
        with _cursor(conn) as cursor:
            cursor.execute(
                "SELECT order_id, trailing_stop_peak FROM trades "
                "WHERE status = 'OPEN' AND trailing_stop_peak IS NOT NULL"
            )
            return {row[0]: row[1] for row in cursor.fetchall()}
    except (sqlite3.Error, psycopg2.Error) as e:
        log.error(f"Database error in load_trailing_stop_peaks: {e}", exc_info=True)
        return {}
    finally:
        release_db_connection(conn)


@async_db
def save_trailing_stop_peak(order_id: str, peak_price: float):
    """Persists the trailing stop peak price for a trade.

    Called only when the peak increases, so writes are infrequent.
    """
    conn = None
    try:
        conn = get_db_connection()
        is_postgres_conn = isinstance(conn, psycopg2.extensions.connection)
        query = (
            "UPDATE trades SET trailing_stop_peak = %s WHERE order_id = %s"
            if is_postgres_conn else
            "UPDATE trades SET trailing_stop_peak = ? WHERE order_id = ?"
        )
        with _cursor(conn) as cursor:
            cursor.execute(query, (peak_price, order_id))
        conn.commit()
    except (sqlite3.Error, psycopg2.Error) as e:
        log.error(f"Database error in save_trailing_stop_peak: {e}", exc_info=True)
    finally:
        release_db_connection(conn)


def get_article_count(hours: int = 24) -> int:
    """Returns the total number of archived articles collected in the last N hours."""
    conn = None
    try:
        conn = get_db_connection()
        is_postgres_conn = isinstance(conn, psycopg2.extensions.connection)
        with _cursor(conn) as cursor:
            if is_postgres_conn:
                query = "SELECT COUNT(*) FROM scraped_articles WHERE collected_at >= NOW() - INTERVAL '%s hours'"
                cursor.execute(query, (hours,))
            else:
                query = "SELECT COUNT(*) FROM scraped_articles WHERE collected_at >= datetime('now', ? || ' hours')"
                cursor.execute(query, (f'-{hours}',))
            return cursor.fetchone()[0]
    except (sqlite3.Error, psycopg2.Error) as e:
        log.error(f"Database error in get_article_count: {e}", exc_info=True)
        return 0
    finally:
        release_db_connection(conn)


@async_db
def save_stoploss_cooldown(symbol: str, expires_at):
    """Persists a stoploss cooldown expiry for a symbol (UPSERT)."""
    conn = None
    try:
        conn = get_db_connection()
        is_pg = isinstance(conn, psycopg2.extensions.connection)
        with _cursor(conn) as cursor:
            if is_pg:
                query = """
                    INSERT INTO stoploss_cooldowns (symbol, cooldown_expires_at)
                    VALUES (%s, %s)
                    ON CONFLICT (symbol) DO UPDATE SET cooldown_expires_at = EXCLUDED.cooldown_expires_at
                """
            else:
                query = """
                    INSERT OR REPLACE INTO stoploss_cooldowns (symbol, cooldown_expires_at)
                    VALUES (?, ?)
                """
            cursor.execute(query, (symbol, expires_at))
        conn.commit()
    except (sqlite3.Error, psycopg2.Error) as e:
        log.error(f"Database error in save_stoploss_cooldown: {e}", exc_info=True)
    finally:
        release_db_connection(conn)


@async_db
def load_stoploss_cooldowns() -> dict:
    """Loads non-expired stoploss cooldowns from the database.

    Returns {symbol: expires_at} for rows where cooldown has not yet expired.
    """
    conn = None
    try:
        conn = get_db_connection()
        is_pg = isinstance(conn, psycopg2.extensions.connection)
        with _cursor(conn) as cursor:
            if is_pg:
                query = "SELECT symbol, cooldown_expires_at FROM stoploss_cooldowns WHERE cooldown_expires_at > NOW()"
            else:
                query = "SELECT symbol, cooldown_expires_at FROM stoploss_cooldowns WHERE cooldown_expires_at > datetime('now')"
            cursor.execute(query)
            result = {}
            for row in cursor.fetchall():
                row_dict = dict(row) if hasattr(row, 'keys') else {'symbol': row[0], 'cooldown_expires_at': row[1]}
                sym = row_dict['symbol']
                expires = row_dict['cooldown_expires_at']
                # Convert string to datetime if needed (SQLite returns strings)
                if isinstance(expires, str):
                    from datetime import datetime, timezone
                    try:
                        expires = datetime.fromisoformat(expires.replace('Z', '+00:00'))
                    except ValueError:
                        expires = datetime.strptime(expires, '%Y-%m-%d %H:%M:%S.%f').replace(tzinfo=timezone.utc)
                result[sym] = expires
            return result
    except (sqlite3.Error, psycopg2.Error) as e:
        log.error(f"Database error in load_stoploss_cooldowns: {e}", exc_info=True)
        return {}
    finally:
        release_db_connection(conn)


@async_db
def clear_stoploss_cooldown(symbol: str):
    """Removes a stoploss cooldown entry for a symbol."""
    conn = None
    try:
        conn = get_db_connection()
        is_pg = isinstance(conn, psycopg2.extensions.connection)
        with _cursor(conn) as cursor:
            query = "DELETE FROM stoploss_cooldowns WHERE symbol = %s" if is_pg else \
                    "DELETE FROM stoploss_cooldowns WHERE symbol = ?"
            cursor.execute(query, (symbol,))
        conn.commit()
    except (sqlite3.Error, psycopg2.Error) as e:
        log.error(f"Database error in clear_stoploss_cooldown: {e}", exc_info=True)
    finally:
        release_db_connection(conn)


@async_db
def save_signal_cooldown(symbol: str, signal_type: str, expires_at, is_auto: bool = False):
    """Persists a signal cooldown expiry for a symbol+signal_type (UPSERT)."""
    conn = None
    key = f"{symbol}:{signal_type}:{'auto' if is_auto else 'manual'}"
    try:
        conn = get_db_connection()
        is_pg = isinstance(conn, psycopg2.extensions.connection)
        with _cursor(conn) as cursor:
            if is_pg:
                query = """
                    INSERT INTO signal_cooldowns (symbol_signal, symbol, signal_type, cooldown_expires_at, is_auto)
                    VALUES (%s, %s, %s, %s, %s)
                    ON CONFLICT (symbol_signal) DO UPDATE SET cooldown_expires_at = EXCLUDED.cooldown_expires_at
                """
            else:
                query = """
                    INSERT OR REPLACE INTO signal_cooldowns (symbol_signal, symbol, signal_type, cooldown_expires_at, is_auto)
                    VALUES (?, ?, ?, ?, ?)
                """
            cursor.execute(query, (key, symbol, signal_type, expires_at, is_auto))
        conn.commit()
    except (sqlite3.Error, psycopg2.Error) as e:
        log.error(f"Database error in save_signal_cooldown: {e}", exc_info=True)
    finally:
        release_db_connection(conn)


@async_db
def load_signal_cooldowns() -> tuple[dict, dict]:
    """Loads non-expired signal cooldowns from the database.

    Returns (manual_dict, auto_dict) where each is {"symbol:signal_type": expires_at}.
    """
    conn = None
    try:
        conn = get_db_connection()
        is_pg = isinstance(conn, psycopg2.extensions.connection)
        with _cursor(conn) as cursor:
            if is_pg:
                query = "SELECT symbol, signal_type, cooldown_expires_at, is_auto FROM signal_cooldowns WHERE cooldown_expires_at > NOW()"
            else:
                query = "SELECT symbol, signal_type, cooldown_expires_at, is_auto FROM signal_cooldowns WHERE cooldown_expires_at > datetime('now')"
            cursor.execute(query)
            manual = {}
            auto = {}
            for row in cursor.fetchall():
                row_dict = dict(row) if hasattr(row, 'keys') else {
                    'symbol': row[0], 'signal_type': row[1],
                    'cooldown_expires_at': row[2], 'is_auto': row[3],
                }
                sym = row_dict['symbol']
                sig_type = row_dict['signal_type']
                expires = row_dict['cooldown_expires_at']
                is_auto = row_dict['is_auto']
                # Convert string to datetime if needed (SQLite returns strings)
                if isinstance(expires, str):
                    from datetime import datetime, timezone
                    try:
                        expires = datetime.fromisoformat(expires.replace('Z', '+00:00'))
                    except ValueError:
                        expires = datetime.strptime(expires, '%Y-%m-%d %H:%M:%S.%f').replace(tzinfo=timezone.utc)
                key = f"{sym}:{sig_type}"
                if is_auto:
                    auto[key] = expires
                else:
                    manual[key] = expires
            return manual, auto
    except (sqlite3.Error, psycopg2.Error) as e:
        log.error(f"Database error in load_signal_cooldowns: {e}", exc_info=True)
        return {}, {}
    finally:
        release_db_connection(conn)


@async_db
def clear_signal_cooldown(symbol: str, signal_type: str, is_auto: bool = False):
    """Removes a signal cooldown entry."""
    conn = None
    key = f"{symbol}:{signal_type}:{'auto' if is_auto else 'manual'}"
    try:
        conn = get_db_connection()
        is_pg = isinstance(conn, psycopg2.extensions.connection)
        with _cursor(conn) as cursor:
            query = "DELETE FROM signal_cooldowns WHERE symbol_signal = %s" if is_pg else \
                    "DELETE FROM signal_cooldowns WHERE symbol_signal = ?"
            cursor.execute(query, (key,))
        conn.commit()
    except (sqlite3.Error, psycopg2.Error) as e:
        log.error(f"Database error in clear_signal_cooldown: {e}", exc_info=True)
    finally:
        release_db_connection(conn)


def save_position_addition(parent_order_id: str, price: float, quantity: float, reason: str = ""):
    """Records a position size increase (addition) in the database."""
    conn = None
    try:
        conn = get_db_connection()
        is_pg = isinstance(conn, psycopg2.extensions.connection)
        with _cursor(conn) as cursor:
            query = (
                "INSERT INTO position_additions (parent_order_id, addition_price, addition_quantity, reason) "
                "VALUES (%s, %s, %s, %s)"
            ) if is_pg else (
                "INSERT INTO position_additions (parent_order_id, addition_price, addition_quantity, reason) "
                "VALUES (?, ?, ?, ?)"
            )
            cursor.execute(query, (parent_order_id, price, quantity, reason))
        conn.commit()
        log.info(f"Saved position addition for {parent_order_id}: +{quantity} at ${price:,.2f}")
    except (sqlite3.Error, psycopg2.Error) as e:
        log.error(f"Database error in save_position_addition: {e}", exc_info=True)
    finally:
        release_db_connection(conn)


@async_db
def get_position_additions(parent_order_id: str) -> list:
    """Returns all position additions for a given order, ordered by created_at."""
    conn = None
    try:
        conn = get_db_connection()
        is_pg = isinstance(conn, psycopg2.extensions.connection)
        with _cursor(conn) as cursor:
            query = (
                "SELECT parent_order_id, addition_price, addition_quantity, reason, created_at "
                "FROM position_additions WHERE parent_order_id = %s ORDER BY created_at"
            ) if is_pg else (
                "SELECT parent_order_id, addition_price, addition_quantity, reason, created_at "
                "FROM position_additions WHERE parent_order_id = ? ORDER BY created_at"
            )
            cursor.execute(query, (parent_order_id,))
            return [dict(row) for row in cursor.fetchall()]
    except (sqlite3.Error, psycopg2.Error) as e:
        log.error(f"Database error in get_position_additions: {e}", exc_info=True)
        return []
    finally:
        release_db_connection(conn)


def update_trade_position(order_id: str, new_entry_price: float, new_quantity: float):
    """Updates entry_price and quantity for an open trade (used after position increase)."""
    conn = None
    try:
        conn = get_db_connection()
        is_pg = isinstance(conn, psycopg2.extensions.connection)
        with _cursor(conn) as cursor:
            query = (
                "UPDATE trades SET entry_price = %s, quantity = %s WHERE order_id = %s"
            ) if is_pg else (
                "UPDATE trades SET entry_price = ?, quantity = ? WHERE order_id = ?"
            )
            cursor.execute(query, (new_entry_price, new_quantity, order_id))
        conn.commit()
        log.info(f"Updated trade {order_id}: new avg price=${new_entry_price:,.2f}, qty={new_quantity}")
    except (sqlite3.Error, psycopg2.Error) as e:
        log.error(f"Database error in update_trade_position: {e}", exc_info=True)
    finally:
        release_db_connection(conn)


def save_ipo_event(company_name, ticker, status, event_type, event_detail=None,
                   source_url=None, source_article_hash=None):
    """Saves a new IPO event. Deduplicates by company_name + event_type via unique index."""
    conn = None
    try:
        conn = get_db_connection()
        is_pg = isinstance(conn, psycopg2.extensions.connection)
        with _cursor(conn) as cursor:
            if is_pg:
                query = '''
                    INSERT INTO ipo_events (company_name, ticker, status, event_type,
                        event_detail, source_url, source_article_hash)
                    VALUES (%s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (company_name, event_type) DO NOTHING
                '''
            else:
                query = '''
                    INSERT OR IGNORE INTO ipo_events (company_name, ticker, status, event_type,
                        event_detail, source_url, source_article_hash)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                '''
            cursor.execute(query, (company_name, ticker, status, event_type,
                                   event_detail, source_url, source_article_hash))
        conn.commit()
    except (sqlite3.Error, psycopg2.Error) as e:
        log.error(f"Database error in save_ipo_event: {e}", exc_info=True)
        if conn:
            conn.rollback()
    finally:
        release_db_connection(conn)


def get_ipo_events(status=None, since_hours=None):
    """Returns IPO events, optionally filtered by status and time window."""
    conn = None
    try:
        conn = get_db_connection()
        is_pg = isinstance(conn, psycopg2.extensions.connection)
        with _cursor(conn) as cursor:
            conditions = []
            params = []
            if status:
                conditions.append("status = %s" if is_pg else "status = ?")
                params.append(status)
            if since_hours is not None:
                if is_pg:
                    conditions.append("detected_at >= NOW() - INTERVAL '%s hours'")
                    params.append(since_hours)
                else:
                    conditions.append("detected_at >= datetime('now', ?)")
                    params.append(f'-{since_hours} hours')
            where_clause = " AND ".join(conditions)
            query = "SELECT * FROM ipo_events"
            if where_clause:
                query += f" WHERE {where_clause}"
            query += " ORDER BY detected_at DESC"
            cursor.execute(query, tuple(params))
            rows = cursor.fetchall()
            if is_pg:
                return [dict(row) for row in rows]
            # SQLite: convert Row objects
            columns = [desc[0] for desc in cursor.description] if cursor.description else []
            return [dict(zip(columns, row)) for row in rows]
    except (sqlite3.Error, psycopg2.Error) as e:
        log.error(f"Database error in get_ipo_events: {e}", exc_info=True)
        return []
    finally:
        release_db_connection(conn)


def mark_ipo_watchlist_added(event_id):
    """Sets auto_added_to_watchlist = True for a given event."""
    conn = None
    try:
        conn = get_db_connection()
        is_pg = isinstance(conn, psycopg2.extensions.connection)
        with _cursor(conn) as cursor:
            query = (
                "UPDATE ipo_events SET auto_added_to_watchlist = TRUE WHERE id = %s"
            ) if is_pg else (
                "UPDATE ipo_events SET auto_added_to_watchlist = 1 WHERE id = ?"
            )
            cursor.execute(query, (event_id,))
        conn.commit()
    except (sqlite3.Error, psycopg2.Error) as e:
        log.error(f"Database error in mark_ipo_watchlist_added: {e}", exc_info=True)
        if conn:
            conn.rollback()
    finally:
        release_db_connection(conn)


@async_db
def save_macro_regime(regime_data: dict):
    """Saves a macro regime snapshot to the history table."""
    conn = None
    try:
        conn = get_db_connection()
        is_pg = isinstance(conn, psycopg2.extensions.connection)
        signals = regime_data.get('signals', {})
        indicators = regime_data.get('indicators', {})
        vix_current = None
        if indicators.get('vix') and isinstance(indicators['vix'], dict):
            vix_current = indicators['vix'].get('current')

        query = '''
            INSERT INTO macro_regime_history
            (regime, position_size_multiplier, suppress_buys,
             vix_current, vix_signal, sp500_trend, yield_direction, btc_trend, score)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
        ''' if is_pg else '''
            INSERT INTO macro_regime_history
            (regime, position_size_multiplier, suppress_buys,
             vix_current, vix_signal, sp500_trend, yield_direction, btc_trend, score)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        '''
        with _cursor(conn) as cursor:
            cursor.execute(query, (
                regime_data.get('regime'),
                regime_data.get('position_size_multiplier'),
                regime_data.get('suppress_buys', False),
                vix_current,
                str(signals.get('vix_signal', '')),
                str(signals.get('sp500_trend', '')),
                str(signals.get('yield_direction', '')),
                str(signals.get('btc_trend', '')),
                regime_data.get('score', 0),
            ))
        conn.commit()
    except (sqlite3.Error, psycopg2.Error) as e:
        log.error(f"Database error in save_macro_regime: {e}", exc_info=True)
        if conn:
            conn.rollback()
    finally:
        release_db_connection(conn)


def get_macro_regime_history(limit=10):
    """Returns the most recent macro regime snapshots."""
    conn = None
    try:
        conn = get_db_connection()
        is_pg = isinstance(conn, psycopg2.extensions.connection)
        with _cursor(conn) as cursor:
            query = (
                "SELECT * FROM macro_regime_history "
                "ORDER BY recorded_at DESC LIMIT %s"
            ) if is_pg else (
                "SELECT * FROM macro_regime_history "
                "ORDER BY recorded_at DESC LIMIT ?"
            )
            cursor.execute(query, (limit,))
            rows = cursor.fetchall()
            if is_pg:
                return [dict(row) for row in rows]
            columns = [desc[0] for desc in cursor.description] if cursor.description else []
            return [dict(zip(columns, row)) for row in rows]
    except (sqlite3.Error, psycopg2.Error) as e:
        log.error(f"Database error in get_macro_regime_history: {e}", exc_info=True)
        return []
    finally:
        release_db_connection(conn)


def cleanup_old_rows(days: int = 30) -> dict:
    """Delete market_prices, signals, and news_sentiment rows older than `days`."""
    conn = None
    deleted = {}
    try:
        conn = get_db_connection()
        is_pg = isinstance(conn, psycopg2.extensions.connection)
        with _cursor(conn) as cursor:
            tables = [
                ("market_prices", "timestamp"),
                ("signals", "timestamp"),
                ("news_sentiment", "analysis_timestamp"),
            ]
            for table, col in tables:
                if is_pg:
                    cursor.execute(
                        f"DELETE FROM {table} WHERE {col} < NOW() - INTERVAL '{days} days'")
                else:
                    cursor.execute(
                        f"DELETE FROM {table} WHERE {col} < datetime('now', '-{days} days')")
                deleted[table] = cursor.rowcount
            conn.commit()
    except Exception as e:
        log.error(f"cleanup_old_rows failed: {e}", exc_info=True)
        if conn:
            conn.rollback()
    finally:
        release_db_connection(conn)
    return deleted
