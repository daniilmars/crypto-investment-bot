import os
import time
import sqlite3
from collections import deque
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP

import psycopg2
from src.config import app_config
from src.database import get_db_connection, release_db_connection, _cursor
from src.logger import log

# --- Alpaca Client (lazy-initialized) ---
_trading_client = None

# PDT tracking: rolling window of day-trade timestamps
_day_trades = deque()


def _get_alpaca_client():
    """Returns an authenticated Alpaca TradingClient. Lazy-initialized."""
    global _trading_client
    if _trading_client is not None:
        return _trading_client

    try:
        from alpaca.trading.client import TradingClient
    except ImportError:
        log.error("alpaca-py is not installed. Run: pip install alpaca-py")
        return None

    api_key = app_config.get('api_keys', {}).get('alpaca', {}).get('api_key')
    api_secret = app_config.get('api_keys', {}).get('alpaca', {}).get('api_secret')

    if not api_key or not api_secret:
        log.error("ALPACA_API_KEY and ALPACA_API_SECRET must be set for stock trading.")
        return None

    stock_settings = app_config.get('settings', {}).get('stock_trading', {})
    paper = stock_settings.get('alpaca', {}).get('paper', True)

    _trading_client = TradingClient(api_key, api_secret, paper=paper)
    log.info(f"Initialized Alpaca TradingClient (paper={paper})")
    return _trading_client


def _is_market_open():
    """Checks if NYSE is currently open (9:30-16:00 ET, weekdays)."""
    try:
        client = _get_alpaca_client()
        if client:
            clock = client.get_clock()
            return clock.is_open
    except Exception as e:
        log.warning(f"Could not check market hours via Alpaca: {e}")

    # Fallback: manual check
    try:
        from zoneinfo import ZoneInfo
    except ImportError:
        from backports.zoneinfo import ZoneInfo

    now_et = datetime.now(ZoneInfo("America/New_York"))
    # Weekday check (Mon=0, Fri=4)
    if now_et.weekday() > 4:
        return False
    market_open = now_et.replace(hour=9, minute=30, second=0, microsecond=0)
    market_close = now_et.replace(hour=16, minute=0, second=0, microsecond=0)
    return market_open <= now_et <= market_close


def get_market_hours():
    """Returns market status and next open/close times."""
    try:
        client = _get_alpaca_client()
        if client:
            clock = client.get_clock()
            return {
                'is_open': clock.is_open,
                'next_open': str(clock.next_open),
                'next_close': str(clock.next_close),
            }
    except Exception as e:
        log.warning(f"Could not fetch market hours: {e}")

    return {
        'is_open': _is_market_open(),
        'next_open': 'unknown',
        'next_close': 'unknown',
    }


def _check_pdt_rule():
    """
    Checks Pattern Day Trader rule compliance.
    Accounts under $25K are limited to 3 day trades in 5 business days.

    Returns:
        dict with 'day_trades_used', 'day_trades_remaining', 'is_restricted'
    """
    # Clean old entries (older than 5 business days ≈ 7 calendar days)
    cutoff = datetime.now(timezone.utc) - timedelta(days=7)
    while _day_trades and _day_trades[0] < cutoff:
        _day_trades.popleft()

    used = len(_day_trades)
    remaining = max(0, 3 - used)
    return {
        'day_trades_used': used,
        'day_trades_remaining': remaining,
        'is_restricted': remaining == 0,
    }


def _record_day_trade():
    """Records a day trade for PDT tracking."""
    _day_trades.append(datetime.now(timezone.utc))


def place_stock_order(symbol, side, quantity, price):
    """
    Places a stock order via Alpaca.

    Args:
        symbol: Stock ticker (e.g., 'AAPL')
        side: 'BUY' or 'SELL'
        quantity: Number of shares (can be fractional)
        price: Current market price (used for record-keeping)

    Returns:
        dict with order details or error status
    """
    client = _get_alpaca_client()
    if not client:
        return {"status": "FAILED", "message": "Alpaca client not available"}

    if not _is_market_open():
        return {"status": "FAILED", "message": "Market is closed"}

    try:
        from alpaca.trading.requests import MarketOrderRequest
        from alpaca.trading.enums import OrderSide, TimeInForce

        alpaca_side = OrderSide.BUY if side == "BUY" else OrderSide.SELL
        order_request = MarketOrderRequest(
            symbol=symbol,
            qty=quantity,
            side=alpaca_side,
            time_in_force=TimeInForce.DAY
        )

        log.info(f"[ALPACA] Placing {side} order: {quantity} {symbol} at ~${price}")
        order = client.submit_order(order_request)

        order_id = f"ALPACA_{symbol}_{side}_{int(time.time() * 1000)}"
        exchange_order_id = str(order.id)

        fill_price = float(order.filled_avg_price) if order.filled_avg_price else price
        fill_qty = float(order.filled_qty) if order.filled_qty else quantity

        # Record in DB
        _record_stock_trade(symbol, order_id, side, fill_price, fill_qty,
                            exchange_order_id)

        if side == "BUY":
            # Attempt bracket order for SL/TP
            bracket = _place_bracket_order(symbol, quantity, fill_price)
            result = {
                "order_id": order_id, "exchange_order_id": exchange_order_id,
                "symbol": symbol, "side": side, "quantity": fill_qty,
                "price": fill_price, "status": "FILLED", "trading_mode": "alpaca",
            }
            if bracket:
                result["bracket"] = bracket
            return result

        return {
            "order_id": order_id, "exchange_order_id": exchange_order_id,
            "symbol": symbol, "side": side, "quantity": fill_qty,
            "price": fill_price, "status": "FILLED", "trading_mode": "alpaca",
        }

    except Exception as e:
        log.error(f"Alpaca order error: {e}", exc_info=True)
        return {"status": "FAILED", "message": str(e)}


