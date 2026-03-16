import math
import os
import random
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

def place_order(symbol, side, quantity, price, order_type="MARKET", existing_order_id=None, asset_type="crypto", trading_strategy="manual", strategy_type=None, trade_reason=None, exit_reason=None, dynamic_sl_pct=None, dynamic_tp_pct=None):
    """
    Places an order — dispatches to paper or live based on config.
    """
    if price <= 0:
        return {"status": "FAILED", "message": "Invalid price"}
    kw = dict(asset_type=asset_type, trading_strategy=trading_strategy, strategy_type=strategy_type, trade_reason=trade_reason, exit_reason=exit_reason, dynamic_sl_pct=dynamic_sl_pct, dynamic_tp_pct=dynamic_tp_pct)
    # Stocks always use paper path — Binance live API is crypto-only
    if asset_type == 'stock':
        return _paper_place_order(symbol, side, quantity, price, order_type, existing_order_id, **kw)
    if _is_live_trading() and trading_strategy != 'auto':
        return _live_place_order(symbol, side, quantity, price, order_type, existing_order_id, **kw)
    else:
        return _paper_place_order(symbol, side, quantity, price, order_type, existing_order_id, **kw)


def _paper_place_order(symbol, side, quantity, price, order_type="MARKET", existing_order_id=None, asset_type="crypto", trading_strategy="manual", strategy_type=None, trade_reason=None, exit_reason=None, dynamic_sl_pct=None, dynamic_tp_pct=None):
    """Simulates placing an order for paper trading. Records the trade in the database."""
    conn = None
    cursor = None
    try:
        conn = get_db_connection()
        is_postgres_conn = isinstance(conn, psycopg2.extensions.connection)
        cursor = conn.cursor()

        if side == "BUY":
            # LIMIT orders → create PENDING row (no slippage, no fill yet)
            if order_type == "LIMIT":
                prefix = "AUTO" if trading_strategy == "auto" else "PAPER"
                order_id = f"{prefix}_{symbol}_LIMIT_{int(time.time() * 1000)}"

                # Compute expiry
                limit_cfg = app_config.get('settings', {}).get('limit_orders', {})
                ttl_cycles = limit_cfg.get('ttl_cycles', 8)
                run_interval = app_config.get('settings', {}).get('run_interval_minutes', 15)
                from datetime import datetime, timedelta, timezone
                expires_at = datetime.now(timezone.utc) + timedelta(minutes=ttl_cycles * run_interval)

                ph = '%s' if is_postgres_conn else '?'
                query = (f'INSERT INTO trades (symbol, order_id, side, entry_price, quantity, status, '
                         f'trading_mode, asset_type, trading_strategy, strategy_type, trade_reason, '
                         f'order_type, limit_price, limit_expires_at, dynamic_sl_pct, dynamic_tp_pct) '
                         f'VALUES ({", ".join([ph]*16)})')
                cursor.execute(query, (
                    symbol, order_id, side, price, quantity, "PENDING", "paper",
                    asset_type, trading_strategy, strategy_type, trade_reason,
                    "LIMIT", price, expires_at, dynamic_sl_pct, dynamic_tp_pct))
                conn.commit()
                log.info(f"Limit order recorded: {order_id} (limit=${price:.4f}, expires={expires_at})")
                return {"order_id": order_id, "symbol": symbol, "side": side,
                        "quantity": quantity, "price": price, "status": "PENDING",
                        "order_type": "LIMIT"}

            # MARKET orders: apply simulated slippage
            slippage_pct = app_config.get('settings', {}).get('simulated_slippage_pct', 0.001)
            fill_price = price * (1 + slippage_pct)
            log.info(f"Simulating BUY order for {quantity} {symbol} at {price} (Type: {order_type})")
            log.info(f"Paper fill with {slippage_pct*100:.2f}% slippage: requested ${price:.4f} → filled ${fill_price:.4f}")
            prefix = "AUTO" if trading_strategy == "auto" else "PAPER"
            order_id = f"{prefix}_{symbol}_BUY_{int(time.time() * 1000)}"
            query = ('INSERT INTO trades (symbol, order_id, side, entry_price, quantity, status, trading_mode, asset_type, trading_strategy, strategy_type, trade_reason, dynamic_sl_pct, dynamic_tp_pct) '
                     'VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)') if is_postgres_conn else \
                    ('INSERT INTO trades (symbol, order_id, side, entry_price, quantity, status, trading_mode, asset_type, trading_strategy, strategy_type, trade_reason, dynamic_sl_pct, dynamic_tp_pct) '
                     'VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)')
            cursor.execute(query, (symbol, order_id, side, fill_price, quantity, "OPEN", "paper", asset_type, trading_strategy, strategy_type, trade_reason, dynamic_sl_pct, dynamic_tp_pct))
            conn.commit()
            log.info(f"Paper trade recorded: Order ID {order_id}" + (f" [strategic: {strategy_type}]" if strategy_type else "")
                     + (f" [SL={dynamic_sl_pct:.2%}, TP={dynamic_tp_pct:.2%}]" if dynamic_sl_pct else ""))
            return {"order_id": order_id, "symbol": symbol, "side": side, "quantity": quantity, "price": fill_price, "status": "FILLED"}

        elif side == "SELL" and existing_order_id:
            # Apply simulated slippage: SELL fills lower than requested
            slippage_pct = app_config.get('settings', {}).get('simulated_slippage_pct', 0.001)
            fill_price = price * (1 - slippage_pct)
            log.info(f"Simulating SELL order for {quantity} {symbol} at {price} (Type: {order_type}) for existing order {existing_order_id}")
            log.info(f"Paper fill with {slippage_pct*100:.2f}% slippage: requested ${price:.4f} → filled ${fill_price:.4f}")

            query_entry = 'SELECT entry_price, side FROM trades WHERE order_id = %s' if is_postgres_conn else \
                          'SELECT entry_price, side FROM trades WHERE order_id = ?'
            cursor.execute(query_entry, (existing_order_id,))
            result = cursor.fetchone()
            if not result:
                log.error(f"Could not find existing order {existing_order_id} to calculate PnL.")
                return {"status": "FAILED", "message": "Existing order not found"}

            entry_price, trade_side = result[0], result[1]

            d_price = Decimal(str(fill_price))
            d_entry = Decimal(str(entry_price))
            d_qty = Decimal(str(quantity))

            if trade_side == "BUY":
                pnl = (d_price - d_entry) * d_qty
            elif trade_side == "SELL":
                pnl = (d_entry - d_price) * d_qty
            else:
                pnl = Decimal("0")

            # Deduct simulated round-trip fees (entry fill + exit fill),
            # matching live Binance behavior where both sides are charged
            fee_pct = Decimal(str(app_config.get('settings', {}).get('simulated_fee_pct', 0.001)))
            simulated_fees = d_price * d_qty * fee_pct + d_entry * d_qty * fee_pct
            pnl -= simulated_fees

            pnl_float = float(pnl.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))

            query_update = ('UPDATE trades SET status = %s, exit_price = %s, exit_timestamp = CURRENT_TIMESTAMP, pnl = %s, exit_reason = %s '
                            'WHERE order_id = %s') if is_postgres_conn else \
                           ('UPDATE trades SET status = ?, exit_price = ?, exit_timestamp = CURRENT_TIMESTAMP, pnl = ?, exit_reason = ? '
                            'WHERE order_id = ?')
            cursor.execute(query_update, ("CLOSED", fill_price, pnl_float, exit_reason, existing_order_id))
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


