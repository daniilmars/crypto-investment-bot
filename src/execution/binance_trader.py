import time
from src.logger import log
from src.config import app_config
from src.database import get_db_connection, IS_POSTGRES

def initialize_trades_table():
    """
    Creates the 'trades' table if it doesn't already exist.
    This table will store both open and closed paper trades.
    """
    log.info(f"Initializing trades table ({'PostgreSQL' if IS_POSTGRES else 'SQLite'})...")
    conn = get_db_connection()
    cursor = conn.cursor()

    trades_sql = '''
        CREATE TABLE IF NOT EXISTS trades (
            id SERIAL PRIMARY KEY,
            symbol TEXT NOT NULL,
            order_id TEXT UNIQUE,
            side TEXT NOT NULL, -- BUY or SELL
            entry_price REAL NOT NULL,
            quantity REAL NOT NULL,
            status TEXT NOT NULL, -- OPEN, CLOSED, CANCELED
            pnl REAL, -- Profit and Loss
            exit_price REAL,
            entry_timestamp TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
            exit_timestamp TIMESTAMPTZ
        )
    ''' if IS_POSTGRES else '''
        CREATE TABLE IF NOT EXISTS trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol TEXT NOT NULL,
            order_id TEXT UNIQUE,
            side TEXT NOT NULL,
            entry_price REAL NOT NULL,
            quantity REAL NOT NULL,
            status TEXT NOT NULL,
            pnl REAL,
            exit_price REAL,
            entry_timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            exit_timestamp TIMESTAMP
        )
    '''
    cursor.execute(trades_sql)
    conn.commit()
    cursor.close()
    conn.close()
    log.info("Trades table initialized successfully.")

def place_order(symbol: str, side: str, quantity: float, price: float, order_type: str = "MARKET") -> dict:
    """
    Simulates placing an order for paper trading.
    Records the trade in the database.
    """
    log.info(f"Simulating {side} order for {quantity} {symbol} at {price} (Type: {order_type})")
    
    # Generate a unique order ID for simulation
    order_id = f"PAPER_{symbol}_{side}_{int(time.time())}"

    conn = get_db_connection()
    cursor = conn.cursor()

    query = 'INSERT INTO trades (symbol, order_id, side, entry_price, quantity, status) VALUES (%s, %s, %s, %s, %s, %s)' if IS_POSTGRES else \
            'INSERT INTO trades (symbol, order_id, side, entry_price, quantity, status) VALUES (?, ?, ?, ?, ?, ?)'

    cursor.execute(query, (symbol, order_id, side, price, quantity, "OPEN"))
    conn.commit()
    cursor.close()
    conn.close()

    log.info(f"Paper trade recorded: Order ID {order_id}")
    return {"order_id": order_id, "symbol": symbol, "side": side, "quantity": quantity, "price": price, "status": "FILLED"}

def get_open_positions() -> list:
    """
    Retrieves all currently open paper trading positions from the database.
    """
    conn = get_db_connection()
    if IS_POSTGRES:
        from psycopg2.extras import RealDictCursor
        cursor = conn.cursor(cursor_factory=RealDictCursor)
    else:
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()

    query = 'SELECT * FROM trades WHERE status = %s' if IS_POSTGRES else \
            'SELECT * FROM trades WHERE status = ?'
    cursor.execute(query, ("OPEN",))
    
    positions = [dict(row) for row in cursor.fetchall()]
    
    cursor.close()
    conn.close()
    log.info(f"Retrieved {len(positions)} open paper positions.")
    return positions

def get_account_balance() -> dict:
    """
    Simulates retrieving the account balance for paper trading.
    For simplicity, we'll assume a fixed starting capital for paper trading.
    """
    # In a real scenario, this would query the exchange for actual balance.
    # For paper trading, we can simulate a starting capital.
    initial_capital = app_config.get('settings', {}).get('paper_trading_initial_capital', 10000.0)
    log.info(f"Simulating account balance: {initial_capital} USDT (paper trading)")
    return {"USDT": initial_capital, "total_usd": initial_capital}


if __name__ == '__main__':
    log.info("--- Testing Binance Trader Module (Paper Trading) ---")
    initialize_trades_table()
    balance = get_account_balance()
    log.info(f"Initial Balance: {balance}")

    # Simulate a BUY order
    buy_order = place_order("BTC", "BUY", 0.001, 30000.0)
    log.info(f"Buy Order: {buy_order}")

    # Simulate a SELL order
    sell_order = place_order("ETH", "SELL", 0.01, 2000.0)
    log.info(f"Sell Order: {sell_order}")

    open_positions = get_open_positions()
    log.info(f"Open Positions: {open_positions}")
    log.info("--- Test Complete ---")
