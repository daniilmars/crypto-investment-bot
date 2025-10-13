import yaml
import requests
import os

# --- Configuration Loading ---

def load_config():
    """Loads the configuration from the settings.yaml file."""
    # Construct the absolute path to the config file
    # This makes the script runnable from any directory
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

# --- Telegram Bot Logic ---

def send_telegram_alert(signal_data: dict):
    """
    Sends a formatted alert to a Telegram chat.

    Args:
        signal_data (dict): A dictionary containing the signal information,
                            typically from the signal_engine.
    
    Returns:
        bool: True if the message was sent successfully, False otherwise.
    """
    config = load_config()
    if not config:
        return False

    telegram_config = config.get('notification_services', {}).get('telegram', {})
    if not telegram_config.get('enabled'):
        print("Telegram notifications are disabled in the configuration.")
        return False

    bot_token = telegram_config.get('token')
    chat_id = telegram_config.get('chat_id')

    if not bot_token or not chat_id or bot_token == "YOUR_TELEGRAM_BOT_TOKEN":
        print("Error: Telegram bot_token or chat_id is not configured in config/settings.yaml")
        return False

    # Format the message
    signal = signal_data.get('signal', 'N/A')
    reason = signal_data.get('reason', 'No reason provided.')
    
    # Emojis for different signals
    emojis = {
        "BUY": "🚨🟢",
        "SELL": "🚨🔴",
        "HOLD": "⚪️"
    }
    
    message = (
        f"{emojis.get(signal, 'ℹ️')} **Crypto Alert**\n\n"
        f"**Signal:** {signal}\n"
        f"**Reason:** {reason}\n"
    )

    # Construct the API URL
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    
    payload = {
        'chat_id': chat_id,
        'text': message,
        'parse_mode': 'Markdown'
    }

    try:
        response = requests.post(url, json=payload)
        response.raise_for_status()
        
        if response.json().get('ok'):
            print("Successfully sent Telegram alert.")
            return True
        else:
            print(f"Error from Telegram API: {response.json().get('description')}")
            return False

    except requests.exceptions.RequestException as e:
        print(f"Error sending Telegram alert: {e}")
        return False

if __name__ == '__main__':
    # This block allows you to run the script directly for testing purposes.
    # IMPORTANT: You must fill in your bot token and chat ID in config/settings.yaml for this to work.
    
    print("--- Testing Telegram Notifier ---")

    # Test case 1: A BUY signal
    print("\nTesting with a BUY signal...")
    buy_signal = {
        'signal': 'BUY',
        'reason': 'Fear & Greed Index is at 15 (Extreme Fear), indicating a potential buying opportunity.'
    }
    send_telegram_alert(buy_signal)

    # Test case 2: A SELL signal
    print("\nTesting with a SELL signal...")
    sell_signal = {
        'signal': 'SELL',
        'reason': 'Fear & Greed Index is at 85 (Extreme Greed), indicating a potential selling opportunity.'
    }
    send_telegram_alert(sell_signal)
    
    print("\n--- Test Complete ---")
    print("Check your Telegram for the test messages.")