def _live_place_order(symbol, side, quantity, price, order_type="MARKET", existing_order_id=None, asset_type="crypto", trading_strategy="manual", strategy_type=None, trade_reason=None, exit_reason=None, dynamic_sl_pct=None, dynamic_tp_pct=None):
    """Places a real order on Binance (testnet or live)."""
    from binance.exceptions import BinanceAPIException

    client = _get_binance_client()
    if not client:
        return {"status": "FAILED", "message": "Binance client not available"}

    trading_mode = _get_trading_mode()
    api_symbol = symbol if symbol.endswith("USDT") else f"{symbol}USDT"

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

            # Partial fill detection
            fill_ratio = fill_qty / adjusted_qty if adjusted_qty > 0 else 1.0
            if fill_ratio < 0.95:
                log.warning(f"Partial fill for {symbol} BUY: requested {adjusted_qty}, "
                            f"filled {fill_qty} ({fill_ratio:.1%})")

            # Record in DB (use fill_qty, not adjusted_qty)
            order_id = f"{trading_mode.upper()}_{symbol}_BUY_{int(time.time() * 1000)}"
            _record_live_trade(symbol, order_id, "BUY", fill_price or price, fill_qty,
                               trading_mode, exchange_order_id, fees, fill_price, fill_qty,
                               asset_type=asset_type,
                               strategy_type=strategy_type, trade_reason=trade_reason)

            # Place OCO bracket for stop-loss and take-profit
            oco_result = _place_oco_with_retry(api_symbol, fill_price or price, fill_qty,
                                                sl_pct=dynamic_sl_pct, tp_pct=dynamic_tp_pct)

            if not oco_result:
                log.warning(f"OCO bracket failed for {symbol} after BUY — "
                            f"attempting emergency market close")
                emergency = _emergency_market_close(
                    api_symbol, fill_qty,
                    reason="OCO+fallback both failed after BUY")

                if emergency:
                    # Emergency close succeeded — update DB to CLOSED
                    _close_live_trade(order_id, emergency['fill_price'] or (fill_price or price),
                                     fees, emergency['fill_price'], emergency['fill_qty'],
                                     exit_reason="emergency_oco_failure")
                    try:
                        from src.notify.telegram_bot import send_telegram_alert
                        import asyncio
                        loop = asyncio.get_event_loop()
                        if loop.is_running():
                            asyncio.ensure_future(send_telegram_alert({
                                'signal': 'WARNING', 'symbol': symbol,
                                'current_price': emergency.get('fill_price') or (fill_price or price),
                                'reason': ('OCO bracket + fallback SL both failed after BUY. '
                                           'Position was EMERGENCY CLOSED at market price.'),
                            }))
                    except Exception as e:
                        log.debug(f"Emergency close alert send failed: {e}")
                    return {
                        "order_id": order_id, "exchange_order_id": exchange_order_id,
                        "symbol": symbol, "side": "BUY", "quantity": fill_qty,
                        "price": fill_price or price, "fees": fees,
                        "status": "EMERGENCY_CLOSED", "trading_mode": trading_mode,
                    }
                else:
                    # Emergency close also failed — position is truly naked
                    log.critical(f"NAKED POSITION: {symbol} qty={fill_qty} — "
                                 f"OCO, fallback SL, and emergency close ALL failed")
                    try:
                        from src.notify.telegram_bot import send_telegram_alert
                        import asyncio
                        loop = asyncio.get_event_loop()
                        if loop.is_running():
                            asyncio.ensure_future(send_telegram_alert({
                                'signal': 'CRITICAL', 'symbol': symbol,
                                'current_price': fill_price or price,
                                'reason': ('ALL protection failed after BUY (OCO, fallback SL, '
                                           'emergency close). Position is UNPROTECTED — '
                                           'manual intervention required!'),
                            }))
                    except Exception as e:
                        log.debug(f"Naked position alert send failed: {e}")
                    return {
                        "order_id": order_id, "exchange_order_id": exchange_order_id,
                        "symbol": symbol, "side": "BUY", "quantity": fill_qty,
                        "price": fill_price or price, "fees": fees,
                        "status": "FILLED", "trading_mode": trading_mode,
                        "unprotected": True,
                    }

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

            # Partial fill detection
            fill_ratio = fill_qty / adjusted_qty if adjusted_qty > 0 else 1.0
            if fill_ratio < 0.95:
                log.warning(f"Partial fill for {symbol} SELL: requested {adjusted_qty}, "
                            f"filled {fill_qty} ({fill_ratio:.1%})")

            # Update existing trade in DB
            if existing_order_id:
                _close_live_trade(existing_order_id, fill_price or price, fees, fill_price, fill_qty, exit_reason=exit_reason)

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
                       trading_mode, exchange_order_id, fees, fill_price, fill_qty,
                       asset_type="crypto", strategy_type=None, trade_reason=None):
    """Records a live trade in the database."""
    conn = None
    try:
        conn = get_db_connection()
        is_pg = isinstance(conn, psycopg2.extensions.connection)
        with _cursor(conn) as cursor:
            query = (
                'INSERT INTO trades (symbol, order_id, side, entry_price, quantity, status, '
                'trading_mode, exchange_order_id, fees, fill_price, fill_quantity, asset_type, '
                'strategy_type, trade_reason) '
                'VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)'
            ) if is_pg else (
                'INSERT INTO trades (symbol, order_id, side, entry_price, quantity, status, '
                'trading_mode, exchange_order_id, fees, fill_price, fill_quantity, asset_type, '
                'strategy_type, trade_reason) '
                'VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)'
            )
            cursor.execute(query, (
                symbol, order_id, side, entry_price, quantity, "OPEN",
                trading_mode, exchange_order_id, fees, fill_price, fill_qty, asset_type,
                strategy_type, trade_reason
            ))
        conn.commit()
        log.info(f"Recorded {trading_mode} trade: {order_id} (exchange: {exchange_order_id})")
    except (sqlite3.Error, psycopg2.Error) as e:
        log.error(f"DB error recording live trade: {e}", exc_info=True)
        if conn:
            conn.rollback()
    finally:
        release_db_connection(conn)


