import math
import os
import time
import sqlite3
from decimal import Decimal, ROUND_HALF_UP

import psycopg2
from src.logger import log
from src.config import app_config
from src.database import get_db_connection, release_db_connection, _cursor


# --- Binance Client (lazy-initialized) ---
_binance_client = None


def _get_binance_client():
    """Returns an authenticated Binance client (testnet or live). Lazy-initialized."""
    global _binance_client
    if _binance_client is not None:
        return _binance_client

    try:
        from binance.client import Client
    except ImportError:
        log.error("python-binance is not installed. Run: pip install python-binance")
        return None

    live_config = app_config.get('settings', {}).get('live_trading', {})
    mode = live_config.get('mode', 'testnet')

    if mode == 'testnet':
        api_key = os.environ.get('BINANCE_API_KEY_TESTNET')
        api_secret = os.environ.get('BINANCE_API_SECRET_TESTNET')
    else:
        api_key = os.environ.get('BINANCE_API_KEY')
        api_secret = os.environ.get('BINANCE_API_SECRET')

    if not api_key or not api_secret:
        suffix = '_TESTNET' if mode == 'testnet' else ''
        log.error(f"BINANCE_API_KEY{suffix} and BINANCE_API_SECRET{suffix} must be set for {mode} trading.")
        return None

    _binance_client = Client(api_key, api_secret, testnet=(mode == 'testnet'))
    log.info(f"Initialized Binance client (mode={mode})")
    return _binance_client


def _is_live_trading():
    """Returns True if live trading is active (not paper trading)."""
    settings = app_config.get('settings', {})
    if settings.get('paper_trading', True):
        return False
    live_config = settings.get('live_trading', {})
    return live_config.get('enabled', False)


def _get_trading_mode():
    """Returns the current trading mode string: 'paper', 'testnet', or 'live'."""
    settings = app_config.get('settings', {})
    if settings.get('paper_trading', True):
        return 'paper'
    live_config = settings.get('live_trading', {})
    if not live_config.get('enabled', False):
        return 'paper'
    return live_config.get('mode', 'testnet')


# --- Position Sizing Helpers ---

def _round_to_step_size(quantity, step_size):
    """Round quantity down to Binance's lot step size."""
    if step_size <= 0:
        return quantity
    precision = max(0, -int(round(math.log10(float(step_size)))))
    return math.floor(quantity * 10**precision) / 10**precision


def _get_symbol_info(symbol):
    """Fetches symbol trading rules from Binance (lot size, min notional)."""
    client = _get_binance_client()
    if not client:
        return None
    try:
        info = client.get_symbol_info(symbol)
        if not info:
            return None
        result = {'symbol': symbol, 'filters': {}}
        for f in info.get('filters', []):
            result['filters'][f['filterType']] = f
        return result
    except Exception as e:
        log.error(f"Failed to get symbol info for {symbol}: {e}")
        return None


def _validate_order_quantity(symbol_info, quantity, price):
    """
    Validates and adjusts order quantity against Binance's trading rules.
    Returns adjusted quantity or None if order cannot be placed.
    """
    if not symbol_info:
        return quantity

    filters = symbol_info.get('filters', {})

    # LOT_SIZE filter
    lot_size = filters.get('LOT_SIZE', {})
    step_size = float(lot_size.get('stepSize', 0))
    min_qty = float(lot_size.get('minQty', 0))
    max_qty = float(lot_size.get('maxQty', float('inf')))

    if step_size > 0:
        quantity = _round_to_step_size(quantity, step_size)

    if quantity < min_qty:
        log.warning(f"Quantity {quantity} below min {min_qty} for {symbol_info['symbol']}")
        return None
    if quantity > max_qty:
        quantity = max_qty

    # NOTIONAL / MIN_NOTIONAL filter
    notional = filters.get('NOTIONAL', filters.get('MIN_NOTIONAL', {}))
    min_notional = float(notional.get('minNotional', 0))
    if min_notional > 0 and quantity * price < min_notional:
        log.warning(f"Order notional ${quantity * price:.2f} below minimum ${min_notional:.2f}")
        return None

    return quantity


