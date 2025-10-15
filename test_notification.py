# test_notification.py
from src.notify.telegram_bot import send_telegram_alert

def run_test():
    """Sends a test notification to Telegram."""
    print("--- Running Telegram Notification Test ---")
    
    test_signal = {
        'signal': 'TEST',
        'reason': 'This is a test message to confirm the Heroku deployment is working correctly.',
        'symbol': 'BOT',
        'current_price': 'N/A'
    }
    
    try:
        send_telegram_alert(test_signal)
        print("Test message sent successfully!")
    except Exception as e:
        print(f"An error occurred: {e}")

if __name__ == "__main__":
    run_test()