def _close_live_trade(order_id, exit_price, fees, fill_price, fill_qty, exit_reason=None):
    """Closes an existing trade with fill details and PnL."""
    conn = None
    try:
        conn = get_db_connection()
        is_pg = isinstance(conn, psycopg2.extensions.connection)
        with _cursor(conn) as cursor:
            # Fetch entry price for PnL calculation (prefer fill_quantity over quantity)
            q_entry = ('SELECT entry_price, side, quantity, fill_quantity FROM trades WHERE order_id = %s'
                       if is_pg else
                       'SELECT entry_price, side, quantity, fill_quantity FROM trades WHERE order_id = ?')
            cursor.execute(q_entry, (order_id,))
            row = cursor.fetchone()
            if not row:
                log.error(f"Cannot close trade — order {order_id} not found")
                return

            entry_price = float(row[0])
            trade_side = row[1]
            raw_qty = float(row[2])
            raw_fill_qty = row[3]
            qty = float(raw_fill_qty) if raw_fill_qty else raw_qty

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
                'pnl = %s, fees = %s, fill_price = %s, fill_quantity = %s, exit_reason = %s WHERE order_id = %s'
            ) if is_pg else (
                'UPDATE trades SET status = ?, exit_price = ?, exit_timestamp = CURRENT_TIMESTAMP, '
                'pnl = ?, fees = ?, fill_price = ?, fill_quantity = ?, exit_reason = ? WHERE order_id = ?'
            )
            cursor.execute(q_update, ("CLOSED", exit_price, pnl_float, fees,
                                      fill_price, fill_qty, exit_reason, order_id))
        conn.commit()
        log.info(f"Closed trade {order_id}: exit=${exit_price}, PnL=${pnl_float:.2f}, fees=${fees:.4f}")
    except (sqlite3.Error, psycopg2.Error) as e:
        log.error(f"DB error closing live trade: {e}", exc_info=True)
        if conn:
            conn.rollback()
    finally:
        release_db_connection(conn)