# --- Order Placement ---

def place_order(symbol, side, quantity, price, order_type="MARKET", existing_order_id=None):
    """
    Places an order — dispatches to paper or live based on config.
    """
    if _is_live_trading():
        return _live_place_order(symbol, side, quantity, price, order_type, existing_order_id)
    else:
        return _paper_place_order(symbol, side, quantity, price, order_type, existing_order_id)


def _paper_place_order(symbol, side, quantity, price, order_type="MARKET", existing_order_id=None):
    """Simulates placing an order for paper trading. Records the trade in the database."""
    conn = None
    cursor = None
    try:
        conn = get_db_connection()
        is_postgres_conn = isinstance(conn, psycopg2.extensions.connection)
        cursor = conn.cursor()

        if side == "BUY":
            log.info(f"Simulating BUY order for {quantity} {symbol} at {price} (Type: {order_type})")
            order_id = f"PAPER_{symbol}_BUY_{int(time.time() * 1000)}"
            query = ('INSERT INTO trades (symbol, order_id, side, entry_price, quantity, status, trading_mode) '
                     'VALUES (%s, %s, %s, %s, %s, %s, %s)') if is_postgres_conn else \
                    ('INSERT INTO trades (symbol, order_id, side, entry_price, quantity, status, trading_mode) '
                     'VALUES (?, ?, ?, ?, ?, ?, ?)')
            cursor.execute(query, (symbol, order_id, side, price, quantity, "OPEN", "paper"))
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

            query_update = ('UPDATE trades SET status = %s, exit_price = %s, exit_timestamp = CURRENT_TIMESTAMP, pnl = %s '
                            'WHERE order_id = %s') if is_postgres_conn else \
                           ('UPDATE trades SET status = ?, exit_price = ?, exit_timestamp = CURRENT_TIMESTAMP, pnl = ? '
                            'WHERE order_id = ?')
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


def _live_place_order(symbol, side, quantity, price, order_type="MARKET", existing_order_id=None):
    """Places a real order on Binance (testnet or live)."""
    from binance.exceptions import BinanceAPIException

    client = _get_binance_client()
    if not client:
        return {"status": "FAILED", "message": "Binance client not available"}

    trading_mode = _get_trading_mode()
    api_symbol = symbol if "USDT" in symbol else f"{symbol}USDT"

    try:
        if side == "BUY":
            # Validate quantity against Binance trading rules
            sym_info = _get_symbol_info(api_symbol)
            adjusted_qty = _validate_order_quantity(sym_info, quantity, price)
            if adjusted_qty is None:
                return {"status": "FAILED", "message": f"Order quantity too small for {api_symbol}"}

            log.info(f"[{trading_mode.upper()}] Placing BUY order: {adjusted_qty} {api_symbol} at ~${price}")
            order = client.order_market_buy(symbol=api_symbol, quantity=adjusted_qty)

            # Extract fill details
            fill_price = _extract_fill_price(order)
            fill_qty = float(order.get('executedQty', adjusted_qty))
            fees = _extract_fees(order)
            exchange_order_id = str(order.get('orderId', ''))

            # Record in DB
            order_id = f"{trading_mode.upper()}_{symbol}_BUY_{int(time.time() * 1000)}"
            _record_live_trade(symbol, order_id, "BUY", fill_price or price, fill_qty,
                               trading_mode, exchange_order_id, fees, fill_price, fill_qty)

            # Place OCO bracket for stop-loss and take-profit
            oco_result = _place_oco_bracket(api_symbol, fill_price or price, fill_qty)

            result = {
                "order_id": order_id, "exchange_order_id": exchange_order_id,
                "symbol": symbol, "side": "BUY", "quantity": fill_qty,
                "price": fill_price or price, "fees": fees,
                "status": "FILLED", "trading_mode": trading_mode,
            }
            if oco_result:
                result["oco"] = oco_result
            return result

        elif side == "SELL":
            # Cancel any existing OCO on this symbol first
            _cancel_open_oco_orders(api_symbol)

            sym_info = _get_symbol_info(api_symbol)
            adjusted_qty = _validate_order_quantity(sym_info, quantity, price)
            if adjusted_qty is None:
                return {"status": "FAILED", "message": f"Order quantity too small for {api_symbol}"}

            log.info(f"[{trading_mode.upper()}] Placing SELL order: {adjusted_qty} {api_symbol} at ~${price}")
            order = client.order_market_sell(symbol=api_symbol, quantity=adjusted_qty)

            fill_price = _extract_fill_price(order)
            fill_qty = float(order.get('executedQty', adjusted_qty))
            fees = _extract_fees(order)
            exchange_order_id = str(order.get('orderId', ''))

            # Update existing trade in DB
            if existing_order_id:
                _close_live_trade(existing_order_id, fill_price or price, fees, fill_price, fill_qty)

            return {
                "order_id": existing_order_id or exchange_order_id,
                "exchange_order_id": exchange_order_id,
                "symbol": symbol, "side": "SELL", "quantity": fill_qty,
                "price": fill_price or price, "fees": fees,
                "status": "CLOSED", "trading_mode": trading_mode,
            }

        return {"status": "FAILED", "message": "Invalid order parameters"}

    except BinanceAPIException as e:
        log.error(f"Binance API error in live order: {e}", exc_info=True)
        return {"status": "FAILED", "message": f"Binance API error: {e.message}"}
    except Exception as e:
        log.error(f"Unexpected error in live order: {e}", exc_info=True)
        return {"status": "FAILED", "message": str(e)}