def _place_bracket_order(symbol, quantity, entry_price):
    """
    Places a bracket order (stop-loss + take-profit) on Alpaca.
    """
    client = _get_alpaca_client()
    if not client:
        return None

    stock_settings = app_config.get('settings', {}).get('stock_trading', {})
    sl_pct = stock_settings.get('stop_loss_percentage',
                                app_config.get('settings', {}).get('stop_loss_percentage', 0.07))
    tp_pct = stock_settings.get('take_profit_percentage',
                                app_config.get('settings', {}).get('take_profit_percentage', 0.10))

    stop_price = round(entry_price * (1 - sl_pct), 2)
    take_profit_price = round(entry_price * (1 + tp_pct), 2)

    try:
        from alpaca.trading.requests import MarketOrderRequest, TakeProfitRequest, StopLossRequest
        from alpaca.trading.enums import OrderSide, TimeInForce, OrderClass

        bracket_request = MarketOrderRequest(
            symbol=symbol,
            qty=quantity,
            side=OrderSide.BUY,
            time_in_force=TimeInForce.GTC,
            order_class=OrderClass.BRACKET,
            take_profit=TakeProfitRequest(limit_price=take_profit_price),
            stop_loss=StopLossRequest(stop_price=stop_price),
        )

        log.info(f"Placing Alpaca bracket: {symbol} TP=${take_profit_price}, SL=${stop_price}")
        order = client.submit_order(bracket_request)
        return {
            "take_profit": take_profit_price,
            "stop_loss": stop_price,
            "order_id": str(order.id),
        }
    except Exception as e:
        log.error(f"Failed to place Alpaca bracket order: {e}")
        return None


def _record_stock_trade(symbol, order_id, side, price, quantity, exchange_order_id):
    """Records a stock trade in the database with asset_type='stock'."""
    conn = None
    try:
        conn = get_db_connection()
        is_pg = isinstance(conn, psycopg2.extensions.connection)
        with _cursor(conn) as cursor:
            query = (
                'INSERT INTO trades (symbol, order_id, side, entry_price, quantity, status, '
                'trading_mode, exchange_order_id, asset_type) '
                'VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)'
            ) if is_pg else (
                'INSERT INTO trades (symbol, order_id, side, entry_price, quantity, status, '
                'trading_mode, exchange_order_id, asset_type) '
                'VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)'
            )
            cursor.execute(query, (
                symbol, order_id, side, price, quantity, "OPEN",
                "alpaca", exchange_order_id, "stock"
            ))
        conn.commit()
        log.info(f"Recorded Alpaca stock trade: {order_id}")
    except (sqlite3.Error, psycopg2.Error) as e:
        log.error(f"DB error recording stock trade: {e}", exc_info=True)
        if conn:
            conn.rollback()
    finally:
        release_db_connection(conn)


def get_stock_positions():
    """Returns real stock positions from Alpaca."""
    client = _get_alpaca_client()
    if not client:
        return []
    try:
        positions = client.get_all_positions()
        result = []
        for pos in positions:
            result.append({
                'symbol': pos.symbol,
                'quantity': float(pos.qty),
                'entry_price': float(pos.avg_entry_price),
                'current_price': float(pos.current_price),
                'market_value': float(pos.market_value),
                'unrealized_pl': float(pos.unrealized_pl),
                'unrealized_plpc': float(pos.unrealized_plpc),
            })
        return result
    except Exception as e:
        log.error(f"Failed to fetch Alpaca positions: {e}")
        return []


def get_stock_balance():
    """Returns Alpaca account balance."""
    client = _get_alpaca_client()
    if not client:
        return {"cash": 0.0, "portfolio_value": 0.0, "buying_power": 0.0}
    try:
        account = client.get_account()
        return {
            "cash": float(account.cash),
            "portfolio_value": float(account.portfolio_value),
            "buying_power": float(account.buying_power),
            "equity": float(account.equity),
        }
    except Exception as e:
        log.error(f"Failed to fetch Alpaca balance: {e}")
        return {"cash": 0.0, "portfolio_value": 0.0, "buying_power": 0.0}
