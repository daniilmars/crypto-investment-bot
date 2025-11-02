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

async def send_performance_report(summary: dict, interval_hours: int):
    """
    Sends a periodic performance report to the Telegram chat.
    """
    if not telegram_config.get('enabled'):
        log.info("Telegram notifications are disabled.")
        return

    if not TOKEN or not CHAT_ID or TOKEN == "YOUR_TELEGRAM_BOT_TOKEN":
        log.error("Telegram bot token or chat ID is not configured.")
        return

    bot = Bot(token=TOKEN)
    
    message = (
        f"📈 *Bot Performance Report ({interval_hours}h)* 📈\n\n"
        f"✅ Bot is running.\n\n"
        f"*Trades (Paper Trading):*\n"
        f"- Total Closed: {summary.get('total_closed', 0)}\n"
        f"- Wins: {summary.get('wins', 0)}\n"
        f"- Losses: {summary.get('losses', 0)}\n\n"
        f"*Performance:*\n"
        f"- Total PnL: ${summary.get('total_pnl', 0):,.2f}\n"
        f"- Win Rate: {summary.get('win_rate', 0):.2f}%\n\n"
        f"*This is a paper trading summary.*"
    )

    try:
        await bot.send_message(chat_id=CHAT_ID, text=message, parse_mode='Markdown')
        log.info("Successfully sent performance report.")
    except Exception as e:
        log.error(f"Error sending performance report: {e}")

# --- Command Handlers ---
async def start(update, context):
    """Handles the /start command."""
    await update.message.reply_text('Crypto Investment Bot is running. Use /help to see a list of available commands.')

async def help_command(update, context):
    """Handles the /help command."""
    help_text = (
        "Available Commands:\n\n"
        "/start - Check if the bot is running.\n"
        "/status - Get a detailed AI-generated market and bot health summary.\n"
        "/positions - View all open paper trades.\n"
        "/performance - Get a performance report of closed trades.\n"
        "/db_stats - View database table statistics.\n"
        "/pause - Temporarily pause new trades.\n"
        "/resume - Resume trading after a pause.\n"
        "/help - Show this help message."
    )
    await update.message.reply_text(help_text)

async def positions(update, context):
    """Handles the /positions command."""
    # --- Authorization Check ---
    if str(update.message.chat_id) != str(CHAT_ID):
        log.warning(f"Unauthorized /positions command from chat_id: {update.message.chat_id}")
        await update.message.reply_text("You are not authorized to use this command.")
        return

    try:
        from src.execution.binance_trader import get_open_positions
        from src.collectors.binance_data import get_current_price
        
        open_positions = get_open_positions()
        if not open_positions:
            await update.message.reply_text("No open positions.")
            return

        message = "📊 *Open Positions (Paper Trading)* 📊\n\n"
        total_pnl = 0

        for pos in open_positions:
            symbol = pos.get('symbol')
            quantity = pos.get('quantity', 0)
            entry_price = pos.get('entry_price', 0)
            
            price_data = get_current_price(f"{symbol}USDT")
            current_price = float(price_data.get('price', 0)) if price_data else entry_price
            
            pnl = (current_price - entry_price) * quantity
            pnl_percentage = ((current_price - entry_price) / entry_price * 100) if entry_price > 0 else 0
            total_pnl += pnl

            message += (
                f"*Symbol: {symbol}*\n"
                f"- Quantity: {quantity}\n"
                f"- Entry Price: ${entry_price:,.2f}\n"
                f"- Current Price: ${current_price:,.2f}\n"
                f"- PnL: ${pnl:,.2f} ({pnl_percentage:+.2f}%)\n\n"
            )
        
        message += f"*Total PnL on Open Positions: ${total_pnl:,.2f}*"
        await update.message.reply_text(message, parse_mode='Markdown')

    except Exception as e:
        log.error(f"Error fetching open positions: {e}")
        await update.message.reply_text("Sorry, there was an error fetching open positions.")