def _extract_fill_price(order):
    """Extracts weighted average fill price from Binance order response."""
    fills = order.get('fills', [])
    if not fills:
        return None
    total_qty = sum(float(f['qty']) for f in fills)
    if total_qty == 0:
        return None
    weighted_price = sum(float(f['price']) * float(f['qty']) for f in fills)
    return weighted_price / total_qty


def _extract_fees(order):
    """Extracts total fees from Binance order response."""
    fills = order.get('fills', [])
    total_fees = 0.0
    for f in fills:
        commission = float(f.get('commission', 0))
        # Approximate: if commission asset is not USDT, this is approximate
        total_fees += commission
    return total_fees


def _record_live_trade(symbol, order_id, side, entry_price, quantity,
                       trading_mode, exchange_order_id, fees, fill_price, fill_qty):
    """Records a live trade in the database."""
    conn = None
    try:
        conn = get_db_connection()
        is_pg = isinstance(conn, psycopg2.extensions.connection)
        with _cursor(conn) as cursor:
            query = (
                'INSERT INTO trades (symbol, order_id, side, entry_price, quantity, status, '
                'trading_mode, exchange_order_id, fees, fill_price, fill_quantity) '
                'VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)'
            ) if is_pg else (
                'INSERT INTO trades (symbol, order_id, side, entry_price, quantity, status, '
                'trading_mode, exchange_order_id, fees, fill_price, fill_quantity) '
                'VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)'
            )
            cursor.execute(query, (
                symbol, order_id, side, entry_price, quantity, "OPEN",
                trading_mode, exchange_order_id, fees, fill_price, fill_qty
            ))
        conn.commit()
        log.info(f"Recorded {trading_mode} trade: {order_id} (exchange: {exchange_order_id})")
    except (sqlite3.Error, psycopg2.Error) as e:
        log.error(f"DB error recording live trade: {e}", exc_info=True)
        if conn:
            conn.rollback()
    finally:
        release_db_connection(conn)


