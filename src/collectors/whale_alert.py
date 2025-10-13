import requests
import yaml
import os
import time
from src.database import get_db_connection
from src.logger import log

# Whale Alert API base URL
WHALE_ALERT_API_URL = "https://api.whale-alert.io/v1"

# --- Configuration Loading ---
def load_config():
    """Loads the configuration from the settings.yaml file."""
    script_dir = os.path.dirname(os.path.abspath(__file__))
    config_path = os.path.join(script_dir, '..', '..', 'config', 'settings.yaml')
    try:
        with open(config_path, 'r') as f: return yaml.safe_load(f)
    except (FileNotFoundError, yaml.YAMLError) as e:
        log.error(f"Error loading config: {e}")
        return None

def save_whale_transactions(transactions: list):
    """Saves whale transactions to the database."""
    if not transactions:
        return

    conn = get_db_connection()
    cursor = conn.cursor()
    
    for tx in transactions:
        cursor.execute('''
            INSERT OR IGNORE INTO whale_transactions (id, symbol, timestamp, amount_usd, from_owner, from_owner_type, to_owner, to_owner_type)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ''', (
            tx['id'], tx['symbol'], tx['timestamp'], tx['amount_usd'],
            tx['from'].get('owner'), tx['from'].get('owner_type'),
            tx['to'].get('owner'), tx['to'].get('owner_type')
        ))
    
    conn.commit()
    conn.close()
    log.info(f"Processed {len(transactions)} whale transactions for the database.")

def get_whale_transactions(min_value_usd: int = 1000000):
    """
    Fetches the latest transactions from the Whale Alert API and saves them.
    """
    config = load_config()
    if not config: return None

    api_key = config.get('api_keys', {}).get('whale_alert')
    if not api_key or api_key == "YOUR_WHALE_ALERT_API_KEY":
        log.error("Whale Alert API key is not configured.")
        return None

    start_timestamp = int(time.time()) - 3600
    headers = {'X-WA-API-KEY': api_key}
    params = {'start': start_timestamp, 'min_value': min_value_usd}

    try:
        response = requests.get(f"{WHALE_ALERT_API_URL}/transactions", headers=headers, params=params)
        response.raise_for_status()
        data = response.json()
        
        if data.get('result') == 'success':
            transactions = data.get('transactions', [])
            log.info(f"Successfully fetched {len(transactions)} whale transactions.")
            save_whale_transactions(transactions)
            return transactions
        else:
            log.warning(f"Whale Alert API error: {data.get('message')}")
            return None
    except requests.exceptions.RequestException as e:
        log.error(f"Error fetching from Whale Alert API: {e}")
        return None

if __name__ == '__main__':
    log.info("--- Testing Whale Alert Data Collector (with DB saving) ---")
    get_whale_transactions()
    log.info("--- Test Complete ---")
