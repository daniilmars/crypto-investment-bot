from functools import wraps
from telegram import Bot, Update
from telegram.ext import Application, CommandHandler, ContextTypes
from src.logger import log
from src.config import app_config
from src.database import (
    get_whale_transactions_since, get_price_history_since,
    get_database_schema, get_table_counts, get_trade_summary, get_last_signal
)
from src.analysis.gemini_summary import generate_market_summary
from src.gcp.costs import get_gcp_billing_summary
from src.execution.binance_trader import get_open_positions
from src.collectors.binance_data import get_current_price
from src.state import bot_is_running

# --- Bot Initialization ---
telegram_config = app_config.get('notification_services', {}).get('telegram', {})
TOKEN = telegram_config.get('token')
CHAT_ID = telegram_config.get('chat_id')
AUTHORIZED_USER_IDS = telegram_config.get('authorized_user_ids', [])

# --- Decorators ---
def authorized(func):
    """Decorator to check if a user is authorized."""
    @wraps(func)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE, *args, **kwargs):
        user_id = update.message.from_user.id
        if user_id not in AUTHORIZED_USER_IDS:
            log.warning(f"Unauthorized access attempt by user_id: {user_id}")
            await update.message.reply_text("You are not authorized to use this command.")
            return
        return await func(update, context, *args, **kwargs)
    return wrapper

# --- Helper Functions ---
async def send_telegram_message(bot: Bot, chat_id: str, message: str):
    """Sends a message to a Telegram chat."""
    try:
        await bot.send_message(chat_id=chat_id, text=message, parse_mode='Markdown')
        log.info("Successfully sent Telegram message.")
    except Exception as e:
        log.error(f"Error sending Telegram message: {e}")

# --- Alerting Functions ---
async def send_telegram_alert(signal: dict):
    """Formats and sends a signal alert."""
    if not telegram_config.get('enabled') or not TOKEN or TOKEN == "YOUR_TELEGRAM_BOT_TOKEN":
        log.error("Telegram bot is not configured.")
        return
    bot = Bot(token=TOKEN)
    signal_type = signal.get('signal', 'N/A')
    symbol = signal.get('symbol', 'N/A').upper()
    price = signal.get('current_price', 0)
    reason = signal.get('reason', 'No reason provided.')
    asset_type = signal.get('asset_type', 'crypto')
    alert_header = "Stock Alert" if asset_type == "stock" else "Crypto Alert"
    message = (
        f"🚨 *{alert_header}* 🚨\n\n"
        f"*{signal_type} Signal for {symbol}*\n\n"
        f"*Price:* ${price:,.2f}\n"
        f"*Reason:* {reason}"
    )
    await send_telegram_message(bot, CHAT_ID, message)

async def send_news_alert(triggered_symbols, sentiment_data, gemini_assessments=None):
    """Sends a breaking news alert when news volume spikes or sentiment shifts."""
    if not telegram_config.get('enabled') or not TOKEN or TOKEN == "YOUR_TELEGRAM_BOT_TOKEN":
        log.warning("Telegram bot is not configured. Skipping news alert.")
        return
    bot = Bot(token=TOKEN)

    lines = ["*Breaking News Alert*\n"]
    for symbol in triggered_symbols:
        sym_data = sentiment_data.get(symbol, {})
        avg_score = sym_data.get('avg_sentiment_score', 0)
        volume = sym_data.get('news_volume', 0)
        lines.append(f"*{symbol}:* {volume} articles, VADER sentiment {avg_score:+.3f}")

        # Add Gemini assessment if available
        if gemini_assessments:
            ga = gemini_assessments.get('symbol_assessments', {}).get(symbol)
            if ga:
                direction = ga.get('direction', 'neutral')
                confidence = ga.get('confidence', 0)
                reasoning = ga.get('reasoning', '')
                lines.append(f"  Gemini: {direction} (conf {confidence:.2f}) — {reasoning}")

    if gemini_assessments and gemini_assessments.get('market_mood'):
        lines.append(f"\n*Market Mood:* {gemini_assessments['market_mood']}")

    message = "\n".join(lines)
    await send_telegram_message(bot, CHAT_ID, message)

async def send_performance_report(application: Application, summary: dict, interval_hours: int):
    """Formats and sends a performance report."""
    if not telegram_config.get('enabled') or not TOKEN or TOKEN == "YOUR_TELEGRAM_BOT_TOKEN":
        log.error("Telegram bot is not configured.")
        return
    
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
    await application.bot.send_message(chat_id=CHAT_ID, text=message, parse_mode='Markdown')
    log.info("Successfully sent hourly performance report.")