# --- OCO Bracket Orders ---

_OCO_MAX_RETRIES = 3
_OCO_RETRY_BASE_DELAY = 1.5


def _is_retryable_binance_error(exc) -> bool:
    """Check if a Binance error is transient and worth retrying."""
    type_name = type(exc).__name__
    if type_name == 'BinanceAPIException':
        code = getattr(exc, 'code', 0)
        if code in (-1015, -1001, -1003):  # too many orders, disconnected, rate limit
            return True
    msg = str(exc).lower()
    return any(s in msg for s in ('500', '503', 'timeout', 'connection', 'rate limit'))


def _place_oco_bracket(symbol, entry_price, quantity, sl_pct=None, tp_pct=None):
    """
    Places an OCO (One-Cancels-Other) order with stop-loss and take-profit
    after a BUY fill. Runs server-side on Binance for protection during bot downtime.

    sl_pct/tp_pct: per-position dynamic values (from ATR). Falls back to config.
    """
    client = _get_binance_client()
    if not client:
        return None

    live_config = app_config.get('settings', {}).get('live_trading', {})
    if sl_pct is None:
        sl_pct = live_config.get('stop_loss_percentage', 0.03)
    if tp_pct is None:
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

    log.info(f"Placing OCO bracket for {symbol}: TP=${take_profit_price}, SL=${stop_price}, qty={quantity}")
    oco = client.create_oco_order(
        symbol=symbol,
        side='SELL',
        quantity=quantity,
        aboveType='LIMIT_MAKER',
        abovePrice=str(take_profit_price),
        belowType='STOP_LOSS_LIMIT',
        belowPrice=str(stop_limit_price),
        belowStopPrice=str(stop_price),
        belowTimeInForce='GTC',
    )
    log.info(f"OCO bracket placed: orderListId={oco.get('orderListId')}")
    return {
        "order_list_id": oco.get('orderListId'),
        "take_profit": take_profit_price,
        "stop_loss": stop_price,
    }


