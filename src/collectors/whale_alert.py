import requests
import yaml
import os
import time

# Whale Alert API base URL
WHALE_ALERT_API_URL = "https://api.whale-alert.io/v1"

# --- Configuration Loading ---

def load_config():
    """Loads the configuration from the settings.yaml file."""
    script_dir = os.path.dirname(os.path.abspath(__file__))
    config_path = os.path.join(script_dir, '..', '..', 'config', 'settings.yaml')
    
    try:
        with open(config_path, 'r') as f:
            return yaml.safe_load(f)
    except FileNotFoundError:
        print(f"Error: Configuration file not found at {config_path}")
        return None
    except yaml.YAMLError as e:
        print(f"Error parsing YAML file: {e}")
        return None

# --- Whale Alert API Logic ---

def get_whale_transactions(min_value_usd: int = 1000000):
    """
    Fetches the latest transactions from the Whale Alert API.

    Args:
        min_value_usd (int): The minimum value in USD to filter transactions.
                             Defaults to 1,000,000.

    Returns:
        dict: A dictionary containing the latest transactions if the request is successful,
              otherwise None.
    """
    config = load_config()
    if not config:
        return None

    api_key = config.get('api_keys', {}).get('whale_alert')
    if not api_key or api_key == "YOUR_WHALE_ALERT_API_KEY":
        print("Error: Whale Alert API key is not configured in config/settings.yaml")
        return None

    # The API requires the 'start' parameter, which is a Unix timestamp.
    # We'll fetch transactions from the last 60 minutes.
    start_timestamp = int(time.time()) - 3600

    endpoint = f"{WHALE_ALERT_API_URL}/transactions"
    headers = {'X-WA-API-KEY': api_key}
    params = {
        'start': start_timestamp,
        'min_value': min_value_usd
    }

    try:
        response = requests.get(endpoint, headers=headers, params=params)
        response.raise_for_status()

        data = response.json()
        if data.get('result') == 'success':
            print(f"Successfully fetched {data.get('count', 0)} whale transactions.")
            return data['transactions']
        else:
            print(f"Whale Alert API returned an error: {data.get('message', 'Unknown error')}")
            return None

    except requests.exceptions.HTTPError as http_err:
        if response.status_code == 401:
            print("Error: Unauthorized. Your Whale Alert API key is likely invalid.")
        else:
            print(f"HTTP error occurred: {http_err}")
        return None
    except requests.exceptions.RequestException as e:
        print(f"Error fetching data from Whale Alert API: {e}")
        return None

if __name__ == '__main__':
    print("--- Testing Whale Alert Data Collector ---")
    
    # Load min_value from config to use in the test
    app_config = load_config()
    min_transaction_value = 1000000 # Default
    if app_config and 'settings' in app_config and 'min_whale_transaction_usd' in app_config['settings']:
        min_transaction_value = app_config['settings']['min_whale_transaction_usd']
        print(f"Using minimum transaction value from config: ${min_transaction_value:,}")
    
    transactions = get_whale_transactions(min_value_usd=min_transaction_value)
    
    if transactions is not None:
        if transactions:
            print(f"\nFound {len(transactions)} transactions in the last hour with a value over ${min_transaction_value:,}:")
            # Print details of the first 5 transactions
            for tx in transactions[:5]:
                symbol = tx['symbol']
                amount_usd = tx['amount_usd']
                from_owner = tx['from']['owner']
                to_owner = tx['to']['owner']
                print(f"- {symbol}: ${amount_usd:,.2f} from {from_owner} to {to_owner}")
        else:
            print(f"\nNo transactions found in the last hour with a value over ${min_transaction_value:,}.")
            
    print("\n--- Test Complete ---")
