import time
import sqlite3
from decimal import Decimal, ROUND_HALF_UP

import psycopg2
from src.logger import log
from src.config import app_config
from src.database import get_db_connection, release_db_connection, _cursor

def place_order(symbol: str, side: str, quantity: float, price: float, order_type: str = "MARKET", existing_order_id: str = None) -> dict:
    """
    Simulates placing an order for paper trading.
    Records the trade in the database.
    """
    conn = None
    cursor = None
    try:
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
            return {"order_id": order_id, "symbol": symbol, "side": side, "quantity": quantity, "price": price, "status": "FILLED"}

        elif side == "SELL" and existing_order_id:
            log.info(f"Simulating SELL order for {quantity} {symbol} at {price} (Type: {order_type}) for existing order {existing_order_id}")

            query_entry = 'SELECT entry_price, side FROM trades WHERE order_id = %s' if is_postgres_conn else \
                          'SELECT entry_price, side FROM trades WHERE order_id = ?'
            cursor.execute(query_entry, (existing_order_id,))
            result = cursor.fetchone()
            if not result:
                log.error(f"Could not find existing order {existing_order_id} to calculate PnL.")
                return {"status": "FAILED", "message": "Existing order not found"}

            entry_price, trade_side = result[0], result[1]

            d_price = Decimal(str(price))
            d_entry = Decimal(str(entry_price))
            d_qty = Decimal(str(quantity))

            if trade_side == "BUY":
                pnl = (d_price - d_entry) * d_qty
            elif trade_side == "SELL":
                pnl = (d_entry - d_price) * d_qty
            else:
                pnl = Decimal("0")

            pnl_float = float(pnl.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))

            query_update = 'UPDATE trades SET status = %s, exit_price = %s, exit_timestamp = CURRENT_TIMESTAMP, pnl = %s WHERE order_id = %s' if is_postgres_conn else \
                           'UPDATE trades SET status = ?, exit_price = ?, exit_timestamp = CURRENT_TIMESTAMP, pnl = ? WHERE order_id = ?'
            cursor.execute(query_update, ("CLOSED", price, pnl_float, existing_order_id))
            conn.commit()

            log.info(f"Paper trade {existing_order_id} updated to CLOSED at {price}. PnL: ${pnl_float:.2f}")
            return {"order_id": existing_order_id, "status": "CLOSED", "pnl": pnl_float}

        log.warning(f"Invalid place_order call: side={side}, existing_order_id={existing_order_id}")
        return {"status": "FAILED", "message": "Invalid order parameters"}
    except (sqlite3.Error, psycopg2.Error) as e:
        log.error(f"Database error in place_order: {e}", exc_info=True)
        if conn:
            conn.rollback()
        return {"status": "FAILED", "message": str(e)}
    finally:
        if cursor:
            cursor.close()
        release_db_connection(conn)

def get_open_positions() -> list:
    """
    Retrieves all currently open paper trading positions from the database.
    """
    conn = None
    cursor = None
    try:
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

        log.info(f"Retrieved {len(positions)} open paper positions.")
        return positions
    except (sqlite3.Error, psycopg2.Error) as e:
        log.error(f"Database error in get_open_positions: {e}", exc_info=True)
        return []
    finally:
        if cursor:
            cursor.close()
        release_db_connection(conn)

def get_account_balance() -> dict:
    """
    Calculates the current paper trading balance based on initial capital and closed trade PnL.
    """
    initial_capital = Decimal(str(app_config.get('settings', {}).get('paper_trading_initial_capital', 10000.0)))
    conn = None
    try:
        conn = get_db_connection()
        is_postgres_conn = isinstance(conn, psycopg2.extensions.connection)
        with _cursor(conn) as cursor:
            # Sum PnL from all closed trades
            query_pnl = 'SELECT COALESCE(SUM(pnl), 0) FROM trades WHERE status = %s' if is_postgres_conn else \
                        'SELECT COALESCE(SUM(pnl), 0) FROM trades WHERE status = ?'
            cursor.execute(query_pnl, ("CLOSED",))
            total_pnl = Decimal(str(cursor.fetchone()[0]))

            # Sum capital locked in open positions
            query_open = 'SELECT COALESCE(SUM(entry_price * quantity), 0) FROM trades WHERE status = %s' if is_postgres_conn else \
                         'SELECT COALESCE(SUM(entry_price * quantity), 0) FROM trades WHERE status = ?'
            cursor.execute(query_open, ("OPEN",))
            locked_capital = Decimal(str(cursor.fetchone()[0]))

        available = initial_capital + total_pnl - locked_capital
        total = initial_capital + total_pnl
        available_f = float(available.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))
        total_f = float(total.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))
        log.info(f"Paper trading balance: available=${available_f:.2f}, total=${total_f:.2f} (PnL=${float(total_pnl):.2f}, locked=${float(locked_capital):.2f})")
        return {"USDT": available_f, "total_usd": available_f}
    except (sqlite3.Error, psycopg2.Error) as e:
        log.error(f"Database error in get_account_balance: {e}", exc_info=True)
        fallback = float(initial_capital)
        return {"USDT": fallback, "total_usd": fallback}
    finally:
        release_db_connection(conn)


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