def _place_oco_with_retry(symbol, entry_price, quantity, sl_pct=None, tp_pct=None):
    """Retry OCO bracket placement with exponential backoff.

    Falls back to plain stop-loss if all retries fail.
    """
    for attempt in range(1, _OCO_MAX_RETRIES + 1):
        try:
            return _place_oco_bracket(symbol, entry_price, quantity,
                                       sl_pct=sl_pct, tp_pct=tp_pct)
        except Exception as exc:
            if not _is_retryable_binance_error(exc) or attempt == _OCO_MAX_RETRIES:
                log.error(f"OCO bracket failed (attempt {attempt}/{_OCO_MAX_RETRIES}): {exc}")
                break
            delay = _OCO_RETRY_BASE_DELAY * (2 ** (attempt - 1)) + random.uniform(0, 0.5)
            log.warning(f"OCO bracket attempt {attempt} failed (retryable): {exc}. "
                        f"Retrying in {delay:.1f}s...")
            time.sleep(delay)

    # All retries exhausted — fall back to plain stop-loss
    log.warning(f"OCO bracket exhausted {_OCO_MAX_RETRIES} retries for {symbol}. "
                f"Attempting fallback stop-loss order.")
    return _place_fallback_stop_loss(symbol, entry_price, quantity)


def _place_fallback_stop_loss(symbol, entry_price, quantity):
    """Places a plain STOP_LOSS_LIMIT as fallback when OCO fails."""
    client = _get_binance_client()
    if not client:
        return None

    live_config = app_config.get('settings', {}).get('live_trading', {})
    sl_pct = live_config.get('stop_loss_percentage', 0.03)
    stop_price = round(entry_price * (1 - sl_pct), 8)
    stop_limit_price = round(stop_price * 0.998, 8)

    # Round to symbol precision
    sym_info = _get_symbol_info(symbol)
    if sym_info:
        filters = sym_info.get('filters', {})
        price_filter = filters.get('PRICE_FILTER', {})
        tick_size = float(price_filter.get('tickSize', 0))
        if tick_size > 0:
            precision = max(0, -int(round(math.log10(tick_size))))
            stop_price = round(stop_price, precision)
            stop_limit_price = round(stop_limit_price, precision)

        lot_filter = filters.get('LOT_SIZE', {})
        step = float(lot_filter.get('stepSize', 0))
        if step > 0:
            quantity = _round_to_step_size(quantity, step)

    try:
        log.info(f"Placing fallback STOP_LOSS_LIMIT for {symbol}: "
                 f"SL=${stop_price}, qty={quantity}")
        order = client.create_order(
            symbol=symbol,
            side='SELL',
            type='STOP_LOSS_LIMIT',
            timeInForce='GTC',
            quantity=quantity,
            price=str(stop_limit_price),
            stopPrice=str(stop_price),
        )
        log.info(f"Fallback stop-loss placed: orderId={order.get('orderId')}")
        return {
            "order_id": order.get('orderId'),
            "stop_loss": stop_price,
            "fallback": True,
        }
    except Exception as e:
        log.error(f"Fallback stop-loss also failed for {symbol}: {e}")
        return None


def _emergency_market_close(symbol, quantity, reason=""):
    """Emergency market sell when OCO+fallback both fail. Better to close at ~breakeven than hold naked."""
    client = _get_binance_client()
    if not client:
        log.critical(f"EMERGENCY CLOSE FAILED for {symbol}: no Binance client")
        return None

    # Round quantity to symbol precision
    sym_info = _get_symbol_info(symbol)
    if sym_info:
        lot_filter = sym_info.get('filters', {}).get('LOT_SIZE', {})
        step = float(lot_filter.get('stepSize', 0))
        if step > 0:
            quantity = _round_to_step_size(quantity, step)

    try:
        log.warning(f"EMERGENCY MARKET SELL for {symbol}: qty={quantity}, reason={reason}")
        order = client.order_market_sell(symbol=symbol, quantity=quantity)
        fill_price = _extract_fill_price(order)
        fill_qty = float(order.get('executedQty', quantity))
        log.warning(f"Emergency close completed for {symbol}: "
                    f"filled {fill_qty} at ${fill_price}")
        return {
            "order_id": order.get('orderId'),
            "fill_price": fill_price,
            "fill_qty": fill_qty,
            "emergency": True,
        }
    except Exception as e:
        log.critical(f"EMERGENCY CLOSE FAILED for {symbol}: {e}", exc_info=True)
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


# --- Position Increase ---

