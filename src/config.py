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
    
    # API Keys
    config['api_keys']['whale_alert'] = os.getenv('WHALE_ALERT_API_KEY', config.get('api_keys', {}).get('whale_alert'))
    config['api_keys']['gemini'] = os.getenv('GEMINI_API_KEY', config.get('api_keys', {}).get('gemini'))
    
    # Notification Services (Telegram)
    if 'telegram' not in config['notification_services']:
        config['notification_services']['telegram'] = {}
    config['notification_services']['telegram']['token'] = os.getenv('TELEGRAM_BOT_TOKEN', config.get('notification_services', {}).get('telegram', {}).get('token'))
    config['notification_services']['telegram']['chat_id'] = os.getenv('TELEGRAM_CHAT_ID', config.get('notification_services', {}).get('telegram', {}).get('chat_id'))

    # General Settings (example, can be expanded)
    # For settings like watch_list, it's often better to manage them in the yaml,
    # but this shows how it could be done for simple values.
    min_usd_str = os.getenv('MIN_WHALE_TRANSACTION_USD')
    if min_usd_str and min_usd_str.isdigit():
        config['settings']['min_whale_transaction_usd'] = int(min_usd_str)

    # Database URL (for PostgreSQL in production)
    config['database'] = {
        'url': os.getenv('DATABASE_URL')
    }

    return config

# Create a single, memoized config instance to be used across the application
# This avoids reloading the file and re-checking env vars every time.
app_config = load_config()
