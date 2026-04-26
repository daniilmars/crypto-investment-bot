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
    keys['gemini'] = _get_env('GEMINI_API_KEY', keys.get('gemini'))
    binance = keys.get('binance', {})
    binance['api_key'] = _get_env('BINANCE_API_KEY', binance.get('api_key'))
    binance['api_secret'] = _get_env('BINANCE_API_SECRET', binance.get('api_secret'))
    keys['binance'] = binance
    keys['alpha_vantage'] = _get_env('ALPHA_VANTAGE_API_KEY', keys.get('alpha_vantage'))
    alpaca = keys.get('alpaca', {})
    alpaca['api_key'] = _get_env('ALPACA_API_KEY', alpaca.get('api_key'))
    alpaca['api_secret'] = _get_env('ALPACA_API_SECRET', alpaca.get('api_secret'))
    keys['alpaca'] = alpaca
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

def _load_watch_list(settings):
    """Loads watch lists from config/watch_list.yaml and merges into settings.
    Called BEFORE env var overrides so env vars still take priority."""
    script_dir = os.path.dirname(os.path.abspath(__file__))
    watch_list_path = os.path.join(script_dir, '..', 'config', 'watch_list.yaml')
    try:
        with open(watch_list_path, 'r', encoding='utf-8') as f:
            wl_data = yaml.safe_load(f) or {}
    except FileNotFoundError:
        log.info("config/watch_list.yaml not found. Using settings.yaml watch lists.")
        return settings
    except yaml.YAMLError as e:
        log.error(f"Error parsing watch_list.yaml: {e}")
        return settings

    # Crypto symbols
    crypto_symbols = wl_data.get('symbols', [])
    if crypto_symbols:
        settings['watch_list'] = crypto_symbols

    # Stock symbols: combine US + Europe + Asia + AI + Macro ETFs
    us_stocks = wl_data.get('stocks', [])
    eu_stocks = wl_data.get('stocks_europe', [])
    asia_stocks = wl_data.get('stocks_asia', [])
    ai_stocks = wl_data.get('stocks_ai', [])
    macro_stocks = wl_data.get('stocks_macro', [])
    combined_stocks = us_stocks + eu_stocks + asia_stocks + ai_stocks + macro_stocks
    if combined_stocks:
        stock_trading = settings.get('stock_trading', {})
        stock_trading['watch_list'] = combined_stocks
        settings['stock_trading'] = stock_trading

    log.info(f"Loaded watch_list.yaml: {len(crypto_symbols)} crypto, "
             f"{len(us_stocks)} US + {len(eu_stocks)} EU + {len(asia_stocks)} Asia "
             f"+ {len(ai_stocks)} AI + {len(macro_stocks)} macro stocks")
    return settings


def _load_settings(base_config):
    """Loads general settings from config and environment variables."""
    settings = base_config.get('settings', {})

    # Load watch lists from watch_list.yaml first (before env var overrides)
    settings = _load_watch_list(settings)

    # Env var overrides for watch lists
    watch_list_env = _get_env('WATCH_LIST')
    if watch_list_env:
        settings['watch_list'] = [symbol.strip() for symbol in watch_list_env.split(';')]

    stock_watch_list_env = _get_env('STOCK_WATCH_LIST')
    if stock_watch_list_env:
        stock_trading = settings.get('stock_trading', {})
        stock_trading['watch_list'] = [symbol.strip() for symbol in stock_watch_list_env.split(';')]
        settings['stock_trading'] = stock_trading

    # Live trading env var overrides
    live_trading = settings.get('live_trading', {})
    live_enabled_env = _get_env('LIVE_TRADING_ENABLED')
    if live_enabled_env is not None:
        live_trading['enabled'] = live_enabled_env.lower() == 'true'
    live_mode_env = _get_env('LIVE_TRADING_MODE')
    if live_mode_env:
        live_trading['mode'] = live_mode_env
    if live_trading:
        settings['live_trading'] = live_trading

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

# --- Business descriptions (per-ticker, for sector-aware Gemini ranking) ---

_BUSINESS_DESCRIPTIONS_CACHE: dict[str, str] | None = None


def _load_business_descriptions() -> dict[str, str]:
    """Lazy-load + cache config/business_descriptions.yaml.

    Returns {} on any error (missing file, malformed YAML, empty descriptions).
    """
    global _BUSINESS_DESCRIPTIONS_CACHE
    if _BUSINESS_DESCRIPTIONS_CACHE is not None:
        return _BUSINESS_DESCRIPTIONS_CACHE
    script_dir = os.path.dirname(os.path.abspath(__file__))
    path = os.path.join(script_dir, '..', 'config', 'business_descriptions.yaml')
    try:
        with open(path, 'r', encoding='utf-8') as f:
            data = yaml.safe_load(f) or {}
        descs = data.get('descriptions') or {}
        if not isinstance(descs, dict):
            descs = {}
        _BUSINESS_DESCRIPTIONS_CACHE = {str(k): str(v) for k, v in descs.items() if v}
    except FileNotFoundError:
        _BUSINESS_DESCRIPTIONS_CACHE = {}
    except yaml.YAMLError as e:
        log.warning(f"business_descriptions.yaml parse error: {e}")
        _BUSINESS_DESCRIPTIONS_CACHE = {}
    return _BUSINESS_DESCRIPTIONS_CACHE


def get_business_description(symbol: str) -> str | None:
    """Returns the 1-line business description for a ticker, or None.

    Used by:
      - Gemini news prompts (gives the model context about what each ticker
        actually does, so it can rank symbols by direct catalyst impact)
      - headline_validator alias matching (so 'Merck reports Q1' counts as
        a valid headline for ticker MRK)
    """
    if not symbol:
        return None
    return _load_business_descriptions().get(str(symbol).strip())


def reset_business_descriptions_cache():
    """Test hook to force a re-read on next get_business_description() call."""
    global _BUSINESS_DESCRIPTIONS_CACHE
    _BUSINESS_DESCRIPTIONS_CACHE = None


def get_strategy_configs(settings=None):
    """Returns configured trading strategies as {name: config} dict.

    Reads from settings.strategies if present, otherwise synthesizes
    a single 'auto' strategy from the legacy auto_trading section.
    The 'manual' strategy is never included — it uses the Telegram
    confirmation flow and is handled separately.
    """
    if settings is None:
        settings = app_config.get('settings', {})

    strategies = settings.get('strategies')
    if strategies and isinstance(strategies, dict):
        return {name: cfg for name, cfg in strategies.items()
                if isinstance(cfg, dict)}

    # Fallback: synthesize from legacy auto_trading section
    auto_cfg = settings.get('auto_trading', {})
    if not auto_cfg.get('enabled', False):
        return {}
    return {'auto': auto_cfg}


# Create a single, memoized config instance.
app_config = load_config()

# Validate config at startup (fails fast with clear error)
try:
    from src.config_validation import validate_config
    validate_config(app_config)
except SystemExit:
    raise
except Exception as e:
    log.warning(f"Config validation could not run: {e}")