def add_to_position(parent_order_id, symbol, add_quantity, add_price,
                    reason="", asset_type="crypto", trading_strategy="manual"):
    """Adds to an existing position with weighted-average entry price update.

    Paper trading: updates DB directly with new avg price and total quantity.
    Live trading: places market BUY, cancels existing OCO, updates DB, places new OCO.

    Returns dict with status, new_avg_price, new_total_quantity.
    """
    if _is_live_trading() and trading_strategy != 'auto':
        return _live_add_to_position(parent_order_id, symbol, add_quantity, add_price,
                                     reason, asset_type)

    # --- Paper trading path (atomic: single connection, single commit) ---
    # Apply simulated slippage to addition price (BUY direction)
    slippage_pct = app_config.get('settings', {}).get('simulated_slippage_pct', 0.001)
    slipped_add_price = add_price * (1 + slippage_pct)
    log.info(f"Paper add with {slippage_pct*100:.2f}% slippage: requested ${add_price:.4f} → filled ${slipped_add_price:.4f}")

    conn = None
    try:
        conn = get_db_connection()
        is_pg = isinstance(conn, psycopg2.extensions.connection)
        ph = '%s' if is_pg else '?'
        with _cursor(conn) as cursor:
            q = f'SELECT entry_price, quantity FROM trades WHERE order_id = {ph} AND status = {ph}'
            cursor.execute(q, (parent_order_id, 'OPEN'))
            row = cursor.fetchone()
            if not row:
                log.error(f"Cannot add to position — order {parent_order_id} not found or not OPEN")
                return {"status": "FAILED", "message": "Position not found or not open"}

            old_price = float(row[0])
            old_qty = float(row[1])

            new_total = old_qty + add_quantity
            new_avg = (old_price * old_qty + slipped_add_price * add_quantity) / new_total

            # Update trade position (inline, same cursor)
            q_update = f'UPDATE trades SET entry_price = {ph}, quantity = {ph} WHERE order_id = {ph}'
            cursor.execute(q_update, (new_avg, new_total, parent_order_id))

            # Record position addition (inline, same cursor)
            q_add = (f'INSERT INTO position_additions '
                     f'(parent_order_id, addition_price, addition_quantity, reason) '
                     f'VALUES ({ph}, {ph}, {ph}, {ph})')
            cursor.execute(q_add, (parent_order_id, slipped_add_price, add_quantity, reason))

        conn.commit()

        log.info(f"Paper position increase for {symbol}: +{add_quantity} at ${slipped_add_price:,.2f} "
                 f"→ avg ${new_avg:,.2f}, total {new_total}")
        return {
            "status": "FILLED",
            "order_id": parent_order_id,
            "symbol": symbol,
            "new_avg_price": round(new_avg, 8),
            "new_total_quantity": round(new_total, 8),
            "price": slipped_add_price,
        }

    except (sqlite3.Error, psycopg2.Error) as e:
        log.error(f"Database error in add_to_position: {e}", exc_info=True)
        if conn:
            conn.rollback()
        return {"status": "FAILED", "message": str(e)}
    finally:
        release_db_connection(conn)


def _live_add_to_position(parent_order_id, symbol, add_quantity, add_price,
                          reason="", asset_type="crypto"):
    """Live trading path for adding to a position (future implementation)."""
    from src.database import (save_position_addition, update_trade_position)

    api_symbol = symbol if symbol.endswith("USDT") else f"{symbol}USDT"
    client = _get_binance_client()
    if not client:
        return {"status": "FAILED", "message": "Binance client not available"}

    try:
        # Step 1: Place market BUY first (old OCO still protects existing position)
        sym_info = _get_symbol_info(api_symbol)
        adjusted_qty = _validate_order_quantity(sym_info, add_quantity, add_price)
        if adjusted_qty is None:
            return {"status": "FAILED", "message": f"Order quantity too small for {api_symbol}"}

        order = client.order_market_buy(symbol=api_symbol, quantity=adjusted_qty)
        fill_price = _extract_fill_price(order) or add_price
        fill_qty = float(order.get('executedQty', adjusted_qty))

        # Step 2: Cancel old OCO (after BUY succeeds)
        _cancel_open_oco_orders(api_symbol)

        # Get existing position to compute new avg
        conn = get_db_connection()
        is_pg = isinstance(conn, psycopg2.extensions.connection)
        try:
            with _cursor(conn) as cursor:
                q = ('SELECT entry_price, quantity FROM trades WHERE order_id = %s AND status = %s'
                     if is_pg else
                     'SELECT entry_price, quantity FROM trades WHERE order_id = ? AND status = ?')
                cursor.execute(q, (parent_order_id, 'OPEN'))
                row = cursor.fetchone()
        finally:
            release_db_connection(conn)

        if not row:
            log.error(f"Position {parent_order_id} not found after live buy")
            return {"status": "FAILED", "message": "Position not found"}

        old_price = float(row[0])
        old_qty = float(row[1])
        new_total = old_qty + fill_qty
        new_avg = (old_price * old_qty + fill_price * fill_qty) / new_total

        update_trade_position(parent_order_id, new_avg, new_total)
        save_position_addition(parent_order_id, fill_price, fill_qty, reason)

        # Step 3: Place new OCO via retry path (not bare _place_oco_bracket)
        oco_result = _place_oco_with_retry(api_symbol, new_avg, new_total)

        if not oco_result:
            # OCO failed — entire position is naked, emergency close all
            log.warning(f"New OCO failed after add-to-position for {symbol} — "
                        f"emergency closing entire position ({new_total})")
            emergency = _emergency_market_close(
                api_symbol, new_total,
                reason="OCO failed after add-to-position, closing entire position")
            if emergency:
                _close_live_trade(parent_order_id,
                                  emergency['fill_price'] or fill_price,
                                  0, emergency['fill_price'], emergency['fill_qty'],
                                  exit_reason="emergency_oco_failure")
                return {
                    "status": "EMERGENCY_CLOSED",
                    "order_id": parent_order_id,
                    "symbol": symbol,
                    "message": "OCO failed after position increase — emergency closed",
                }
            else:
                log.critical(f"NAKED POSITION after add-to-position: {symbol} qty={new_total}")

        log.info(f"Live position increase for {symbol}: +{fill_qty} at ${fill_price:,.2f} "
                 f"→ avg ${new_avg:,.2f}, total {new_total}")
        return {
            "status": "FILLED",
            "order_id": parent_order_id,
            "symbol": symbol,
            "new_avg_price": round(new_avg, 8),
            "new_total_quantity": round(new_total, 8),
            "price": fill_price,
        }

    except Exception as e:
        log.error(f"Live add_to_position failed: {e}", exc_info=True)
        return {"status": "FAILED", "message": str(e)}


