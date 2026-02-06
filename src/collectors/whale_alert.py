import requests
import os
import time
from src.database import get_db_connection, release_db_connection
from src.config import app_config
from src.logger import log
import psycopg2

MAX_RETRIES = 3
RETRY_BACKOFF_BASE = 2

# Whale Alert API base URL
WHALE_ALERT_API_URL = "https://api.whale-alert.io/v1"

def save_whale_transactions(transactions: list):
    """Saves whale transactions to the database."""
    if not transactions:
        return

    conn = None
    cursor = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()

        is_postgres_conn = isinstance(conn, psycopg2.extensions.connection)

        query = '''
            INSERT INTO whale_transactions (id, symbol, timestamp, amount_usd, from_owner, from_owner_type, to_owner, to_owner_type)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (id) DO NOTHING
        ''' if is_postgres_conn else '''
            INSERT OR IGNORE INTO whale_transactions (id, symbol, timestamp, amount_usd, from_owner, from_owner_type, to_owner, to_owner_type)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        '''

        for tx in transactions:
            params = (
                tx['id'], tx['symbol'], tx['timestamp'], tx['amount_usd'],
                tx['from'].get('owner'), tx['from'].get('owner_type'),
                tx['to'].get('owner'), tx['to'].get('owner_type')
            )
            cursor.execute(query, params)

        conn.commit()
        log.info(f"Processed {len(transactions)} whale transactions for the database.")
    except Exception as e:
        log.error(f"Error saving whale transactions: {e}", exc_info=True)
    finally:
        if cursor:
            cursor.close()
        release_db_connection(conn)

def get_whale_transactions(min_value_usd: int = 500000, symbols: list = None):
    """
    Fetches the latest transactions from the Whale Alert API and saves them.
    """
    api_key = app_config.get('api_keys', {}).get('whale_alert')
    if not api_key or api_key == "YOUR_WHALE_ALERT_API_KEY":
        log.error("Whale Alert API key is not configured.")
        return None

    start_timestamp = int(time.time()) - 3600
    log.info(f"Fetching whale transactions from Whale Alert API. Start Timestamp: {start_timestamp}, Min Value USD: {min_value_usd}")
    log.debug(f"Whale Alert API Request URL: {WHALE_ALERT_API_URL}/transactions?start={start_timestamp}&min_value={min_value_usd}")
    headers = {'X-WA-API-KEY': api_key}
    params = {'start': start_timestamp, 'min_value': min_value_usd}

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            response = requests.get(f"{WHALE_ALERT_API_URL}/transactions", headers=headers, params=params, timeout=30)
            response.raise_for_status()
            data = response.json()

            log.debug(f"Whale Alert API Raw Response: {data}")

            if data.get('result') == 'success':
                transactions = data.get('transactions', [])
                log.info(f"Successfully fetched {len(transactions)} whale transactions.")
                save_whale_transactions(transactions)
                log.info(f"Saved {len(transactions)} whale transactions to the database.")
                return transactions
            else:
                log.warning(f"Whale Alert API error: {data.get('message')}")
                return None  # API returned an error response, don't retry
        except requests.exceptions.RequestException as e:
            log.error(f"Error fetching from Whale Alert API (attempt {attempt}/{MAX_RETRIES}): {e}")
            if attempt < MAX_RETRIES:
                backoff = RETRY_BACKOFF_BASE ** attempt
                log.info(f"Retrying in {backoff}s...")
                time.sleep(backoff)

    log.error(f"Failed to fetch whale transactions after {MAX_RETRIES} attempts.")
    return None

def get_stablecoin_flows(transactions: list, stablecoins: list):
    """
    Analyzes a list of transactions to calculate the net inflow of stablecoins to exchanges.
    
    Args:
        transactions (list): A list of whale transaction dictionaries.
        stablecoins (list): A list of stablecoin symbols to monitor (e.g., ['usdt', 'usdc']).
        
    Returns:
        dict: A dictionary containing the total USD value of stablecoins moved to exchanges.
    """
    inflow_usd = 0
    if not transactions or not stablecoins:
        return {'stablecoin_inflow_usd': inflow_usd}

    for tx in transactions:
        is_stablecoin = tx.get('symbol', '').lower() in stablecoins
        is_to_exchange = tx.get('to', {}).get('owner_type', '') == 'exchange'
        
        if is_stablecoin and is_to_exchange:
            inflow_usd += tx.get('amount_usd', 0)
            
    log.info(f"Calculated total stablecoin inflow to exchanges: ${inflow_usd:,.2f}")
    return {'stablecoin_inflow_usd': inflow_usd}


if __name__ == '__main__':
    log.info("--- Testing Whale Alert Data Collector (with DB saving) ---")
    all_transactions = get_whale_transactions()
    if all_transactions:
        # Example of how to use the new function
        get_stablecoin_flows(all_transactions, stablecoins=['usdt', 'usdc'])
    log.info("--- Test Complete ---")