def _close_live_trade(order_id, exit_price, fees, fill_price, fill_qty):
    """Closes an existing trade with fill details and PnL."""
    conn = None
    try:
        conn = get_db_connection()
        is_pg = isinstance(conn, psycopg2.extensions.connection)
        with _cursor(conn) as cursor:
            # Fetch entry price for PnL calculation
            q_entry = ('SELECT entry_price, side, quantity FROM trades WHERE order_id = %s'
                       if is_pg else
                       'SELECT entry_price, side, quantity FROM trades WHERE order_id = ?')
            cursor.execute(q_entry, (order_id,))
            row = cursor.fetchone()
            if not row:
                log.error(f"Cannot close trade — order {order_id} not found")
                return

            entry_price = float(row[0])
            trade_side = row[1]
            qty = float(row[2])

            d_exit = Decimal(str(exit_price))
            d_entry = Decimal(str(entry_price))
            d_qty = Decimal(str(qty))
            d_fees = Decimal(str(fees))

            if trade_side == "BUY":
                pnl = (d_exit - d_entry) * d_qty - d_fees
            else:
                pnl = (d_entry - d_exit) * d_qty - d_fees

            pnl_float = float(pnl.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))

            q_update = (
                'UPDATE trades SET status = %s, exit_price = %s, exit_timestamp = CURRENT_TIMESTAMP, '
                'pnl = %s, fees = %s, fill_price = %s, fill_quantity = %s WHERE order_id = %s'
            ) if is_pg else (
                'UPDATE trades SET status = ?, exit_price = ?, exit_timestamp = CURRENT_TIMESTAMP, '
                'pnl = ?, fees = ?, fill_price = ?, fill_quantity = ? WHERE order_id = ?'
            )
            cursor.execute(q_update, ("CLOSED", exit_price, pnl_float, fees,
                                      fill_price, fill_qty, order_id))
        conn.commit()
        log.info(f"Closed trade {order_id}: exit=${exit_price}, PnL=${pnl_float:.2f}, fees=${fees:.4f}")
    except (sqlite3.Error, psycopg2.Error) as e:
        log.error(f"DB error closing live trade: {e}", exc_info=True)
        if conn:
            conn.rollback()
    finally:
        release_db_connection(conn)


# --- OCO Bracket Orders ---

def _place_oco_bracket(symbol, entry_price, quantity):
    """
    Places an OCO (One-Cancels-Other) order with stop-loss and take-profit
    after a BUY fill. Runs server-side on Binance for protection during bot downtime.
    """
    client = _get_binance_client()
    if not client:
        return None

    live_config = app_config.get('settings', {}).get('live_trading', {})
    sl_pct = live_config.get('stop_loss_percentage', 0.03)
    tp_pct = live_config.get('take_profit_percentage', 0.06)

    stop_price = round(entry_price * (1 - sl_pct), 8)
    stop_limit_price = round(stop_price * 0.998, 8)  # slightly below stop for limit fill
    take_profit_price = round(entry_price * (1 + tp_pct), 8)

    # Round prices and quantity to symbol's precision
    sym_info = _get_symbol_info(symbol)
    if sym_info:
        filters = sym_info.get('filters', {})
        price_filter = filters.get('PRICE_FILTER', {})
        tick_size = float(price_filter.get('tickSize', 0))
        if tick_size > 0:
            precision = max(0, -int(round(math.log10(tick_size))))
            stop_price = round(stop_price, precision)
            stop_limit_price = round(stop_limit_price, precision)
            take_profit_price = round(take_profit_price, precision)

        lot_filter = filters.get('LOT_SIZE', {})
        step = float(lot_filter.get('stepSize', 0))
        if step > 0:
            quantity = _round_to_step_size(quantity, step)

    try:
        from binance.exceptions import BinanceAPIException
        log.info(f"Placing OCO bracket for {symbol}: TP=${take_profit_price}, SL=${stop_price}, qty={quantity}")
        oco = client.create_oco_order(
            symbol=symbol,
            side='SELL',
            quantity=quantity,
            price=str(take_profit_price),
            stopPrice=str(stop_price),
            stopLimitPrice=str(stop_limit_price),
            stopLimitTimeInForce='GTC',
        )
        log.info(f"OCO bracket placed: orderListId={oco.get('orderListId')}")
        return {
            "order_list_id": oco.get('orderListId'),
            "take_profit": take_profit_price,
            "stop_loss": stop_price,
        }
    except BinanceAPIException as e:
        log.error(f"Failed to place OCO bracket: {e}")
        return None
    except Exception as e:
        log.error(f"Unexpected error placing OCO bracket: {e}")
        return None