# --- Command Handlers ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles the /start command."""
    await update.message.reply_text('Crypto Investment Bot is running. Use /help for commands.')

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles the /help command."""
    help_text = (
        "🤖 *Crypto Investment Bot Help*\n\n"
        "`/start` - Check if the bot is running.\n"
        "`/status` - Get a detailed market and bot health summary.\n"
        "`/positions` - View open paper trades.\n"
        "`/performance` - Get a performance report.\n"
        "`/pause` - Pause new trades.\n"
        "`/resume` - Resume trading.\n"
        "`/db_stats` - View database statistics.\n"
        "`/db_schema` - View the database schema.\n"
        "`/gcosts` - Get GCP billing summary (authorized users only)."
    )
    await update.message.reply_text(help_text, parse_mode='Markdown')

@authorized
async def db_schema(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles the /db_schema command."""
    await update.message.reply_text("Fetching database schema...")
    try:
        tables = get_database_schema()
        message = "📋 *Database Schema*\n\n" + "\n".join([f"- `{table}`" for table in tables]) if tables else "Database is empty."
        await update.message.reply_text(message, parse_mode='Markdown')
    except Exception as e:
        log.error(f"Error fetching DB schema: {e}")
        await update.message.reply_text("Error fetching database schema.")

@authorized
async def gcosts(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles the /gcosts command."""
    await update.message.reply_text("Fetching GCP billing summary...")
    summary = get_gcp_billing_summary()
    await update.message.reply_text(summary, parse_mode='Markdown')

def _is_stock_symbol(symbol):
    """Checks if a symbol is a stock (present in stock_trading watch_list)."""
    stock_watch_list = app_config.get('settings', {}).get('stock_trading', {}).get('watch_list', [])
    return symbol in stock_watch_list

def _get_position_price(symbol):
    """Gets current price for a position, using the appropriate data source."""
    if _is_stock_symbol(symbol):
        from src.collectors.alpha_vantage_data import get_stock_price
        price_data = get_stock_price(symbol)
        return price_data.get('price', 0) if price_data else 0
    else:
        price_data = get_current_price(f"{symbol}USDT")
        return float(price_data.get('price', 0)) if price_data else 0

async def positions(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles the /positions command."""
    try:
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
            current_price = _get_position_price(symbol) or entry_price
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
        await update.message.reply_text("Error fetching open positions.")

async def performance(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles the /performance command."""
    try:
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
        await update.message.reply_text("Error fetching performance summary.")

@authorized
async def pause(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles the /pause command."""
    if bot_is_running.is_set():
        bot_is_running.clear()
        log.info("Bot trading paused via Telegram.")
        await update.message.reply_text("⏸️ Bot trading paused.")
    else:
        await update.message.reply_text("Bot is already paused.")

@authorized
async def resume(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles the /resume command."""
    if not bot_is_running.is_set():
        bot_is_running.set()
        log.info("Bot trading resumed via Telegram.")
        await update.message.reply_text("▶️ Bot trading resumed.")
    else:
        await update.message.reply_text("Bot is already running.")

async def status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles the /status command."""
    await update.message.reply_text('Fetching status and generating report...')
    try:
        report_hours = app_config.get('settings', {}).get('status_report_hours', 24)
        whale_transactions = get_whale_transactions_since(hours_ago=report_hours)
        price_history = get_price_history_since(hours_ago=report_hours)
        last_signal = get_last_signal()
        summary = generate_market_summary(whale_transactions, price_history, last_signal)
        await update.message.reply_text(summary)
    except Exception as e:
        log.error(f"Error generating /status report: {e}")
        await update.message.reply_text("Error generating report.")

async def db_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles the /db_stats command."""
    try:
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
        await update.message.reply_text("Error fetching database statistics.")

# --- Bot Lifecycle Management ---
async def start_bot() -> Application:
    """
    Initializes and starts the Telegram bot.
    """
    log.info("Entering start_bot function.")
    if not telegram_config.get('enabled') or not TOKEN or TOKEN == "YOUR_TELEGRAM_BOT_TOKEN":
        log.warning("Telegram bot is not enabled or configured.")
        return None
    try:
        log.info("Starting Telegram bot...")
        application = Application.builder().token(TOKEN).build()
        handlers = [
            CommandHandler("start", start), CommandHandler("help", help_command),
            CommandHandler("status", status), CommandHandler("db_stats", db_stats),
            CommandHandler("positions", positions), CommandHandler("performance", performance),
            CommandHandler("pause", pause), CommandHandler("resume", resume),
            CommandHandler("gcosts", gcosts), CommandHandler("db_schema", db_schema)
        ]
        application.add_handlers(handlers)
        await application.initialize()
        await application.start()
        log.info("Deleting any existing webhooks...")
        await application.bot.delete_webhook()
        log.info("Calling application.updater.start_polling...")
        await application.updater.start_polling(drop_pending_updates=True)
        log.info("Telegram bot started successfully and polling initiated.")
        return application
    except Exception as e:
        log.error(f"Failed to start Telegram bot: {e}", exc_info=True)
        return None

async def stop_bot(application: Application):
    """Gracefully stops the Telegram bot."""
    if application:
        log.info("Stopping Telegram bot...")
        try:
            await application.updater.stop()
            await application.stop()
            await application.shutdown()
            log.info("Telegram bot stopped successfully.")
        except Exception as e:
            log.error(f"Error stopping Telegram bot: {e}", exc_info=True)
