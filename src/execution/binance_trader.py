import time
import sqlite3
import psycopg2
from src.logger import log
from src.config import app_config
from src.database import get_db_connection

def place_order(symbol: str, side: str, quantity: float, price: float, order_type: str = "MARKET", existing_order_id: str = None) -> dict:
    """
    Simulates placing an order for paper trading.
    Records the trade in the database.
    """
    conn = get_db_connection()
    is_postgres_conn = isinstance(conn, psycopg2.extensions.connection)
    cursor = conn.cursor()

    if side == "BUY":
        log.info(f"Simulating BUY order for {quantity} {symbol} at {price} (Type: {order_type})")
        order_id = f"PAPER_{symbol}_BUY_{int(time.time() * 1000)}"
        query = 'INSERT INTO trades (symbol, order_id, side, entry_price, quantity, status) VALUES (%s, %s, %s, %s, %s, %s)' if is_postgres_conn else \
                'INSERT INTO trades (symbol, order_id, side, entry_price, quantity, status) VALUES (?, ?, ?, ?, ?, ?)'
        cursor.execute(query, (symbol, order_id, side, price, quantity, "OPEN"))
        conn.commit()
        log.info(f"Paper trade recorded: Order ID {order_id}")
        cursor.close()
        conn.close()
        return {"order_id": order_id, "symbol": symbol, "side": side, "quantity": quantity, "price": price, "status": "FILLED"}

    elif side == "SELL" and existing_order_id:
        log.info(f"Simulating SELL order for {quantity} {symbol} at {price} (Type: {order_type}) for existing order {existing_order_id}")
        
        # First, get the entry price to calculate PnL
        query_entry = 'SELECT entry_price FROM trades WHERE order_id = %s' if is_postgres_conn else \
                      'SELECT entry_price FROM trades WHERE order_id = ?'
        cursor.execute(query_entry, (existing_order_id,))
        result = cursor.fetchone()
        if not result:
            log.error(f"Could not find existing order {existing_order_id} to calculate PnL.")
            cursor.close()
            conn.close()
            return {"status": "FAILED", "message": "Existing order not found"}
        
        entry_price = result[0]
        pnl = (price - entry_price) * quantity
        
        # Now, update the trade record
        query_update = 'UPDATE trades SET status = %s, exit_price = %s, exit_timestamp = CURRENT_TIMESTAMP, pnl = %s WHERE order_id = %s' if is_postgres_conn else \
                       'UPDATE trades SET status = ?, exit_price = ?, exit_timestamp = CURRENT_TIMESTAMP, pnl = ? WHERE order_id = ?'
        cursor.execute(query_update, ("CLOSED", price, pnl, existing_order_id))
        conn.commit()
        
        log.info(f"Paper trade {existing_order_id} updated to CLOSED at {price}. PnL: ${pnl:.2f}")
        cursor.close()
        conn.close()
        return {"order_id": existing_order_id, "status": "CLOSED", "pnl": pnl}
    
    log.warning(f"Invalid place_order call: side={side}, existing_order_id={existing_order_id}")
    cursor.close()
    conn.close()
    return {"status": "FAILED", "message": "Invalid order parameters"}

def get_open_positions() -> list:
    """
    Retrieves all currently open paper trading positions from the database.
    """
    conn = get_db_connection()
    is_postgres_conn = isinstance(conn, psycopg2.extensions.connection)
    if is_postgres_conn:
        from psycopg2.extras import RealDictCursor
        cursor = conn.cursor(cursor_factory=RealDictCursor)
    else:
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()

    query = 'SELECT * FROM trades WHERE status = %s' if is_postgres_conn else \
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
