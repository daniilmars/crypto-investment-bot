import yaml
import os
from src.logger import log

# --- Centralized Configuration Management ---

def load_config():
    """
    Loads configuration from settings.yaml and overrides with environment variables if they exist.
    This provides a flexible configuration system for both local development and production.
    """
    # Base path for the config file
    script_dir = os.path.dirname(os.path.abspath(__file__))
    config_path = os.path.join(script_dir, '..', 'config', 'settings.yaml')
    
    config = {}
    
    # 1. Load base configuration from YAML file
    try:
        with open(config_path, 'r') as f:
            config = yaml.safe_load(f)
        log.info("Loaded base configuration from settings.yaml.")
    except FileNotFoundError:
        log.warning("settings.yaml not found. Relying solely on environment variables.")
        # Initialize the config structure to prevent KeyErrors when overriding with env vars
        config = {
            'api_keys': {},
            'notification_services': {'telegram': {}},
            'settings': {}
        }
    except yaml.YAMLError as e:
        log.error(f"Error parsing settings.yaml: {e}")
        # Return a default structure to avoid crashes downstream
        return {
            'api_keys': {},
            'notification_services': {'telegram': {}},
            'settings': {}
        }

    # 2. Override with Environment Variables
    # This is more secure for production environments like Docker.
    
    # --- DIAGNOSTIC LOG ---
    gemini_key_env = os.getenv('GEMINI_API_KEY')
    if gemini_key_env:
        log.info(f"Found GEMINI_API_KEY environment variable. [Key: {gemini_key_env[:5]}...{gemini_key_env[-5:]}]")
    else:
        log.warning("GEMINI_API_KEY environment variable not found.")
    # --- END DIAGNOSTIC LOG ---

    # API Keys
    config['api_keys']['whale_alert'] = os.getenv('WHALE_ALERT_API_KEY', config.get('api_keys', {}).get('whale_alert'))
    config['api_keys']['gemini'] = os.getenv('GEMINI_API_KEY', config.get('api_keys', {}).get('gemini'))
    
    # Notification Services (Telegram)
    if 'telegram' not in config['notification_services']:
        config['notification_services']['telegram'] = {}
    config['notification_services']['telegram']['token'] = os.getenv('TELEGRAM_BOT_TOKEN', config.get('notification_services', {}).get('telegram', {}).get('token'))
    config['notification_services']['telegram']['chat_id'] = os.getenv('TELEGRAM_CHAT_ID', config.get('notification_services', {}).get('telegram', {}).get('chat_id'))

    # If the token and chat_id are provided via environment variables, assume the service should be enabled.
    if config['notification_services']['telegram']['token'] and config['notification_services']['telegram']['chat_id']:
        config['notification_services']['telegram']['enabled'] = True

    # General Settings (example, can be expanded)
    # For settings like watch_list, it's often better to manage them in the yaml,
    # but this shows how it could be done for simple values.
    min_usd_str = os.getenv('MIN_WHALE_TRANSACTION_USD')
    if min_usd_str and min_usd_str.isdigit():
        config['settings']['min_whale_transaction_usd'] = int(min_usd_str)

    # Load watch_list from environment variable, overriding YAML if present
    watch_list_env = os.getenv('WATCH_LIST')
    if watch_list_env:
        config['settings']['watch_list'] = [symbol.strip() for symbol in watch_list_env.split(';')]
    elif 'watch_list' not in config['settings']:
        config['settings']['watch_list'] = ['BTC'] # Default if neither YAML nor env var provides it

    # Load stablecoins_to_monitor from environment variable, overriding YAML if present
    stablecoins_env = os.getenv('STABLECOINS_TO_MONITOR')
    if stablecoins_env:
        config['settings']['stablecoins_to_monitor'] = [symbol.strip() for symbol in stablecoins_env.split(';')]
    elif 'stablecoins_to_monitor' not in config['settings']:
        config['settings']['stablecoins_to_monitor'] = ['usdt', 'usdc'] # Default if neither YAML nor env var provides it

    # Database Configuration
    db_url = os.getenv('DATABASE_URL')
    config['DATABASE_URL'] = db_url # Keep the raw URL for fallback
    config['DB_INSTANCE_CONNECTION_NAME'] = os.getenv('DB_INSTANCE_CONNECTION_NAME')

    if db_url and db_url.startswith('postgresql://'):
        try:
            # Parse the DATABASE_URL to extract components
            from urllib.parse import urlparse
            result = urlparse(db_url)
            config['db'] = {
                'user': result.username,
                'password': result.password,
                'host': result.hostname,
                'port': result.port,
                'name': result.path[1:] # Remove the leading '/'
            }
            log.info("Successfully parsed DATABASE_URL for PostgreSQL credentials.")
        except Exception as e:
            log.error(f"Could not parse DATABASE_URL: {e}")
            config['db'] = {}
    else:
        config['db'] = {}

    return config

# Create a single, memoized config instance to be used across the application
# This avoids reloading the file and re-checking env vars every time.
app_config = load_config()
