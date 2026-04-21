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

ALLOWED_TABLES = frozenset({"market_prices", "signals", "trades", "optimization_results", "news_sentiment", "circuit_breaker_events", "scraped_articles", "stoploss_cooldowns", "position_additions", "ipo_events", "macro_regime_history", "source_registry", "signal_attribution", "experiment_log", "tuning_history", "session_peaks", "watchlist_items", "bot_state_kv", "signal_decisions", "sector_convictions", "gemini_assessments", "strategy_scores", "longterm_thesis", "fx_rates", "gemini_calibration", "attribution_coverage_history"})

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

    pool_max = int(os.environ.get('DB_POOL_MAX', '20'))
    dsn, kwargs = _get_pg_dsn()
    if dsn:
        _pg_pool = psycopg2.pool.ThreadedConnectionPool(1, pool_max, dsn)
        log.info(f"Created PostgreSQL threaded connection pool using DSN (max={pool_max}).")
    elif kwargs:
        _pg_pool = psycopg2.pool.ThreadedConnectionPool(1, pool_max, **kwargs)
        log.info(f"Created PostgreSQL threaded connection pool using Cloud SQL socket (max={pool_max}).")
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
            # Check pool saturation before acquiring
            pool_max = int(os.environ.get('DB_POOL_MAX', '20'))
            used = pool_max - len(getattr(pool, '_pool', []))
            if pool_max > 0 and used / pool_max > 0.8:
                log.warning(f"DB pool saturation high: ~{used}/{pool_max} connections in use")
            conn = pool.getconn()
            log.debug("Acquired connection from pool.")
            return conn
        except psycopg2.pool.PoolError as e:
            log.error(f"DB connection pool exhausted: {e}. "
                      f"Increase DB_POOL_MAX (current: {os.environ.get('DB_POOL_MAX', '20')}).",
                      exc_info=True)
            raise
        except psycopg2.OperationalError as e:
            log.error(f"Could not get connection from pool: {e}", exc_info=True)
            raise

    # Fallback to SQLite for local development without PostgreSQL
    log.debug("No PostgreSQL config found, falling back to SQLite.")
    # BOT_DB_PATH lets local-dev tooling point at a copy of the production DB
    # without colliding with tests that expect a fresh data/crypto_data.db.
    db_path_env = os.environ.get('BOT_DB_PATH')
    if db_path_env:
        db_path = db_path_env
        db_dir = os.path.dirname(db_path) or '.'
    else:
        db_dir = os.path.join(os.path.dirname(__file__), '..', 'data')
        db_path = os.path.join(db_dir, 'crypto_data.db')
    os.makedirs(db_dir, exist_ok=True)
    conn = sqlite3.connect(db_path, check_same_thread=False, timeout=30.0)
    conn.row_factory = sqlite3.Row
    # WAL mode lets readers proceed while a writer holds the lock;
    # busy_timeout backs off instead of failing on brief contention.
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 30000")
    conn.execute("PRAGMA synchronous = NORMAL")
    return conn