async def performance(update, context):
    """Handles the /performance command."""
    # --- Authorization Check ---
    if str(update.message.chat_id) != str(CHAT_ID):
        log.warning(f"Unauthorized /performance command from chat_id: {update.message.chat_id}")
        await update.message.reply_text("You are not authorized to use this command.")
        return

    try:
        from src.database import get_trade_summary
        
        # For now, we'll use the same lookback period as the status report.
        # This could be extended to accept arguments like /performance 7d
        report_hours = app_config.get('settings', {}).get('status_report_hours', 24)
        summary = get_trade_summary(hours_ago=report_hours)

        message = (
            f"📈 *Performance Report ({report_hours}h)* 📈\n\n"
            f"*Trades (Paper Trading):*\n"
            f"- Total Closed: {summary.get('total_closed', 0)}\n"
            f"- Wins: {summary.get('wins', 0)}\n"
            f"- Losses: {summary.get('losses', 0)}\n\n"
            f"*Performance:*\n"
            f"- Total PnL: ${summary.get('total_pnl', 0):,.2f}\n"
            f"- Win Rate: {summary.get('win_rate', 0):.2f}%"
        )
        await update.message.reply_text(message, parse_mode='Markdown')

    except Exception as e:
        log.error(f"Error fetching performance summary: {e}")
        await update.message.reply_text("Sorry, there was an error fetching the performance summary.")

async def pause(update, context):
    """Handles the /pause command."""
    # --- Authorization Check ---
    if str(update.message.chat_id) != str(CHAT_ID):
        log.warning(f"Unauthorized /pause command from chat_id: {update.message.chat_id}")
        await update.message.reply_text("You are not authorized to use this command.")
        return
    
    from src.state import bot_is_running
    if bot_is_running.is_set():
        bot_is_running.clear() # Pauses the bot
        log.info("Bot trading has been paused via Telegram command.")
        await update.message.reply_text("⏸️ Bot trading is now paused. Open positions will still be monitored, but no new trades will be initiated.")
    else:
        await update.message.reply_text("Bot is already paused.")

async def resume(update, context):
    """Handles the /resume command."""
    # --- Authorization Check ---
    if str(update.message.chat_id) != str(CHAT_ID):
        log.warning(f"Unauthorized /resume command from chat_id: {update.message.chat_id}")
        await update.message.reply_text("You are not authorized to use this command.")
        return

    from src.state import bot_is_running
    if not bot_is_running.is_set():
        bot_is_running.set() # Resumes the bot
        log.info("Bot trading has been resumed via Telegram command.")
        await update.message.reply_text("▶️ Bot trading has been resumed.")
    else:
        await update.message.reply_text("Bot is already running.")

async def status(update, context):
    """Handles the /status command."""
    # --- Authorization Check ---
    if str(update.message.chat_id) != str(CHAT_ID):
        log.warning(f"Unauthorized /status command from chat_id: {update.message.chat_id}")
        await update.message.reply_text("You are not authorized to use this command.")
        return
        
    await update.message.reply_text('Fetching status and generating report...')
    
    try:
        report_hours = app_config.get('settings', {}).get('status_report_hours', 24)

        # 1. Gather data
        whale_transactions = get_whale_transactions_since(hours_ago=report_hours)
        price_history = get_price_history_since(hours_ago=report_hours)
        from src.database import get_last_signal # Import here to avoid circular dependency if needed
        last_signal = get_last_signal()
        
        # 2. Generate summary
        summary = generate_market_summary(whale_transactions, price_history, last_signal)
        
        # 3. Send the report
        await update.message.reply_text(summary)
        
    except Exception as e:
        log.error(f"Error generating /status report: {e}")
        await update.message.reply_text("Sorry, there was an error generating the report.")

async def db_stats(update, context):
    """Handles the /db_stats command to get database table counts."""
    # --- Authorization Check ---
    if str(update.message.chat_id) != str(CHAT_ID):
        log.warning(f"Unauthorized /db_stats command from chat_id: {update.message.chat_id}")
        await update.message.reply_text("You are not authorized to use this command.")
        return

    try:
        from src.database import get_table_counts
        counts = get_table_counts()
        message = (
            f"📊 *Database Statistics* 📊\n\n"
            f"Whale Transactions: `{counts.get('whale_transactions', 0)}`\n"
            f"Market Prices: `{counts.get('market_prices', 0)}`\n"
            f"Signals: `{counts.get('signals', 0)}`\n"
            f"Trades: `{counts.get('trades', 0)}`"
        )
        await update.message.reply_text(message, parse_mode='Markdown')
    except Exception as e:
        log.error(f"Error fetching DB stats: {e}")
        await update.message.reply_text("Sorry, there was an error fetching database statistics.")