# --- Position & Balance Queries ---

def get_open_positions(asset_type=None, trading_strategy=None):
    """Retrieves open trading positions from the database, optionally filtered by asset_type and trading_strategy."""
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

        ph = '%s' if is_postgres_conn else '?'
        query = f'SELECT * FROM trades WHERE status = {ph}'
        params = ["OPEN"]

        if asset_type:
            query += f' AND asset_type = {ph}'
            params.append(asset_type)
        if trading_strategy:
            query += f' AND trading_strategy = {ph}'
            params.append(trading_strategy)

        cursor.execute(query, tuple(params))
        positions = [dict(row) for row in cursor.fetchall()]

        log.info(f"Retrieved {len(positions)} open positions (asset_type={asset_type or 'all'}, strategy={trading_strategy or 'all'}).")
        return positions
    except (sqlite3.Error, psycopg2.Error) as e:
        log.error(f"Database error in get_open_positions: {e}", exc_info=True)
        return []
    finally:
        if cursor:
            cursor.close()
        release_db_connection(conn)


def get_account_balance(asset_type=None, trading_strategy=None):
    """
    Returns account balance — paper-based calculation or real Binance balance.
    """
    if _is_live_trading() and trading_strategy != 'auto':
        return _get_live_balance()
    return _get_paper_balance(asset_type=asset_type, trading_strategy=trading_strategy)


