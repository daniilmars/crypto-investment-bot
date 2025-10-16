import sqlite3
import psycopg2
import os
import time
from src.logger import log
from src.config import app_config

import re

# --- Database Connection Management ---

DB_URL = app_config.get('database', {}).get('url')
IS_POSTGRES = DB_URL is not None

def get_db_connection():
    """
    Establishes a connection to the database.
    - In Google Cloud Run, connects to PostgreSQL via a Unix socket.
    - Locally, connects to PostgreSQL using the DATABASE_URL.
    - Falls back to SQLite if no DATABASE_URL is provided.
    """
    # Check if running in Google Cloud Run
    is_in_cloud_run = 'K_SERVICE' in os.environ

    if IS_POSTGRES:
        try:
            if is_in_cloud_run:
                # Use Unix socket for Cloud Run
                instance_connection_name = os.environ.get("DB_INSTANCE_CONNECTION_NAME")
                if not instance_connection_name:
                    raise ValueError("DB_INSTANCE_CONNECTION_NAME environment variable is not set for Cloud Run.")

                # Extract dbname, user, password from DATABASE_URL
                db_url_pattern = re.compile(r"postgresql://(?P<user>.*?):(?P<password>.*?)@(?P<host>.*?)/(?P<dbname>.*)")
                match = db_url_pattern.match(DB_URL)
                if not match:
                    raise ValueError("DATABASE_URL format is invalid.")
                db_parts = match.groupdict()
                
                db_user = db_parts['user']
                db_pass = db_parts['password']
                db_name = db_parts['dbname']
                
                socket_dir = '/cloudsql'
                db_socket = f"{socket_dir}/{instance_connection_name}"

                dsn = (
                    f"dbname={db_name} "
                    f"user={db_user} "
                    f"password={db_pass} "
                    f"host={db_socket}"
                )
                conn = psycopg2.connect(dsn)
            else:
                # Use TCP connection for local/other environments
                conn = psycopg2.connect(DB_URL)
            
            return conn
        except (psycopg2.OperationalError, ValueError) as e:
            log.error(f"Could not connect to PostgreSQL database: {e}")
            raise
    else:
        # Fallback to SQLite for local development
        db_dir = os.path.join(os.path.dirname(__file__), '..', 'data')
        db_path = os.path.join(db_dir, 'crypto_data.db')
        os.makedirs(db_dir, exist_ok=True)
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        return conn

def initialize_database():
    """
    Creates the necessary database tables if they don't already exist.
    Uses PostgreSQL or SQLite syntax based on the connection type.
    """
    log.info(f"Initializing database ({'PostgreSQL' if IS_POSTGRES else 'SQLite'})...")
    conn = get_db_connection()
    cursor = conn.cursor()

    # --- Create market_prices table ---
    market_prices_sql = '''
        CREATE TABLE IF NOT EXISTS market_prices (
            id SERIAL PRIMARY KEY,
            symbol TEXT NOT NULL,
            price REAL NOT NULL,
            timestamp TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP
        )
    ''' if IS_POSTGRES else '''
        CREATE TABLE IF NOT EXISTS market_prices (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol TEXT NOT NULL,
            price REAL NOT NULL,
            timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    '''
    cursor.execute(market_prices_sql)

    # --- Create whale_transactions table ---
    whale_transactions_sql = '''
        CREATE TABLE IF NOT EXISTS whale_transactions (
            id TEXT PRIMARY KEY,
            symbol TEXT NOT NULL,
            timestamp BIGINT NOT NULL,
            amount_usd REAL NOT NULL,
            from_owner TEXT,
            from_owner_type TEXT,
            to_owner TEXT,
            to_owner_type TEXT,
            recorded_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP
        )
    ''' if IS_POSTGRES else '''
        CREATE TABLE IF NOT EXISTS whale_transactions (
            id TEXT PRIMARY KEY,
            symbol TEXT NOT NULL,
            timestamp INTEGER NOT NULL,
            amount_usd REAL NOT NULL,
            from_owner TEXT,
            from_owner_type TEXT,
            to_owner TEXT,
            to_owner_type TEXT,
            recorded_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    '''
    cursor.execute(whale_transactions_sql)

    conn.commit()
    cursor.close()
    conn.close()
    log.info("Database initialized successfully.")

# --- Data Access Functions ---

def get_historical_prices(symbol: str, limit: int = 5):
    """
    Retrieves the most recent 'limit' number of prices for a given symbol.
    Returns a list of prices, oldest first.
    """
    conn = get_db_connection()
    cursor = conn.cursor()
    
    query = 'SELECT price FROM market_prices WHERE symbol = %s ORDER BY timestamp DESC LIMIT %s' if IS_POSTGRES else \
            'SELECT price FROM market_prices WHERE symbol = ? ORDER BY timestamp DESC LIMIT ?'
    
    cursor.execute(query, (symbol, limit))
    
    prices = [row[0] for row in cursor.fetchall()]
    
    cursor.close()
    conn.close()
    
    prices.reverse()
    return prices

def get_transaction_timestamps_since(symbol: str, hours_ago: int):
    """
    Retrieves all transaction timestamps for a symbol since a certain number of hours ago.
    """
    conn = get_db_connection()
    cursor = conn.cursor()
    
    if IS_POSTGRES:
        query = "SELECT timestamp FROM whale_transactions WHERE symbol = %s AND recorded_at >= NOW() - INTERVAL '%s hours'"
        cursor.execute(query, (symbol, hours_ago))
    else:
        start_timestamp = int(time.time()) - (hours_ago * 3600)
        query = "SELECT timestamp FROM whale_transactions WHERE symbol = ? AND timestamp >= ?"
        cursor.execute(query, (symbol, start_timestamp))
    
    timestamps = [row[0] for row in cursor.fetchall()]
    
    cursor.close()
    conn.close()
        
    return timestamps