def close_db_pool():
    """Closes all connections in the PostgreSQL pool. Call during shutdown."""
    global _pg_pool
    if _pg_pool is not None:
        try:
            _pg_pool.closeall()
            log.info("PostgreSQL connection pool closed.")
        except Exception as e:
            log.warning(f"Error closing DB pool: {e}")
        _pg_pool = None


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
                assessment_id INTEGER,
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
                assessment_id INTEGER,
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

        # Watchlist Items (chat-driven symbol tracking)
        watchlist_items_sql = '''
            CREATE TABLE IF NOT EXISTS watchlist_items (
                id SERIAL PRIMARY KEY,
                symbol TEXT NOT NULL,
                asset_type TEXT NOT NULL DEFAULT 'crypto',
                reason TEXT,
                added_by_user_id BIGINT NOT NULL,
                expires_at TIMESTAMPTZ NOT NULL,
                created_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
                is_active BOOLEAN DEFAULT TRUE,
                UNIQUE(symbol, is_active)
            )''' if is_postgres_conn else '''
            CREATE TABLE IF NOT EXISTS watchlist_items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol TEXT NOT NULL,
                asset_type TEXT NOT NULL DEFAULT 'crypto',
                reason TEXT,
                added_by_user_id INTEGER NOT NULL,
                expires_at TIMESTAMP NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                is_active INTEGER DEFAULT 1,
                UNIQUE(symbol, is_active)
            )'''
        cursor.execute(watchlist_items_sql)
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_watchlist_active ON watchlist_items (is_active, asset_type)")

        # Signal Decisions (tracks manual confirm/reject/expire decisions)
        signal_decisions_sql = '''
            CREATE TABLE IF NOT EXISTS signal_decisions (
                id SERIAL PRIMARY KEY,
                symbol TEXT NOT NULL,
                signal_type TEXT NOT NULL,
                asset_type TEXT DEFAULT 'crypto',
                decision TEXT NOT NULL,
                signal_strength REAL,
                gemini_confidence REAL,
                catalyst_freshness TEXT,
                reason TEXT,
                price REAL,
                decided_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP
            )''' if is_postgres_conn else '''
            CREATE TABLE IF NOT EXISTS signal_decisions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol TEXT NOT NULL,
                signal_type TEXT NOT NULL,
                asset_type TEXT DEFAULT 'crypto',
                decision TEXT NOT NULL,
                signal_strength REAL,
                gemini_confidence REAL,
                catalyst_freshness TEXT,
                reason TEXT,
                price REAL,
                decided_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )'''
        cursor.execute(signal_decisions_sql)
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_signal_decisions_symbol "
            "ON signal_decisions (symbol, decided_at)"
        )

        # Bot State KV (persistent key-value store for dashboard message IDs, etc.)
        bot_state_kv_sql = '''
            CREATE TABLE IF NOT EXISTS bot_state_kv (
                key TEXT PRIMARY KEY,
                value TEXT,
                updated_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP
            )''' if is_postgres_conn else '''
            CREATE TABLE IF NOT EXISTS bot_state_kv (
                key TEXT PRIMARY KEY,
                value TEXT,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )'''
        cursor.execute(bot_state_kv_sql)

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

        # --- Migrate trades table: add exit_reasoning column (Mini App) ---
        # Captures the prose-level WHY for each close. `exit_reason` is the
        # short tag ('stop_loss', 'analyst_exit', etc.); exit_reasoning is the
        # full sentence the analyst / deterministic helper produced.
        try:
            cursor.execute("ALTER TABLE trades ADD COLUMN exit_reasoning TEXT")
            log.info("Added column 'exit_reasoning' to trades table.")
        except (sqlite3.OperationalError, psycopg2.errors.DuplicateColumn):
            if is_postgres_conn:
                conn.rollback()
        except Exception as e:
            log.warning(f"Could not add column 'exit_reasoning' to trades: {e}")
            if is_postgres_conn:
                conn.rollback()

        # --- Migrate trades table: add strategy_type column (strategic trades) ---
        try:
            cursor.execute("ALTER TABLE trades ADD COLUMN strategy_type TEXT")
            log.info("Added column 'strategy_type' to trades table.")
        except (sqlite3.OperationalError, psycopg2.errors.DuplicateColumn):
            if is_postgres_conn:
                conn.rollback()
        except Exception as e:
            log.warning(f"Could not add column 'strategy_type' to trades: {e}")
            if is_postgres_conn:
                conn.rollback()

        # --- Migrate trades table: add trade_reason column (investment thesis) ---
        try:
            cursor.execute("ALTER TABLE trades ADD COLUMN trade_reason TEXT")
            log.info("Added column 'trade_reason' to trades table.")
        except (sqlite3.OperationalError, psycopg2.errors.DuplicateColumn):
            if is_postgres_conn:
                conn.rollback()
        except Exception as e:
            log.warning(f"Could not add column 'trade_reason' to trades: {e}")
            if is_postgres_conn:
                conn.rollback()

        # --- Migrate trades table: add dynamic_sl_pct / dynamic_tp_pct (ATR-based risk) ---
        for col in ('dynamic_sl_pct', 'dynamic_tp_pct'):
            try:
                cursor.execute(f"ALTER TABLE trades ADD COLUMN {col} REAL")
                log.info(f"Added column '{col}' to trades table.")
            except (sqlite3.OperationalError, psycopg2.errors.DuplicateColumn):
                if is_postgres_conn:
                    conn.rollback()
            except Exception as e:
                log.warning(f"Could not add column '{col}' to trades: {e}")
                if is_postgres_conn:
                    conn.rollback()

        # --- Migrate trades table: add limit order columns (pullback entry) ---
        for col, col_type in [('order_type', "TEXT DEFAULT 'MARKET'"),
                               ('limit_price', 'REAL'),
                               ('limit_expires_at', 'TIMESTAMP')]:
            try:
                cursor.execute(f"ALTER TABLE trades ADD COLUMN {col} {col_type}")
                log.info(f"Added column '{col}' to trades table.")
            except (sqlite3.OperationalError, psycopg2.errors.DuplicateColumn):
                if is_postgres_conn:
                    conn.rollback()
            except Exception as e:
                log.warning(f"Could not add column '{col}' to trades: {e}")
                if is_postgres_conn:
                    conn.rollback()

        # --- Migrate signal_attribution: add assessment_id (foreign key to
        # gemini_assessments.id) for robust calibration joins ---
        try:
            cursor.execute("ALTER TABLE signal_attribution ADD COLUMN assessment_id INTEGER")
            log.info("Added column 'assessment_id' to signal_attribution table.")
        except (sqlite3.OperationalError, psycopg2.errors.DuplicateColumn):
            if is_postgres_conn:
                conn.rollback()
        except Exception as e:
            log.warning(f"Could not add column 'assessment_id' to signal_attribution: {e}")
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

        # Sector Convictions (daily Gemini Pro sector review scores)
        sector_convictions_sql = '''
            CREATE TABLE IF NOT EXISTS sector_convictions (
                id SERIAL PRIMARY KEY,
                sector_group TEXT NOT NULL,
                asset_class TEXT NOT NULL,
                score REAL NOT NULL,
                rationale TEXT,
                key_catalyst TEXT,
                momentum TEXT,
                review_confidence REAL,
                cross_sector_theme TEXT,
                recorded_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP
            )''' if is_postgres_conn else '''
            CREATE TABLE IF NOT EXISTS sector_convictions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                sector_group TEXT NOT NULL,
                asset_class TEXT NOT NULL,
                score REAL NOT NULL,
                rationale TEXT,
                key_catalyst TEXT,
                momentum TEXT,
                review_confidence REAL,
                cross_sector_theme TEXT,
                recorded_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )'''
        cursor.execute(sector_convictions_sql)
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_sector_conv_recorded "
            "ON sector_convictions (recorded_at)"
        )
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_sector_conv_group "
            "ON sector_convictions (sector_group, recorded_at)"
        )

        # Gemini Assessments (persistent per-symbol assessment history for backtesting)
        gemini_assessments_sql = '''
            CREATE TABLE IF NOT EXISTS gemini_assessments (
                id SERIAL PRIMARY KEY,
                symbol TEXT NOT NULL,
                direction TEXT,
                confidence REAL,
                catalyst_type TEXT,
                catalyst_freshness TEXT,
                catalyst_count INTEGER,
                hype_vs_fundamental TEXT,
                risk_factors TEXT,
                reasoning TEXT,
                key_headline TEXT,
                market_mood TEXT,
                created_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP
            )''' if is_postgres_conn else '''
            CREATE TABLE IF NOT EXISTS gemini_assessments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol TEXT NOT NULL,
                direction TEXT,
                confidence REAL,
                catalyst_type TEXT,
                catalyst_freshness TEXT,
                catalyst_count INTEGER,
                hype_vs_fundamental TEXT,
                risk_factors TEXT,
                reasoning TEXT,
                key_headline TEXT,
                market_mood TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )'''
        cursor.execute(gemini_assessments_sql)
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_gemini_assess_symbol "
            "ON gemini_assessments (symbol, created_at)"
        )
        # Supports fast_path lookback: "WHERE created_at >= now - Nh
        # AND confidence >= X" — created_at-leading index for the range scan.
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_gemini_assess_created_conf "
            "ON gemini_assessments (created_at, confidence)"
        )

        # Strategy Scores (per-strategy effective strength for backtesting)
        strategy_scores_sql = '''
            CREATE TABLE IF NOT EXISTS strategy_scores (
                id SERIAL PRIMARY KEY,
                symbol TEXT NOT NULL,
                strategy_name TEXT NOT NULL,
                signal_type TEXT NOT NULL,
                base_strength REAL,
                effective_strength REAL NOT NULL,
                catalyst_type TEXT,
                hype_vs_fundamental TEXT,
                catalyst_count INTEGER,
                created_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP
            )''' if is_postgres_conn else '''
            CREATE TABLE IF NOT EXISTS strategy_scores (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol TEXT NOT NULL,
                strategy_name TEXT NOT NULL,
                signal_type TEXT NOT NULL,
                base_strength REAL,
                effective_strength REAL NOT NULL,
                catalyst_type TEXT,
                hype_vs_fundamental TEXT,
                catalyst_count INTEGER,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )'''
        cursor.execute(strategy_scores_sql)
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_strategy_scores_strat "
            "ON strategy_scores (strategy_name, created_at)"
        )

        # Longterm Thesis (autonomous sector/stock selection)
        longterm_thesis_sql = '''
            CREATE TABLE IF NOT EXISTS longterm_thesis (
                id SERIAL PRIMARY KEY,
                thesis_json TEXT NOT NULL,
                sectors_summary TEXT,
                model_used TEXT,
                is_active BOOLEAN DEFAULT TRUE,
                generated_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP
            )''' if is_postgres_conn else '''
            CREATE TABLE IF NOT EXISTS longterm_thesis (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                thesis_json TEXT NOT NULL,
                sectors_summary TEXT,
                model_used TEXT,
                is_active INTEGER DEFAULT 1,
                generated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )'''
        cursor.execute(longterm_thesis_sql)

        # FX Rates (USD-per-unit for foreign-currency ticker normalization)
        fx_rates_sql = '''
            CREATE TABLE IF NOT EXISTS fx_rates (
                currency TEXT PRIMARY KEY,
                usd_per_unit REAL NOT NULL,
                fetched_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP
            )''' if is_postgres_conn else '''
            CREATE TABLE IF NOT EXISTS fx_rates (
                currency TEXT PRIMARY KEY,
                usd_per_unit REAL NOT NULL,
                fetched_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )'''
        cursor.execute(fx_rates_sql)

        # Gemini Calibration — bucketed win-rate snapshots over time, lets us
        # detect drift in raw_confidence → realized_outcome calibration.
        gemini_calibration_sql = '''
            CREATE TABLE IF NOT EXISTS gemini_calibration (
                id SERIAL PRIMARY KEY,
                computed_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
                stratify_by TEXT NOT NULL,
                stratify_value TEXT NOT NULL,
                conf_bucket TEXT NOT NULL,
                n INTEGER NOT NULL,
                wins INTEGER NOT NULL,
                win_rate REAL,
                avg_pnl REAL,
                ci_low REAL,
                ci_high REAL
            )''' if is_postgres_conn else '''
            CREATE TABLE IF NOT EXISTS gemini_calibration (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                computed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                stratify_by TEXT NOT NULL,
                stratify_value TEXT NOT NULL,
                conf_bucket TEXT NOT NULL,
                n INTEGER NOT NULL,
                wins INTEGER NOT NULL,
                win_rate REAL,
                avg_pnl REAL,
                ci_low REAL,
                ci_high REAL
            )'''
        cursor.execute(gemini_calibration_sql)

        # Attribution coverage history — daily snapshots of L8 metrics so we
        # can see the coverage curve recover after the WS1 + rotation fixes.
        # Enables a trajectory drilldown in /check-news instead of a
        # point-in-time Apr 21 snapshot.
        attr_coverage_sql = '''
            CREATE TABLE IF NOT EXISTS attribution_coverage_history (
                id SERIAL PRIMARY KEY,
                computed_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
                window_days INTEGER NOT NULL,
                total_attributions INTEGER NOT NULL,
                with_sources INTEGER NOT NULL,
                with_hashes INTEGER NOT NULL,
                with_trade INTEGER NOT NULL,
                with_resolution INTEGER NOT NULL,
                coverage_pct_sources REAL NOT NULL
            )''' if is_postgres_conn else '''
            CREATE TABLE IF NOT EXISTS attribution_coverage_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                computed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                window_days INTEGER NOT NULL,
                total_attributions INTEGER NOT NULL,
                with_sources INTEGER NOT NULL,
                with_hashes INTEGER NOT NULL,
                with_trade INTEGER NOT NULL,
                with_resolution INTEGER NOT NULL,
                coverage_pct_sources REAL NOT NULL
            )'''
        cursor.execute(attr_coverage_sql)

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
            "CREATE INDEX IF NOT EXISTS idx_atth_computed_at "
            "ON attribution_coverage_history (computed_at)",
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
    description, symbol. Optional: gemini_score, category.
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

    Returns list of dicts with keys: title, title_hash, source, vader_score,
    collected_at, source_url, description, category, gemini_score.
    """
    conn = None
    try:
        conn = get_db_connection()
        is_postgres_conn = isinstance(conn, psycopg2.extensions.connection)
        with _cursor(conn) as cursor:
            if is_postgres_conn:
                query = '''
                    SELECT title, title_hash, source, vader_score, collected_at,
                           source_url, description, category, gemini_score
                    FROM scraped_articles
                    WHERE symbol = %s AND collected_at >= NOW() - INTERVAL '%s hours'
                    ORDER BY collected_at DESC LIMIT %s
                '''
                cursor.execute(query, (symbol, hours, limit))
            else:
                query = '''
                    SELECT title, title_hash, source, vader_score, collected_at,
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
                "SELECT order_id, trailing_stop_peak, trading_strategy FROM trades "
                "WHERE status = 'OPEN' AND trailing_stop_peak IS NOT NULL"
            )
            return {row[0]: (row[1], row[2]) for row in cursor.fetchall()}
    except (sqlite3.Error, psycopg2.Error) as e:
        log.error(f"Database error in load_trailing_stop_peaks: {e}", exc_info=True)
        return {}
    finally:
        release_db_connection(conn)


def save_gemini_assessments(assessments: dict):
    """Persist per-symbol Gemini assessments for backtesting.

    Args:
        assessments: Full Gemini result dict with 'symbol_assessments' and 'market_mood'.
    """
    if not assessments:
        return
    symbol_data = assessments.get('symbol_assessments', {})
    if not symbol_data:
        return
    mood = assessments.get('market_mood', '')
    conn = None
    try:
        conn = get_db_connection()
        is_pg = isinstance(conn, psycopg2.extensions.connection)
        query = '''
            INSERT INTO gemini_assessments
            (symbol, direction, confidence, catalyst_type, catalyst_freshness,
             catalyst_count, hype_vs_fundamental, risk_factors, reasoning,
             key_headline, market_mood)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        ''' if is_pg else '''
            INSERT INTO gemini_assessments
            (symbol, direction, confidence, catalyst_type, catalyst_freshness,
             catalyst_count, hype_vs_fundamental, risk_factors, reasoning,
             key_headline, market_mood)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        '''
        import json as _json
        with _cursor(conn) as cursor:
            for sym, sa in symbol_data.items():
                rf = sa.get('risk_factors')
                rf_str = _json.dumps(rf) if isinstance(rf, list) else str(rf) if rf else None
                cursor.execute(query, (
                    sym,
                    sa.get('direction'),
                    sa.get('confidence'),
                    sa.get('catalyst_type'),
                    sa.get('catalyst_freshness'),
                    sa.get('catalyst_count'),
                    sa.get('hype_vs_fundamental'),
                    rf_str,
                    sa.get('reasoning', '')[:500],
                    sa.get('key_headline', '')[:200],
                    mood[:200] if mood else None,
                ))
        conn.commit()
        log.debug(f"Saved {len(symbol_data)} Gemini assessments to DB.")
    except Exception as e:
        log.warning(f"Failed to save Gemini assessments: {e}")
        if conn:
            conn.rollback()
    finally:
        release_db_connection(conn)


def save_strategy_score(symbol, strategy_name, signal_type, base_strength,
                        effective_strength, gemini_assessment=None):
    """Save a single strategy score for backtesting comparison."""
    conn = None
    try:
        conn = get_db_connection()
        is_pg = isinstance(conn, psycopg2.extensions.connection)
        query = '''
            INSERT INTO strategy_scores
            (symbol, strategy_name, signal_type, base_strength,
             effective_strength, catalyst_type, hype_vs_fundamental, catalyst_count)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        ''' if is_pg else '''
            INSERT INTO strategy_scores
            (symbol, strategy_name, signal_type, base_strength,
             effective_strength, catalyst_type, hype_vs_fundamental, catalyst_count)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        '''
        ga = gemini_assessment or {}
        with _cursor(conn) as cursor:
            cursor.execute(query, (
                symbol, strategy_name, signal_type, base_strength,
                effective_strength,
                ga.get('catalyst_type'),
                ga.get('hype_vs_fundamental'),
                ga.get('catalyst_count'),
            ))
        conn.commit()
    except Exception as e:
        log.debug(f"Strategy score save failed: {e}")
        if conn:
            conn.rollback()
    finally:
        release_db_connection(conn)


def save_longterm_thesis(thesis_json: str, sectors_summary: str, model_used: str):
    """Save a new investment thesis, deactivating previous ones."""
    conn = None
    try:
        conn = get_db_connection()
        is_pg = isinstance(conn, psycopg2.extensions.connection)
        with _cursor(conn) as cursor:
            # Deactivate previous theses
            if is_pg:
                cursor.execute("UPDATE longterm_thesis SET is_active = FALSE WHERE is_active = TRUE")
            else:
                cursor.execute("UPDATE longterm_thesis SET is_active = 0 WHERE is_active = 1")
            # Insert new thesis
            query = (
                "INSERT INTO longterm_thesis (thesis_json, sectors_summary, model_used) "
                "VALUES (%s, %s, %s)" if is_pg else
                "INSERT INTO longterm_thesis (thesis_json, sectors_summary, model_used) "
                "VALUES (?, ?, ?)"
            )
            cursor.execute(query, (thesis_json, sectors_summary, model_used))
        conn.commit()
    except Exception as e:
        log.warning(f"Failed to save longterm thesis: {e}")
        if conn:
            conn.rollback()
    finally:
        release_db_connection(conn)


def get_active_thesis() -> dict | None:
    """Returns the latest active investment thesis."""
    conn = None
    try:
        conn = get_db_connection()
        is_pg = isinstance(conn, psycopg2.extensions.connection)
        with _cursor(conn) as cursor:
            query = (
                "SELECT thesis_json, sectors_summary, model_used, generated_at "
                "FROM longterm_thesis WHERE is_active = %s ORDER BY generated_at DESC LIMIT 1"
                if is_pg else
                "SELECT thesis_json, sectors_summary, model_used, generated_at "
                "FROM longterm_thesis WHERE is_active = 1 ORDER BY generated_at DESC LIMIT 1"
            )
            if is_pg:
                cursor.execute(query, (True,))
            else:
                cursor.execute(query)
            row = cursor.fetchone()
            if row:
                if is_pg:
                    return dict(row)
                cols = [d[0] for d in cursor.description]
                return dict(zip(cols, row))
        return None
    except Exception as e:
        log.debug(f"Could not load thesis: {e}")
        return None
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


def save_bot_state(key: str, value: str):
    """Persists a key-value pair in bot_state_kv (UPSERT)."""
    conn = None
    try:
        conn = get_db_connection()
        is_pg = isinstance(conn, psycopg2.extensions.connection)
        with _cursor(conn) as cursor:
            if is_pg:
                query = """
                    INSERT INTO bot_state_kv (key, value, updated_at)
                    VALUES (%s, %s, NOW())
                    ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value,
                    updated_at = NOW()
                """
            else:
                query = """
                    INSERT OR REPLACE INTO bot_state_kv (key, value, updated_at)
                    VALUES (?, ?, datetime('now'))
                """
            cursor.execute(query, (key, value))
        conn.commit()
    except (sqlite3.Error, psycopg2.Error) as e:
        log.error(f"Database error in save_bot_state: {e}", exc_info=True)
    finally:
        release_db_connection(conn)


def load_bot_state(key: str) -> str | None:
    """Loads a value from bot_state_kv by key. Returns None if not found."""
    conn = None
    try:
        conn = get_db_connection()
        is_pg = isinstance(conn, psycopg2.extensions.connection)
        with _cursor(conn) as cursor:
            query = "SELECT value FROM bot_state_kv WHERE key = %s" if is_pg else \
                    "SELECT value FROM bot_state_kv WHERE key = ?"
            cursor.execute(query, (key,))
            row = cursor.fetchone()
            if row:
                return dict(row)['value'] if hasattr(row, 'keys') else row[0]
            return None
    except (sqlite3.Error, psycopg2.Error) as e:
        log.error(f"Database error in load_bot_state: {e}", exc_info=True)
        return None
    finally:
        release_db_connection(conn)


@async_db
def record_signal_decision(signal: dict, decision: str):
    """Persist a manual signal confirmation decision."""
    conn = None
    try:
        conn = get_db_connection()
        is_pg = isinstance(conn, psycopg2.extensions.connection)
        with _cursor(conn) as cursor:
            query = '''
                INSERT INTO signal_decisions
                    (symbol, signal_type, asset_type, decision, signal_strength,
                     gemini_confidence, catalyst_freshness, reason, price)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            ''' if is_pg else '''
                INSERT INTO signal_decisions
                    (symbol, signal_type, asset_type, decision, signal_strength,
                     gemini_confidence, catalyst_freshness, reason, price)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            '''
            cursor.execute(query, (
                signal.get('symbol', ''),
                signal.get('signal', ''),
                signal.get('asset_type', 'crypto'),
                decision,
                signal.get('signal_strength'),
                signal.get('gemini_confidence'),
                signal.get('catalyst_freshness'),
                signal.get('reason', ''),
                signal.get('current_price'),
            ))
        conn.commit()
        log.debug(f"Recorded signal decision: {decision} for "
                  f"{signal.get('signal')} {signal.get('symbol')}")
    except (sqlite3.Error, psycopg2.Error) as e:
        log.error(f"Database error in record_signal_decision: {e}")
    finally:
        release_db_connection(conn)


@async_db
def get_signal_decisions(limit=100, decision=None) -> list[dict]:
    """Query signal decisions, optionally filtered by decision type."""
    conn = None
    try:
        conn = get_db_connection()
        is_pg = isinstance(conn, psycopg2.extensions.connection)
        with _cursor(conn) as cursor:
            params = []
            if is_pg:
                query = "SELECT * FROM signal_decisions"
                if decision:
                    query += " WHERE decision = %s"
                    params.append(decision)
                query += " ORDER BY decided_at DESC LIMIT %s"
                params.append(limit)
            else:
                query = "SELECT * FROM signal_decisions"
                if decision:
                    query += " WHERE decision = ?"
                    params.append(decision)
                query += " ORDER BY decided_at DESC LIMIT ?"
                params.append(limit)
            cursor.execute(query, tuple(params))
            rows = cursor.fetchall()
            if rows and hasattr(rows[0], 'keys'):
                return [dict(r) for r in rows]
            if rows:
                cols = [d[0] for d in cursor.description]
                return [dict(zip(cols, r)) for r in rows]
            return []
    except (sqlite3.Error, psycopg2.Error) as e:
        log.error(f"Database error in get_signal_decisions: {e}")
        return []
    finally:
        release_db_connection(conn)


def get_trades_closed_today(trading_strategy=None) -> list[dict]:
    """Returns trades closed today (UTC). Optionally filtered by trading_strategy."""
    from datetime import datetime, timezone
    conn = None
    try:
        conn = get_db_connection()
        is_pg = isinstance(conn, psycopg2.extensions.connection)
        with _cursor(conn) as cursor:
            if is_pg:
                base = """
                    SELECT symbol, entry_price, exit_price, pnl, exit_reason,
                           entry_timestamp, exit_timestamp, strategy_type
                    FROM trades
                    WHERE status = 'CLOSED'
                    AND exit_timestamp >= date_trunc('day', NOW() AT TIME ZONE 'UTC')
                """
                params = []
                if trading_strategy:
                    base += " AND trading_strategy = %s"
                    params.append(trading_strategy)
                base += " ORDER BY exit_timestamp DESC"
                cursor.execute(base, params)
            else:
                today_start = datetime.now(timezone.utc).strftime('%Y-%m-%d 00:00:00')
                base = """
                    SELECT symbol, entry_price, exit_price, pnl, exit_reason,
                           entry_timestamp, exit_timestamp, strategy_type
                    FROM trades
                    WHERE status = 'CLOSED'
                    AND exit_timestamp >= ?
                """
                params = [today_start]
                if trading_strategy:
                    base += " AND trading_strategy = ?"
                    params.append(trading_strategy)
                base += " ORDER BY exit_timestamp DESC"
                cursor.execute(base, params)
            rows = cursor.fetchall()
            result = []
            for row in rows:
                if hasattr(row, 'keys'):
                    result.append(dict(row))
                else:
                    result.append({
                        'symbol': row[0], 'entry_price': row[1],
                        'exit_price': row[2], 'pnl': row[3],
                        'exit_reason': row[4], 'entry_timestamp': row[5],
                        'exit_timestamp': row[6], 'strategy_type': row[7],
                    })
            return result
    except (sqlite3.Error, psycopg2.Error) as e:
        log.error(f"Database error in get_trades_closed_today: {e}", exc_info=True)
        return []
    finally:
        release_db_connection(conn)


def save_watchlist_item(symbol, asset_type, reason, user_id, ttl_days=30):
    """Persists a watchlist item (UPSERT on symbol+is_active)."""
    from datetime import datetime, timedelta, timezone
    conn = None
    expires_at = datetime.now(timezone.utc) + timedelta(days=ttl_days)
    try:
        conn = get_db_connection()
        is_pg = isinstance(conn, psycopg2.extensions.connection)
        with _cursor(conn) as cursor:
            if is_pg:
                query = """
                    INSERT INTO watchlist_items (symbol, asset_type, reason, added_by_user_id, expires_at)
                    VALUES (%s, %s, %s, %s, %s)
                    ON CONFLICT (symbol, is_active) DO UPDATE
                    SET expires_at = EXCLUDED.expires_at, reason = EXCLUDED.reason
                """
            else:
                query = """
                    INSERT OR REPLACE INTO watchlist_items
                    (symbol, asset_type, reason, added_by_user_id, expires_at, is_active)
                    VALUES (?, ?, ?, ?, ?, 1)
                """
            cursor.execute(query, (symbol, asset_type, reason, user_id, expires_at))
        conn.commit()
    except (sqlite3.Error, psycopg2.Error) as e:
        log.error(f"Database error in save_watchlist_item: {e}", exc_info=True)
    finally:
        release_db_connection(conn)


def remove_watchlist_item(symbol):
    """Deactivates a watchlist item."""
    conn = None
    try:
        conn = get_db_connection()
        is_pg = isinstance(conn, psycopg2.extensions.connection)
        with _cursor(conn) as cursor:
            if is_pg:
                query = "UPDATE watchlist_items SET is_active = FALSE WHERE symbol = %s AND is_active = TRUE"
            else:
                query = "UPDATE watchlist_items SET is_active = 0 WHERE symbol = ? AND is_active = 1"
            cursor.execute(query, (symbol,))
        conn.commit()
    except (sqlite3.Error, psycopg2.Error) as e:
        log.error(f"Database error in remove_watchlist_item: {e}", exc_info=True)
    finally:
        release_db_connection(conn)


def get_active_watchlist(asset_type=None):
    """Returns active, non-expired watchlist items."""
    conn = None
    try:
        conn = get_db_connection()
        is_pg = isinstance(conn, psycopg2.extensions.connection)
        with _cursor(conn) as cursor:
            if is_pg:
                base = "SELECT symbol, asset_type, reason, expires_at FROM watchlist_items WHERE is_active = TRUE AND expires_at > NOW()"
                if asset_type:
                    base += " AND asset_type = %s"
                    cursor.execute(base, (asset_type,))
                else:
                    cursor.execute(base)
            else:
                base = "SELECT symbol, asset_type, reason, expires_at FROM watchlist_items WHERE is_active = 1 AND expires_at > datetime('now')"
                if asset_type:
                    base += " AND asset_type = ?"
                    cursor.execute(base, (asset_type,))
                else:
                    cursor.execute(base)
            rows = cursor.fetchall()
            return [dict(row) if hasattr(row, 'keys') else
                    {'symbol': row[0], 'asset_type': row[1], 'reason': row[2], 'expires_at': row[3]}
                    for row in rows]
    except (sqlite3.Error, psycopg2.Error) as e:
        log.error(f"Database error in get_active_watchlist: {e}", exc_info=True)
        return []
    finally:
        release_db_connection(conn)


def expire_watchlist_items():
    """Marks expired watchlist items as inactive."""
    conn = None
    try:
        conn = get_db_connection()
        is_pg = isinstance(conn, psycopg2.extensions.connection)
        with _cursor(conn) as cursor:
            if is_pg:
                query = "UPDATE watchlist_items SET is_active = FALSE WHERE is_active = TRUE AND expires_at <= NOW()"
            else:
                query = "UPDATE watchlist_items SET is_active = 0 WHERE is_active = 1 AND expires_at <= datetime('now')"
            cursor.execute(query)
        conn.commit()
    except (sqlite3.Error, psycopg2.Error) as e:
        log.error(f"Database error in expire_watchlist_items: {e}", exc_info=True)
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
                ("news_sentiment", "timestamp"),
                ("gemini_assessments", "created_at"),
                ("strategy_scores", "created_at"),
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


# ---------------------------------------------------------------------------
# Pending (limit) order queries
# ---------------------------------------------------------------------------

def get_pending_orders(asset_type: str = 'crypto',
                       trading_strategy: str = 'manual') -> list:
    """Fetch all PENDING limit orders."""
    conn = None
    try:
        conn = get_db_connection()
        is_pg = isinstance(conn, psycopg2.extensions.connection)
        ph = '%s' if is_pg else '?'
        with _cursor(conn) as cursor:
            q = (f"SELECT order_id, symbol, entry_price, quantity, limit_price, "
                 f"limit_expires_at, asset_type, trading_strategy, "
                 f"dynamic_sl_pct, dynamic_tp_pct, strategy_type, trade_reason "
                 f"FROM trades WHERE status = {ph} AND asset_type = {ph} "
                 f"AND trading_strategy = {ph}")
            cursor.execute(q, ('PENDING', asset_type, trading_strategy))
            cols = [d[0] for d in cursor.description]
            return [dict(zip(cols, row)) for row in cursor.fetchall()]
    except Exception as e:
        log.error(f"get_pending_orders failed: {e}", exc_info=True)
        return []
    finally:
        release_db_connection(conn)


def fill_pending_order(order_id: str, fill_price: float):
    """Transition a PENDING order to OPEN (limit filled)."""
    conn = None
    try:
        conn = get_db_connection()
        is_pg = isinstance(conn, psycopg2.extensions.connection)
        ph = '%s' if is_pg else '?'
        with _cursor(conn) as cursor:
            q = (f"UPDATE trades SET status = 'OPEN', entry_price = {ph} "
                 f"WHERE order_id = {ph} AND status = 'PENDING'")
            cursor.execute(q, (fill_price, order_id))
        conn.commit()
        log.info(f"Limit order {order_id} filled at ${fill_price:.4f}")
    except Exception as e:
        log.error(f"fill_pending_order failed: {e}", exc_info=True)
        if conn:
            conn.rollback()
    finally:
        release_db_connection(conn)


def cancel_pending_order(order_id: str, reason: str = 'expired'):
    """Cancel a PENDING order (expired or manually cancelled)."""
    conn = None
    try:
        conn = get_db_connection()
        is_pg = isinstance(conn, psycopg2.extensions.connection)
        ph = '%s' if is_pg else '?'
        with _cursor(conn) as cursor:
            q = (f"UPDATE trades SET status = 'CANCELLED', "
                 f"exit_reason = {ph} WHERE order_id = {ph} AND status = 'PENDING'")
            cursor.execute(q, (reason, order_id))
        conn.commit()
        log.info(f"Pending order {order_id} cancelled: {reason}")
    except Exception as e:
        log.error(f"cancel_pending_order failed: {e}", exc_info=True)
        if conn:
            conn.rollback()
    finally:
        release_db_connection(conn)


def save_sector_convictions(convictions: list[dict]):
    """Batch-inserts sector conviction scores from a daily review."""
    conn = None
    try:
        conn = get_db_connection()
        is_pg = isinstance(conn, psycopg2.extensions.connection)
        query = '''
            INSERT INTO sector_convictions
            (sector_group, asset_class, score, rationale, key_catalyst,
             momentum, review_confidence, cross_sector_theme)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        ''' if is_pg else '''
            INSERT INTO sector_convictions
            (sector_group, asset_class, score, rationale, key_catalyst,
             momentum, review_confidence, cross_sector_theme)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        '''
        with _cursor(conn) as cursor:
            for c in convictions:
                cursor.execute(query, (
                    c.get('sector_group'),
                    c.get('asset_class', 'crypto'),
                    c.get('score', 0.0),
                    c.get('rationale'),
                    c.get('key_catalyst'),
                    c.get('momentum'),
                    c.get('review_confidence'),
                    c.get('cross_sector_theme'),
                ))
        conn.commit()
        log.info(f"Saved {len(convictions)} sector convictions to DB.")
    except (sqlite3.Error, psycopg2.Error) as e:
        log.error(f"Database error in save_sector_convictions: {e}", exc_info=True)
        if conn:
            conn.rollback()
    finally:
        release_db_connection(conn)


@async_db
def get_latest_sector_convictions() -> list[dict]:
    """Returns the most recent conviction per sector group (for startup cache reload)."""
    conn = None
    try:
        conn = get_db_connection()
        is_pg = isinstance(conn, psycopg2.extensions.connection)
        # Use DISTINCT ON (PostgreSQL) or a subquery (SQLite) to get latest per group
        if is_pg:
            query = '''
                SELECT DISTINCT ON (sector_group)
                    sector_group, asset_class, score, rationale, key_catalyst,
                    momentum, review_confidence, cross_sector_theme, recorded_at
                FROM sector_convictions
                ORDER BY sector_group, recorded_at DESC
            '''
        else:
            query = '''
                SELECT sc.sector_group, sc.asset_class, sc.score, sc.rationale,
                       sc.key_catalyst, sc.momentum, sc.review_confidence,
                       sc.cross_sector_theme, sc.recorded_at
                FROM sector_convictions sc
                INNER JOIN (
                    SELECT sector_group, MAX(recorded_at) AS max_recorded
                    FROM sector_convictions
                    GROUP BY sector_group
                ) latest ON sc.sector_group = latest.sector_group
                           AND sc.recorded_at = latest.max_recorded
            '''
        with _cursor(conn) as cursor:
            cursor.execute(query)
            return [dict(row) for row in cursor.fetchall()]
    except (sqlite3.Error, psycopg2.Error) as e:
        log.error(f"Database error in get_latest_sector_convictions: {e}", exc_info=True)
        return []
    finally:
        release_db_connection(conn)