def _get_paper_balance(asset_type=None, trading_strategy=None):
    """Calculates the current paper trading balance based on initial capital and closed trade PnL."""
    if trading_strategy == 'auto':
        auto_cfg = app_config.get('settings', {}).get('auto_trading', {})
        initial_capital = Decimal(str(auto_cfg.get('paper_trading_initial_capital', 10000.0)))
    elif asset_type == 'stock':
        stock_cfg = app_config.get('settings', {}).get('stock_trading', {})
        initial_capital = Decimal(str(stock_cfg.get('paper_trading_initial_capital',
                                  app_config.get('settings', {}).get('paper_trading_initial_capital', 10000.0))))
    else:
        initial_capital = Decimal(str(app_config.get('settings', {}).get('paper_trading_initial_capital', 10000.0)))
    conn = None
    try:
        conn = get_db_connection()
        is_postgres_conn = isinstance(conn, psycopg2.extensions.connection)
        ph = '%s' if is_postgres_conn else '?'
        with _cursor(conn) as cursor:
            # Build PnL query with optional filters
            query_pnl = f'SELECT COALESCE(SUM(pnl), 0) FROM trades WHERE status = {ph}'
            params_pnl = ["CLOSED"]
            if asset_type:
                query_pnl += f' AND asset_type = {ph}'
                params_pnl.append(asset_type)
            if trading_strategy:
                query_pnl += f' AND trading_strategy = {ph}'
                params_pnl.append(trading_strategy)
            cursor.execute(query_pnl, tuple(params_pnl))
            total_pnl = Decimal(str(cursor.fetchone()[0]))

            # Build locked capital query with optional filters
            query_open = f'SELECT COALESCE(SUM(entry_price * quantity), 0) FROM trades WHERE status = {ph}'
            params_open = ["OPEN"]
            if asset_type:
                query_open += f' AND asset_type = {ph}'
                params_open.append(asset_type)
            if trading_strategy:
                query_open += f' AND trading_strategy = {ph}'
                params_open.append(trading_strategy)
            cursor.execute(query_open, tuple(params_open))
            locked_capital = Decimal(str(cursor.fetchone()[0]))

        available = initial_capital + total_pnl - locked_capital
        total = initial_capital + total_pnl
        available_f = float(available.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))
        total_f = float(total.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))
        log.info(f"Paper trading balance (strategy={trading_strategy or 'manual'}): available=${available_f:.2f}, total=${total_f:.2f}")
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
def reconcile_crypto_positions():
    """Reconcile DB positions against Binance exchange state at startup.

    - Paper mode → skip (DB is source of truth).
    - Live/testnet → fetch exchange positions, compare with DB.
    - DB position not on exchange → mark CLOSED with exit_reason='reconciled_stale'.
    - Exchange position not in DB → log WARNING only (don't touch exchange).
    - Any error → log and continue (never crash startup).
    """
    paper_trading = app_config.get('settings', {}).get('paper_trading', True)
    if paper_trading:
        log.info("[Reconcile] Paper mode — skipping crypto position reconciliation.")
        return 0

    if not _is_live_trading():
        log.info("[Reconcile] Live trading not enabled — skipping crypto reconciliation.")
        return 0

    try:
        client = _get_binance_client()
        if not client:
            log.warning("[Reconcile] Binance client unavailable — skipping.")
            return 0

        # Fetch exchange balances (non-zero free+locked)
        account_info = client.get_account()
        exchange_symbols = set()
        for bal in account_info.get('balances', []):
            free = float(bal.get('free', 0))
            locked = float(bal.get('locked', 0))
            if free + locked > 0 and bal['asset'] not in ('USDT', 'USD', 'BUSD'):
                exchange_symbols.add(bal['asset'])

        # Fetch DB open crypto positions
        db_positions = get_open_positions(asset_type='crypto')
        stale_count = 0

        for pos in db_positions:
            symbol = pos['symbol'].replace('USDT', '')
            if symbol not in exchange_symbols:
                order_id = pos.get('order_id')
                log.warning(f"[Reconcile] DB position {order_id} ({pos['symbol']}) "
                            f"not found on exchange — marking CLOSED.")
                conn = None
                try:
                    conn = get_db_connection()
                    with _cursor(conn) as cur:
                        is_pg = isinstance(conn, psycopg2.extensions.connection)
                        ph = '%s' if is_pg else '?'
                        cur.execute(
                            f"UPDATE trades SET status = {ph}, "
                            f"exit_reason = {ph}, "
                            f"exit_timestamp = CURRENT_TIMESTAMP "
                            f"WHERE order_id = {ph} AND status = {ph}",
                            ('CLOSED', 'reconciled_stale', order_id, 'OPEN'))
                    conn.commit()
                    stale_count += 1
                except Exception as e:
                    log.error(f"[Reconcile] Failed to close stale position {order_id}: {e}")
                    if conn:
                        conn.rollback()
                finally:
                    release_db_connection(conn)

        # Warn about exchange positions not in DB
        db_symbols = {p['symbol'].replace('USDT', '') for p in db_positions}
        for ex_sym in exchange_symbols:
            if ex_sym not in db_symbols:
                log.debug(f"[Reconcile] Exchange position {ex_sym} not tracked in DB — "
                          f"manual review recommended.")

        log.info(f"[Reconcile] Crypto reconciliation complete: "
                 f"{stale_count} stale positions closed.")
        return stale_count

    except Exception as e:
        log.error(f"[Reconcile] Crypto reconciliation failed: {e}", exc_info=True)
        return 0


    mode = _get_trading_mode()
    log.info(f"Current trading mode: {mode}")
    balance = get_account_balance()
    log.info(f"Balance: {balance}")

    buy_order = place_order("BTC", "BUY", 0.001, 30000.0)
    log.info(f"Buy Order: {buy_order}")

    open_positions = get_open_positions()
    log.info(f"Open Positions: {open_positions}")
    log.info("--- Test Complete ---")
