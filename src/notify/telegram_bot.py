import asyncio
from telegram import Bot
from telegram.ext import Application, CommandHandler
from src.logger import log
from src.config import app_config
from src.database import get_whale_transactions_since, get_price_history_since
from src.analysis.gemini_summary import generate_market_summary

# --- Bot Initialization ---
telegram_config = app_config.get('notification_services', {}).get('telegram', {})
TOKEN = telegram_config.get('token')
CHAT_ID = telegram_config.get('chat_id')

async def send_telegram_alert(signal: dict):
    """
    Sends a formatted message to a Telegram chat using the bot instance.
    """
    if not telegram_config.get('enabled'):
        log.info("Telegram notifications are disabled.")
        return

    if not TOKEN or not CHAT_ID or TOKEN == "YOUR_TELEGRAM_BOT_TOKEN":
        log.error("Telegram bot token or chat ID is not configured.")
        return

    bot = Bot(token=TOKEN)
    
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

    try:
        await bot.send_message(chat_id=CHAT_ID, text=message, parse_mode='Markdown')
        log.info("Successfully sent Telegram alert.")
    except Exception as e:
        log.error(f"Error sending Telegram alert: {e}")

# --- Command Handlers ---
async def start(update, context):
    """Handles the /start command."""
    await update.message.reply_text('Crypto Investment Bot is running. Use /status to get a report.')

async def status(update, context):
    """Handles the /status command."""
    # --- Authorization Check ---
    if str(update.message.chat_id) != str(CHAT_ID):
        log.warning(f"Unauthorized /status command from chat_id: {update.message.chat_id}")
        await update.message.reply_text("You are not authorized to use this command.")
        return
        
    await update.message.reply_text('Fetching status and generating report...')
    
    try:
        # 1. Gather data
        whale_transactions = get_whale_transactions_since(hours_ago=24)
        price_history = get_price_history_since(hours_ago=24)
        
        # 2. Generate summary
        summary = generate_market_summary(whale_transactions, price_history)
        
        # 3. Send the report
        await update.message.reply_text(summary)
        
    except Exception as e:
        log.error(f"Error generating /status report: {e}")
        await update.message.reply_text("Sorry, there was an error generating the report.")

async def start_bot():
    """Initializes and starts the Telegram bot application."""
    if not TOKEN or TOKEN == "YOUR_TELEGRAM_BOT_TOKEN":
        log.error("Cannot start Telegram bot: Token is not configured.")
        return

    log.info("Starting Telegram bot listener...")
    application = Application.builder().token(TOKEN).build()

    # Register command handlers
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("status", status))

    # Initialize and start the application
    await application.initialize()
    await application.start()
    await application.updater.start_polling()
    log.info("Telegram bot is now polling.")

    # Keep the bot running
    while True:
        await asyncio.sleep(3600) # Sleep for an hour, the bot runs in the background

async def stop_bot(application):
    """Stops the Telegram bot gracefully."""
    log.info("Stopping Telegram bot...")
    await application.updater.stop()
    await application.stop()
    await application.shutdown()
    log.info("Telegram bot stopped.")
