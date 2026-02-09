"""
Backfill historical price data from Binance public API.

Fetches 90 days of hourly candlestick (kline) data for each symbol in the
watch list and inserts it into the market_prices table. Uses close prices.

Usage:
    python scripts/backfill_historical_data.py
    docker exec crypto-bot python3 scripts/backfill_historical_data.py
"""

import sys
import os
import time
import sqlite3
from datetime import datetime, timedelta, timezone

import requests
import psycopg2

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.config import app_config
from src.database import get_db_connection, release_db_connection, _cursor, initialize_database
from src.logger import log

BINANCE_API_URL = "https://api.binance.us/api/v3/klines"
INTERVAL = "1h"
LIMIT = 1000  # max per request
DAYS_TO_FETCH = 90
SLEEP_BETWEEN_REQUESTS = 0.5


def fetch_klines(symbol_pair: str, start_ms: int, end_ms: int) -> list:
    """Fetch klines from Binance for a given symbol pair and time range."""
    params = {
        "symbol": symbol_pair,
        "interval": INTERVAL,
        "startTime": start_ms,
        "endTime": end_ms,
        "limit": LIMIT,
    }
    resp = requests.get(BINANCE_API_URL, params=params, timeout=30)
    resp.raise_for_status()
    return resp.json()


def backfill_symbol(conn, symbol: str, is_pg: bool):
    """Fetch and insert all historical data for one symbol."""
    symbol_pair = f"{symbol}USDT"
    now = datetime.now(timezone.utc)
    start_dt = now - timedelta(days=DAYS_TO_FETCH)
    end_ms = int(now.timestamp() * 1000)
    cursor_ms = int(start_dt.timestamp() * 1000)

    all_rows = []

    while cursor_ms < end_ms:
        log.info(f"  Fetching {symbol_pair} from {datetime.fromtimestamp(cursor_ms / 1000, tz=timezone.utc).isoformat()}")
        try:
            klines = fetch_klines(symbol_pair, cursor_ms, end_ms)
        except requests.exceptions.HTTPError as e:
            log.error(f"  HTTP error fetching {symbol_pair}: {e}")
            break
        except requests.exceptions.RequestException as e:
            log.error(f"  Request error fetching {symbol_pair}: {e}")
            break

        if not klines:
            break

        for k in klines:
            # k[0] = open time (ms), k[4] = close price
            ts = datetime.fromtimestamp(k[0] / 1000, tz=timezone.utc)
            close_price = float(k[4])
            all_rows.append((symbol, close_price, ts.strftime("%Y-%m-%d %H:%M:%S")))

        # Move cursor past the last returned candle
        cursor_ms = klines[-1][0] + 1
        time.sleep(SLEEP_BETWEEN_REQUESTS)

    if not all_rows:
        log.warning(f"  No data fetched for {symbol_pair}")
        return 0

    # Clear existing data for this symbol, then batch insert
    with _cursor(conn) as cur:
        if is_pg:
            cur.execute("DELETE FROM market_prices WHERE symbol = %s", (symbol,))
        else:
            cur.execute("DELETE FROM market_prices WHERE symbol = ?", (symbol,))

        if is_pg:
            query = "INSERT INTO market_prices (symbol, price, timestamp) VALUES (%s, %s, %s)"
        else:
            query = "INSERT INTO market_prices (symbol, price, timestamp) VALUES (?, ?, ?)"

        cur.executemany(query, all_rows)

    conn.commit()
    log.info(f"  Inserted {len(all_rows)} rows for {symbol}")
    return len(all_rows)


def main():
    initialize_database()

    watch_list = app_config.get("settings", {}).get("watch_list", [])
    if not watch_list:
        log.error("No symbols in watch_list config. Nothing to backfill.")
        return

    log.info(f"Starting backfill for {len(watch_list)} symbols: {watch_list}")
    log.info(f"Fetching {DAYS_TO_FETCH} days of hourly data per symbol")

    conn = None
    total_rows = 0
    try:
        conn = get_db_connection()
        is_pg = isinstance(conn, psycopg2.extensions.connection)

        for symbol in watch_list:
            log.info(f"Backfilling {symbol}...")
            rows = backfill_symbol(conn, symbol, is_pg)
            total_rows += rows

        log.info(f"Backfill complete. Total rows inserted: {total_rows}")
    except Exception as e:
        log.error(f"Backfill failed: {e}", exc_info=True)
        if conn:
            conn.rollback()
    finally:
        release_db_connection(conn)


if __name__ == "__main__":
    main()