def _cancel_open_oco_orders(symbol):
    """Cancels all open OCO orders for a symbol before placing a manual sell."""
    client = _get_binance_client()
    if not client:
        return
    try:
        open_orders = client.get_open_orders(symbol=symbol)
        for order in open_orders:
            if order.get('orderListId', -1) != -1:
                client.cancel_order(symbol=symbol, orderId=order['orderId'])
                log.info(f"Cancelled OCO order {order['orderId']} for {symbol}")
    except Exception as e:
        log.warning(f"Error cancelling OCO orders for {symbol}: {e}")


# --- Position & Balance Queries ---

def get_open_positions():
    """Retrieves all currently open trading positions from the database."""
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

        log.info(f"Retrieved {len(positions)} open positions.")
        return positions
    except (sqlite3.Error, psycopg2.Error) as e:
        log.error(f"Database error in get_open_positions: {e}", exc_info=True)
        return []
    finally:
        if cursor:
            cursor.close()
        release_db_connection(conn)


def get_account_balance():
    """
    Returns account balance — paper-based calculation or real Binance balance.
    """
    if _is_live_trading():
        return _get_live_balance()
    return _get_paper_balance()


def _get_paper_balance():
    """Calculates the current paper trading balance based on initial capital and closed trade PnL."""
    initial_capital = Decimal(str(app_config.get('settings', {}).get('paper_trading_initial_capital', 10000.0)))
    conn = None
    try:
        conn = get_db_connection()
        is_postgres_conn = isinstance(conn, psycopg2.extensions.connection)
        with _cursor(conn) as cursor:
            query_pnl = 'SELECT COALESCE(SUM(pnl), 0) FROM trades WHERE status = %s' if is_postgres_conn else \
                        'SELECT COALESCE(SUM(pnl), 0) FROM trades WHERE status = ?'
            cursor.execute(query_pnl, ("CLOSED",))
            total_pnl = Decimal(str(cursor.fetchone()[0]))

            query_open = 'SELECT COALESCE(SUM(entry_price * quantity), 0) FROM trades WHERE status = %s' if is_postgres_conn else \
                         'SELECT COALESCE(SUM(entry_price * quantity), 0) FROM trades WHERE status = ?'
            cursor.execute(query_open, ("OPEN",))
            locked_capital = Decimal(str(cursor.fetchone()[0]))

        available = initial_capital + total_pnl - locked_capital
        total = initial_capital + total_pnl
        available_f = float(available.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))
        total_f = float(total.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))
        log.info(f"Paper trading balance: available=${available_f:.2f}, total=${total_f:.2f}")
        return {"USDT": available_f, "total_usd": total_f}
    except (sqlite3.Error, psycopg2.Error) as e:
        log.error(f"Database error in get_account_balance: {e}", exc_info=True)
        fallback = float(initial_capital)
        return {"USDT": fallback, "total_usd": fallback}
    finally:
        release_db_connection(conn)


def _get_live_balance():
    """Fetches the real USDT balance from Binance."""
    client = _get_binance_client()
    if not client:
        log.error("Cannot fetch live balance — Binance client not available")
        return {"USDT": 0.0, "total_usd": 0.0}
    try:
        balance_info = client.get_asset_balance(asset='USDT')
        free = float(balance_info.get('free', 0))
        locked = float(balance_info.get('locked', 0))
        total = free + locked
        log.info(f"Live Binance balance: free=${free:.2f}, locked=${locked:.2f}, total=${total:.2f}")
        return {"USDT": free, "total_usd": total}
    except Exception as e:
        log.error(f"Failed to fetch Binance balance: {e}")
        return {"USDT": 0.0, "total_usd": 0.0}


if __name__ == '__main__':
    log.info("--- Testing Binance Trader Module ---")
    mode = _get_trading_mode()
    log.info(f"Current trading mode: {mode}")
    balance = get_account_balance()
    log.info(f"Balance: {balance}")

    buy_order = place_order("BTC", "BUY", 0.001, 30000.0)
    log.info(f"Buy Order: {buy_order}")

    open_positions = get_open_positions()
    log.info(f"Open Positions: {open_positions}")
    log.info("--- Test Complete ---")
