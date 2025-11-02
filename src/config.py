import os
import yaml
from urllib.parse import urlparse
from src.logger import log

# --- Centralized Configuration Management ---

def _load_base_config(path):
    """Loads the base configuration from a YAML file."""
    try:
        with open(path, 'r', encoding='utf-8') as f:
            return yaml.safe_load(f) or {}
    except FileNotFoundError:
        log.warning("settings.yaml not found. Relying solely on environment variables.")
    except yaml.YAMLError as e:
        log.error(f"Error parsing settings.yaml: {e}")
    return {}

def _get_env(key, default=None):
    """Gets an environment variable."""
    return os.getenv(key, default)

def _load_api_keys(base_config):
    """Loads API keys from config and environment variables."""
    keys = base_config.get('api_keys', {})
    keys['whale_alert'] = _get_env('WHALE_ALERT_API_KEY', keys.get('whale_alert'))
    keys['gemini'] = _get_env('GEMINI_API_KEY', keys.get('gemini'))
    binance = keys.get('binance', {})
    binance['api_key'] = _get_env('BINANCE_API_KEY', binance.get('api_key'))
    binance['api_secret'] = _get_env('BINANCE_API_SECRET', binance.get('api_secret'))
    keys['binance'] = binance
    return keys

def _load_notifications(base_config):
    """Loads notification settings from config and environment variables."""
    notifications = base_config.get('notification_services', {})
    telegram = notifications.get('telegram', {})
    telegram['token'] = _get_env('TELEGRAM_BOT_TOKEN', telegram.get('token'))
    telegram['chat_id'] = _get_env('TELEGRAM_CHAT_ID', telegram.get('chat_id'))
    
    auth_users_env = _get_env('TELEGRAM_AUTHORIZED_USER_IDS')
    if auth_users_env:
        try:
            telegram['authorized_user_ids'] = [int(uid.strip()) for uid in auth_users_env.split(',')]
        except ValueError:
            log.error("TELEGRAM_AUTHORIZED_USER_IDS contains non-integer values.")
            telegram['authorized_user_ids'] = []
    
    if telegram.get('token') and telegram.get('chat_id'):
        telegram['enabled'] = True
        
    notifications['telegram'] = telegram
    return notifications

def _load_gcp_billing(base_config):
    """Loads GCP billing settings from config and environment variables."""
    billing = base_config.get('gcp_billing', {})
    enabled_env = _get_env('GCP_BILLING_ENABLED')
    if enabled_env is not None:
        billing['enabled'] = enabled_env.lower() == 'true'
    billing['billing_account_id'] = _get_env('GCP_BILLING_ACCOUNT_ID', billing.get('billing_account_id'))
    return billing

def _load_settings(base_config):
    """Loads general settings from config and environment variables."""
    settings = base_config.get('settings', {})
    min_usd_str = _get_env('MIN_WHALE_TRANSACTION_USD')
    if min_usd_str and min_usd_str.isdigit():
        settings['min_whale_transaction_usd'] = int(min_usd_str)
        
    watch_list_env = _get_env('WATCH_LIST')
    if watch_list_env:
        settings['watch_list'] = [symbol.strip() for symbol in watch_list_env.split(';')]
        
    stablecoins_env = _get_env('STABLECOINS_TO_MONITOR')
    if stablecoins_env:
        settings['stablecoins_to_monitor'] = [symbol.strip() for symbol in stablecoins_env.split(';')]
        
    return settings

def _load_database_config(config):
    """Loads database configuration from environment variables."""
    db_url = _get_env('DATABASE_URL')
    config['DATABASE_URL'] = db_url
    config['DB_INSTANCE_CONNECTION_NAME'] = _get_env('DB_INSTANCE_CONNECTION_NAME')
    
    if db_url and db_url.startswith('postgresql://'):
        try:
            result = urlparse(db_url)
            config['db'] = {
                'user': result.username,
                'password': result.password,
                'host': result.hostname,
                'port': result.port,
                'name': result.path[1:]
            }
            log.info("Successfully parsed DATABASE_URL for PostgreSQL credentials.")
        except Exception as e:
            log.error(f"Could not parse DATABASE_URL: {e}")
            config['db'] = {}
    else:
        config['db'] = {}
    return config

def load_config():
    """
    Loads configuration from settings.yaml and overrides with environment variables.
    """
    script_dir = os.path.dirname(os.path.abspath(__file__))
    config_path = os.path.join(script_dir, '..', 'config', 'settings.yaml')
    
    base_config = _load_base_config(config_path)
    
    config = {
        'api_keys': _load_api_keys(base_config),
        'notification_services': _load_notifications(base_config),
        'gcp_billing': _load_gcp_billing(base_config),
        'settings': _load_settings(base_config)
    }
    
    config = _load_database_config(config)
    
    return config

# Create a single, memoized config instance.
app_config = load_config()
