import sqlite3
import os

# --- Database Setup ---

# Define the path to the database file within the 'data' directory
DB_DIR = os.path.join(os.path.dirname(__file__), '..', 'data')
DB_PATH = os.path.join(DB_DIR, 'crypto_data.db')

def get_db_connection():
    """Establishes a connection to the SQLite database."""
    # Ensure the 'data' directory exists
    os.makedirs(DB_DIR, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    # Use Row factory to access columns by name
    conn.row_factory = sqlite3.Row
    return conn

def initialize_database():
    """
    Creates the necessary database tables if they don't already exist.
    This function is idempotent (safe to run multiple times).
    """
    print("Initializing database...")
    conn = get_db_connection()
    cursor = conn.cursor()

    # --- Create fear_and_greed table ---
    # Stores historical Fear & Greed Index data.
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS fear_and_greed (
            timestamp INTEGER PRIMARY KEY,
            value INTEGER NOT NULL,
            value_classification TEXT NOT NULL,
            recorded_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')

    # --- Create market_prices table ---
    # Stores historical price data for monitored assets.
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS market_prices (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol TEXT NOT NULL,
            price REAL NOT NULL,
            timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')

    # --- Create whale_transactions table ---
    # Stores large on-chain transactions from Whale Alert.
    cursor.execute('''
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
    ''')

    # --- Create news_articles table ---
    # Stores news articles and their sentiment.
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS news_articles (
            url TEXT PRIMARY KEY,
            symbol TEXT NOT NULL,
            title TEXT NOT NULL,
            description TEXT,
            source TEXT,
            published_at TEXT NOT NULL,
            sentiment TEXT,
            recorded_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')

    conn.commit()
    conn.close()
    print("Database initialized successfully.")

if __name__ == '__main__':
    # This allows you to run the script directly to set up the database.
    initialize_database()
    print(f"Database file created/updated at: {DB_PATH}")
