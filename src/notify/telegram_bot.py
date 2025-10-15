import requests
from src.logger import log
from src.config import app_config

# Telegram Bot API base URL
TELEGRAM_API_URL = "https://api.telegram.org/bot"

def send_telegram_alert(signal: dict):
    """
    Sends a formatted message to a Telegram chat.
    """
    telegram_config = app_config.get('notification_services', {}).get('telegram', {})
    
    if not telegram_config.get('enabled'):
        log.info("Telegram notifications are disabled.")
        return

    token = telegram_config.get('token')
    chat_id = telegram_config.get('chat_id')

    if not token or not chat_id or token == "YOUR_TELEGRAM_BOT_TOKEN":
        log.error("Telegram bot token or chat ID is not configured.")
        return

    # Format the message
    signal_type = signal.get('signal', 'N/A')
    symbol = signal.get('symbol', 'N/A').upper()
    price = signal.get('current_price', 0)
    reason = signal.get('reason', 'No reason provided.')
    
    message = (
        f"🚨 *Crypto Alert* 🚨\n\n"
        f"*{signal_type} Signal for {symbol}*\n\n"
        f"*Price:* ${price:,.2f}\n"
        f"*Reason:* {reason}"
    )

    # Send the message
    url = f"{TELEGRAM_API_URL}{token}/sendMessage"
    params = {
        'chat_id': chat_id,
        'text': message,
        'parse_mode': 'Markdown'
    }

    try:
        response = requests.post(url, params=params)
        response.raise_for_status()
        log.info("Successfully sent Telegram alert.")
    except requests.exceptions.RequestException as e:
        log.error(f"Error sending Telegram alert: {e}")
