import itertools
import re
from datetime import datetime, timezone
from functools import wraps
from typing import Callable, Optional

from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes
from src.logger import log
from src.config import app_config
from src.database import (
    get_price_history_since,
    get_database_schema, get_table_counts, get_trade_summary, get_last_signal
)
from src.analysis.gemini_summary import generate_market_summary
from src.gcp.costs import get_gcp_billing_summary
from src.execution.binance_trader import (get_open_positions, get_account_balance,
                                          _is_live_trading, _get_trading_mode,
                                          _get_live_balance)
from src.execution.circuit_breaker import get_circuit_breaker_status
from src.execution.stock_trader import (
    get_stock_positions, get_stock_balance, _check_pdt_rule, get_market_hours,
)
from src.collectors.binance_data import get_current_price
from src.analysis.macro_regime import get_macro_regime
from src.analysis.sector_limits import get_sector_exposure_summary
from src.analysis.event_calendar import get_upcoming_macro_events
from src.state import bot_is_running

# --- Bot Initialization ---
telegram_config = app_config.get('notification_services', {}).get('telegram', {})
TOKEN = telegram_config.get('token')
CHAT_ID = telegram_config.get('chat_id')
AUTHORIZED_USER_IDS = telegram_config.get('authorized_user_ids', [])

# --- Signal Confirmation State ---
_pending_signals: dict[int, dict] = {}  # signal_id → {signal, message_id, chat_id, created_at}
_signal_counter = itertools.count(1)
_execute_callback: Optional[Callable] = None  # registered by main.py

# Load confirmation config
_confirmation_config = app_config.get('settings', {}).get('signal_confirmation', {})
CONFIRMATION_ENABLED = _confirmation_config.get('enabled', False)
CONFIRMATION_TIMEOUT_MINUTES = _confirmation_config.get('timeout_minutes', 30)
CONFIRMATION_SIGNALS = _confirmation_config.get('require_confirmation_for', ['BUY', 'SELL'])

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

# --- Signal Confirmation Functions ---
def register_execute_callback(callback: Callable):
    """Registers the trade execution function (called from main.py at startup)."""
    global _execute_callback
    _execute_callback = callback
    log.info("Signal confirmation execute callback registered.")


def is_confirmation_required(signal_type: str) -> bool:
    """Checks if this signal type requires user confirmation."""
    return CONFIRMATION_ENABLED and signal_type in CONFIRMATION_SIGNALS


async def send_signal_for_confirmation(signal: dict) -> int:
    """Sends a signal to Telegram with inline Approve/Reject buttons.
    Returns the signal_id assigned to this pending signal."""
    if not telegram_config.get('enabled') or not TOKEN or TOKEN == "YOUR_TELEGRAM_BOT_TOKEN":
        log.error("Telegram bot is not configured. Cannot send confirmation.")
        return -1

    signal_id = next(_signal_counter)

    bot = Bot(token=TOKEN)
    signal_type = signal.get('signal', 'N/A')
    symbol = signal.get('symbol', 'N/A').upper()
    price = signal.get('current_price', 0)
    reason = _escape_md(signal.get('reason', 'No reason provided.'))
    asset_type = signal.get('asset_type', 'crypto')
    quantity = signal.get('quantity', 0)

    asset_label = "Stock" if asset_type == "stock" else "Crypto"
    mode = _get_trading_mode()
    mode_label = f" [{mode.upper()}]" if mode != 'paper' else ""

    message = f"📊 *NEW SIGNAL: {signal_type} {symbol}*{mode_label}\n\n"
    message += f"💰 *Price:* ${price:,.2f}\n"
    message += f"📈 *Reason:* {reason}\n"

    if quantity and signal_type == "BUY":
        total_value = quantity * price
        message += f"💵 *Quantity:* {quantity:.6f} {symbol} (${total_value:,.2f})\n"
    elif quantity and signal_type == "SELL":
        total_value = quantity * price
        message += f"💵 *Quantity:* {quantity:.6f} {symbol} (${total_value:,.2f})\n"
    elif quantity and signal_type == "INCREASE":
        position_data = signal.get('position', {})
        current_qty = position_data.get('quantity', 0)
        new_total = current_qty + quantity
        message += f"📦 *Current:* {current_qty:.6f} {symbol}\n"
        message += f"➕ *Adding:* {quantity:.6f} {symbol} (${quantity * price:,.2f})\n"
        message += f"📦 *New Total:* {new_total:.6f} {symbol}\n"

    message += f"⏱ *Expires in {CONFIRMATION_TIMEOUT_MINUTES} min*\n"
    message += f"_{asset_label} signal_"

    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ Execute", callback_data=f"a:{signal_id}"),
            InlineKeyboardButton("❌ Skip", callback_data=f"r:{signal_id}"),
        ]
    ])

    try:
        sent_msg = await bot.send_message(
            chat_id=CHAT_ID, text=message, parse_mode='Markdown',
            reply_markup=keyboard
        )
        _pending_signals[signal_id] = {
            'signal': signal,
            'message_id': sent_msg.message_id,
            'chat_id': CHAT_ID,
            'created_at': datetime.now(timezone.utc),
        }
        log.info(f"Signal #{signal_id} sent for confirmation: {signal_type} {symbol}")
        return signal_id
    except Exception as e:
        log.error(f"Error sending signal confirmation: {e}")
        return -1


def _escape_md(text: str) -> str:
    """Escape Telegram Markdown v1 special characters in user-facing text."""
    return re.sub(r'([_*\[\]()~`>#+\-=|{}.!])', r'\\\1', str(text))


async def _safe_edit(query, text: str):
    """Edit a callback message, falling back to plain text if Markdown fails."""
    try:
        await query.edit_message_text(text, parse_mode='Markdown')
    except Exception as md_err:
        log.warning(f"Markdown edit failed ({md_err}), retrying as plain text")
        try:
            await query.edit_message_text(text, parse_mode=None)
        except Exception as plain_err:
            log.error(f"Message edit failed completely: {plain_err}")


async def _handle_signal_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles Approve/Reject button presses for pending signals."""
    query = update.callback_query

    # Authorization check
    user_id = query.from_user.id
    if user_id not in AUTHORIZED_USER_IDS:
        await query.answer("You are not authorized.", show_alert=True)
        return

    data = query.data or ""
    if ":" not in data:
        await query.answer("Invalid action.")
        return

    action, id_str = data.split(":", 1)
    try:
        signal_id = int(id_str)
    except ValueError:
        await query.answer("Invalid signal ID.")
        return

    pending = _pending_signals.pop(signal_id, None)
    if not pending:
        await query.answer("Signal expired or already handled.", show_alert=True)
        return

    signal = pending['signal']
    signal_type = signal.get('signal', 'N/A')
    symbol = signal.get('symbol', 'N/A').upper()
    price = signal.get('current_price', 0)
    reason = _escape_md(signal.get('reason', 'No reason provided.'))
    quantity = signal.get('quantity', 0)

    if action == "a":
        # Approve — execute the trade
        await query.answer("Executing trade...")
        order_result = None
        if _execute_callback:
            try:
                order_result = await _execute_callback(signal)
            except Exception as e:
                log.error(f"Error executing confirmed signal #{signal_id}: {e}")
                error_msg = (
                    f"⚠️ *EXECUTION FAILED: {signal_type} {symbol}*\n\n"
                    f"💰 *Price:* ${price:,.2f}\n"
                    f"📈 *Reason:* {reason}\n\n"
                    f"*Error:* {_escape_md(str(e)[:200])}"
                )
                await _safe_edit(query, error_msg)
                return

        # Check if execution was skipped (e.g., duplicate position)
        if order_result and order_result.get('status') == 'SKIPPED':
            skip_reason = _escape_md(order_result.get('message', 'Unknown reason'))
            message = (
                f"⚠️ *SKIPPED: {signal_type} {symbol}*\n\n"
                f"*Reason:* {skip_reason}\n\n"
                f"_Signal was approved but could not be executed_"
            )
            await _safe_edit(query, message)
            log.info(f"Signal #{signal_id} skipped: {signal_type} {symbol} "
                     f"— {order_result.get('message')}")
            return

        # Build success message
        message = f"✅ *EXECUTED: {signal_type} {symbol}*\n\n"
        if order_result:
            fill_price = order_result.get('price', price)
            message += f"💰 *Fill price:* ${fill_price:,.2f}\n"
        else:
            message += f"💰 *Price:* ${price:,.2f}\n"
        message += f"📈 *Reason:* {reason}\n"
        if quantity:
            message += f"💵 *Quantity:* {quantity:.6f} {symbol}\n"
        if order_result:
            oco = order_result.get('oco')
            if oco:
                message += f"🎯 *TP:* ${oco['take_profit']:,.2f} | *SL:* ${oco['stop_loss']:,.2f}\n"
            pnl = order_result.get('pnl')
            if pnl is not None:
                message += f"*PnL:* ${pnl:,.2f}\n"
        message += "\n_Approved by user_"

        await _safe_edit(query, message)
        log.info(f"Signal #{signal_id} approved: {signal_type} {symbol}")

    elif action == "r":
        # Reject
        await query.answer("Signal skipped.")
        message = (
            f"❌ *SKIPPED: {signal_type} {symbol}*\n\n"
            f"💰 *Price:* ${price:,.2f}\n"
            f"📈 *Reason:* {reason}\n\n"
            f"_Rejected by user_"
        )
        await _safe_edit(query, message)
        log.info(f"Signal #{signal_id} rejected: {signal_type} {symbol}")
    else:
        await query.answer("Unknown action.")


async def cleanup_expired_signals():
    """Removes expired pending signals and edits their messages."""
    if not _pending_signals:
        return

    now = datetime.now(timezone.utc)
    expired_ids = []

    for signal_id, pending in list(_pending_signals.items()):
        age_minutes = (now - pending['created_at']).total_seconds() / 60
        if age_minutes >= CONFIRMATION_TIMEOUT_MINUTES:
            expired_ids.append(signal_id)

    if not expired_ids:
        return

    bot = Bot(token=TOKEN)
    for signal_id in expired_ids:
        pending = _pending_signals.pop(signal_id, None)
        if not pending:
            continue

        signal = pending['signal']
        signal_type = signal.get('signal', 'N/A')
        symbol = signal.get('symbol', 'N/A').upper()
        price = signal.get('current_price', 0)
        reason = signal.get('reason', 'No reason provided.')

        message = (
            f"⏰ *EXPIRED: {signal_type} {symbol}*\n\n"
            f"💰 *Price:* ${price:,.2f}\n"
            f"📈 *Reason:* {reason}\n\n"
            f"_Auto-rejected after {CONFIRMATION_TIMEOUT_MINUTES} min_"
        )
        try:
            await bot.edit_message_text(
                chat_id=pending['chat_id'],
                message_id=pending['message_id'],
                text=message, parse_mode='Markdown'
            )
        except Exception as e:
            log.warning(f"Could not edit expired signal #{signal_id} message: {e}")
        log.info(f"Signal #{signal_id} expired: {signal_type} {symbol}")


# --- Alerting Functions ---
async def send_telegram_alert(signal: dict):
    """Formats and sends a signal alert, with live trade details when available."""
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

    # Show trading mode in header for non-paper modes
    mode = _get_trading_mode()
    if mode != 'paper':
        alert_header = f"{alert_header} [{mode.upper()}]"

    message = (
        f"🚨 *{alert_header}* 🚨\n\n"
        f"*{signal_type} Signal for {symbol}*\n\n"
        f"*Price:* ${price:,.2f}\n"
        f"*Reason:* {reason}"
    )

    # Append live trade details if present
    order_result = signal.get('order_result')
    if order_result:
        fill_price = order_result.get('price', 0)
        fees = order_result.get('fees', 0)
        exchange_id = order_result.get('exchange_order_id', '')
        pnl = order_result.get('pnl')
        message += f"\n\n*Fill Price:* ${fill_price:,.2f}"
        if fees:
            message += f"\n*Fees:* ${fees:.4f}"
        if exchange_id:
            message += f"\n*Exchange Order:* `{exchange_id}`"
        if pnl is not None:
            message += f"\n*PnL:* ${pnl:,.2f}"
        oco = order_result.get('oco')
        if oco:
            message += (f"\n*OCO Bracket:* TP=${oco['take_profit']:,.2f} / "
                        f"SL=${oco['stop_loss']:,.2f}")

    await send_telegram_message(bot, CHAT_ID, message)

async def send_position_health_alert(symbol: str, current_price: float,
                                     pnl_pct: float, health: dict, position: dict):
    """Sends a position health alert when Gemini recommends exiting a position."""
    if not telegram_config.get('enabled') or not TOKEN or TOKEN == "YOUR_TELEGRAM_BOT_TOKEN":
        log.warning("Telegram bot is not configured. Skipping position health alert.")
        return
    bot = Bot(token=TOKEN)

    recommendation = health.get('recommendation', 'unknown')
    confidence = health.get('confidence', 0)
    reasoning = health.get('reasoning', 'No reasoning provided.')
    entry_price = position.get('entry_price', 0)

    # Use risk_level from Gemini if available, fall back to old logic
    risk_level = health.get('risk_level')
    if risk_level == 'red':
        emoji = "🔴"
    elif risk_level == 'yellow':
        emoji = "🟡"
    elif risk_level == 'green':
        emoji = "🟢"
    elif recommendation == 'exit':
        emoji = "🔴"
    elif confidence >= 0.5:
        emoji = "🟡"
    else:
        emoji = "🟢"

    message = (
        f"{emoji} *Position Health Alert* {emoji}\n\n"
        f"*{symbol}*\n"
        f"- Entry: ${entry_price:,.2f}\n"
        f"- Current: ${current_price:,.2f}\n"
        f"- PnL: {pnl_pct:+.2f}%\n\n"
        f"*Recommendation:* {recommendation.upper()}\n"
        f"*Confidence:* {confidence:.0%}\n"
        f"*Reasoning:* {reasoning}"
    )

    # Show primary risk if available
    primary_risk = health.get('primary_risk')
    if primary_risk and primary_risk != 'none':
        risk_label = primary_risk.replace('_', ' ').title()
        message += f"\n*Risk:* {risk_label}"

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

                # Show catalyst info if present
                catalyst_type = ga.get('catalyst_type')
                freshness = ga.get('catalyst_freshness')
                if catalyst_type and catalyst_type != 'none':
                    freshness_label = f", {freshness}" if freshness and freshness != 'none' else ""
                    lines.append(f"  Catalyst: {catalyst_type}{freshness_label}")

                # Show key headline if present
                key_headline = ga.get('key_headline')
                if key_headline:
                    lines.append(f"  _{key_headline}_")

    if gemini_assessments and gemini_assessments.get('market_mood'):
        lines.append(f"\n*Market Mood:* {gemini_assessments['market_mood']}")

    # Show cross-asset theme if present
    if gemini_assessments and gemini_assessments.get('cross_asset_theme'):
        lines.append(f"*Theme:* {gemini_assessments['cross_asset_theme']}")

    message = "\n".join(lines)
    await send_telegram_message(bot, CHAT_ID, message)

async def send_auto_bot_summary(application: Application, summary: dict,
                                positions: list, balance: dict, interval_hours: int):
    """Sends a silent summary of auto-bot performance to the configured chat."""
    if not telegram_config.get('enabled') or not TOKEN or TOKEN == "YOUR_TELEGRAM_BOT_TOKEN":
        log.warning("Telegram bot is not configured. Skipping auto-bot summary.")
        return

    total_pnl = summary.get('total_pnl', 0)
    pnl_sign = "+" if total_pnl >= 0 else ""

    message = f"*Auto-Bot Summary (last {interval_hours}h)*\n\n"
    message += f"*Balance:* ${balance.get('total_usd', 0):,.2f}\n"
    message += f"*Open Positions:* {len(positions)}\n"
    message += f"*Trades:* {summary.get('total_closed', 0)} closed\n"
    message += f"*PnL:* {pnl_sign}${total_pnl:,.2f} (win rate {summary.get('win_rate', 0):.1f}%)\n"

    if positions:
        message += "\n*Open positions:*\n"
        for pos in positions:
            symbol = pos.get('symbol', '?')
            entry = pos.get('entry_price', 0)
            qty = pos.get('quantity', 0)
            current = _get_position_price(symbol) or entry
            pnl = (current - entry) * qty
            pnl_pct = ((current - entry) / entry * 100) if entry > 0 else 0
            pnl_emoji = "+" if pnl_pct >= 0 else ""
            message += f"- {symbol}: {pnl_emoji}{pnl_pct:.1f}% (${pnl:,.2f})\n"

    try:
        await application.bot.send_message(
            chat_id=CHAT_ID, text=message, parse_mode='Markdown',
            disable_notification=True
        )
        log.info("Sent auto-bot summary.")
    except Exception as e:
        log.error(f"Error sending auto-bot summary: {e}")


async def send_market_event_alert(alert: dict):
    """Formats and sends a proactive market event alert.

    Supports 4 alert types: daily_digest, event_urgency, breaking, sector_move.
    """
    if not telegram_config.get('enabled') or not TOKEN or TOKEN == "YOUR_TELEGRAM_BOT_TOKEN":
        log.warning("Telegram bot is not configured. Skipping market alert.")
        return
    bot = Bot(token=TOKEN)

    alert_type = alert.get('type', '')

    if alert_type == 'daily_digest':
        events = alert.get('events', [])
        lookahead = alert.get('lookahead_hours', 72)
        message = f"*Daily Market Calendar* (next {lookahead}h)\n\n"
        for ev in events:
            hours = ev.get('hours_until', 0)
            urgency = " \\- NOW" if hours <= 24 else ""
            event_type = _escape_md(ev.get('event_type', '?'))
            dt = ev.get('event_date')
            date_str = dt.strftime('%b %d %H:%M UTC') if hasattr(dt, 'strftime') else str(dt)
            message += f"{'\\*' if hours <= 24 else '  '} {event_type} \\- {_escape_md(date_str)} ({hours:.0f}h){urgency}\n"
        if not events:
            message += "No major events scheduled\\.\n"

    elif alert_type == 'event_urgency':
        event_type = alert.get('event_type', 'Event')
        hours = alert.get('hours_until', 0)
        message = (
            f"*Event Alert: {_escape_md(event_type)} in {hours:.0f}h*\n\n"
            f"Review your exposure before this event\\."
        )

    elif alert_type == 'breaking':
        symbols = alert.get('symbols', [])
        catalyst = _escape_md(alert.get('catalyst_type', 'unknown'))
        market_wide = alert.get('market_wide', False)
        theme = alert.get('cross_asset_theme')
        assessments = alert.get('assessments', {})

        if market_wide:
            header = "Breaking: MARKET\\-WIDE Alert"
        else:
            header = f"Breaking: {_escape_md(', '.join(symbols))}"

        message = f"*{header}*\n"
        message += f"*Catalyst:* {catalyst}\n"
        if theme:
            message += f"*Theme:* {_escape_md(theme)}\n"
        message += f"*Symbols:* {', '.join(symbols)}\n"

        for sym, assessment in assessments.items():
            direction = assessment.get('direction', '?')
            confidence = assessment.get('confidence', 0)
            headline = assessment.get('key_headline', '')
            arrow = '+' if direction == 'bullish' else '-' if direction == 'bearish' else '~'
            message += f"\n{arrow} *{sym}* ({direction}, {confidence:.0%})"
            if headline:
                message += f"\n  {_escape_md(headline[:80])}"

    elif alert_type == 'sector_move':
        group = _escape_md(alert.get('group', '?'))
        direction = alert.get('direction', '?')
        symbols = alert.get('symbols', [])
        avg_conf = alert.get('avg_confidence', 0)
        velocity_support = alert.get('velocity_support', False)
        arrow = 'up' if direction == 'bullish' else 'down'

        message = f"*Sector Move: {group} \\({arrow}\\)*\n\n"
        message += f"*Direction:* {direction}\n"
        message += f"*Confidence:* {avg_conf:.0%}\n"
        message += f"*Symbols:* {', '.join(symbols)}\n"
        if velocity_support:
            message += "News velocity confirms trend\\.\n"

    else:
        log.warning(f"Unknown market alert type: {alert_type}")
        return

    await send_telegram_message(bot, CHAT_ID, message)
    log.info(f"Sent market alert: {alert_type}")


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
        "*General:*\n"
        "`/start` - Check if the bot is running.\n"
        "`/status` - Get a detailed market and bot health summary.\n"
        "`/positions` - View open crypto trades.\n"
        "`/performance` - Get a performance report.\n"
        "`/trading_mode` - Show current trading mode.\n"
        "`/livebalance` - Show real Binance balance.\n"
        "`/circuitbreaker` - Show circuit breaker status.\n"
        "`/pause` - Pause new trades.\n"
        "`/resume` - Resume trading.\n\n"
        "*Stocks:*\n"
        "`/stocks` - View open stock positions.\n"
        "`/stock_balance` - Show stock account balance.\n"
        "`/pdt` - PDT rule status (day trades remaining).\n"
        "`/market_hours` - NYSE open/closed status.\n\n"
        "*Auto-Bot:*\n"
        "`/auto_status` - Auto-trading shadow bot status.\n\n"
        "*System:*\n"
        "`/db_stats` - View database statistics.\n"
        "`/db_schema` - View the database schema.\n"
        "`/gcosts` - Get GCP billing summary."
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
        summary = await get_trade_summary(hours_ago=report_hours)
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
        price_history = get_price_history_since(hours_ago=report_hours)
        last_signal = get_last_signal()

        # Build open positions with current prices for the summary
        positions_for_summary = []
        try:
            open_pos = get_open_positions()
            for pos in open_pos:
                symbol = pos.get('symbol')
                entry_price = pos.get('entry_price', 0)
                current_price = _get_position_price(symbol) or entry_price
                pnl_pct = ((current_price - entry_price) / entry_price * 100) if entry_price > 0 else 0
                positions_for_summary.append({
                    'symbol': symbol,
                    'entry_price': entry_price,
                    'current_price': current_price,
                    'pnl_percentage': pnl_pct,
                    'quantity': pos.get('quantity', 0),
                })
        except Exception as e:
            log.warning(f"Could not fetch positions for status summary: {e}")

        summary = generate_market_summary(
            price_history, last_signal,
            open_positions=positions_for_summary or None,
        )
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
            f"Market Prices: `{counts.get('market_prices', 0)}`\n"
            f"Signals: `{counts.get('signals', 0)}`\n"
            f"Trades: `{counts.get('trades', 0)}`"
        )
        await update.message.reply_text(message, parse_mode='Markdown')
    except Exception as e:
        log.error(f"Error fetching DB stats: {e}")
        await update.message.reply_text("Error fetching database statistics.")

@authorized
async def trading_mode_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles the /trading_mode command."""
    mode = _get_trading_mode()
    is_live = _is_live_trading()
    settings = app_config.get('settings', {})
    paper = settings.get('paper_trading', True)
    live_config = settings.get('live_trading', {})
    message = (
        f"*Trading Mode:* `{mode}`\n\n"
        f"*paper\\_trading:* `{paper}`\n"
        f"*live\\_trading.enabled:* `{live_config.get('enabled', False)}`\n"
        f"*live\\_trading.mode:* `{live_config.get('mode', 'testnet')}`"
    )
    await update.message.reply_text(message, parse_mode='Markdown')


@authorized
async def livebalance(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles the /livebalance command."""
    if not _is_live_trading():
        await update.message.reply_text("Live trading is not active. Showing paper balance.")
    balance = get_account_balance()
    mode = _get_trading_mode()
    message = (
        f"💰 *Balance [{mode.upper()}]*\n\n"
        f"*Available USDT:* ${balance.get('USDT', 0):,.2f}\n"
        f"*Total USD:* ${balance.get('total_usd', 0):,.2f}"
    )
    await update.message.reply_text(message, parse_mode='Markdown')


@authorized
async def circuitbreaker_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles the /circuitbreaker command."""
    cb_status = get_circuit_breaker_status()
    status_emoji = "🔴" if cb_status['in_cooldown'] else "🟢"
    message = (
        f"{status_emoji} *Circuit Breaker Status*\n\n"
        f"*Active:* {'YES — trading halted' if cb_status['in_cooldown'] else 'No — trading allowed'}\n"
        f"*Cooldown:* {cb_status['cooldown_hours']}h\n\n"
        f"*Thresholds:*\n"
        f"- Balance floor: ${cb_status['balance_floor']:.2f}\n"
        f"- Daily loss limit: {cb_status['daily_loss_limit_pct']*100:.0f}%\n"
        f"- Max drawdown: {cb_status['max_drawdown_pct']*100:.0f}%\n"
        f"- Max consecutive losses: {cb_status['max_consecutive_losses']}"
    )
    last = cb_status.get('last_event')
    if last:
        message += (f"\n\n*Last event:* {last.get('event_type', 'unknown')}\n"
                    f"*Details:* {last.get('details', 'N/A')}\n"
                    f"*Triggered:* {last.get('triggered_at', 'N/A')}")
    await update.message.reply_text(message, parse_mode='Markdown')


@authorized
async def stocks_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles the /stocks command — shows open stock positions with real-time P&L."""
    try:
        stock_settings = app_config.get('settings', {}).get('stock_trading', {})
        broker = stock_settings.get('broker', 'paper_only')

        if broker == 'alpaca':
            positions = get_stock_positions()
            if not positions:
                await update.message.reply_text("No open stock positions on Alpaca.")
                return
            message = "📊 *Stock Positions [ALPACA]* 📊\n\n"
            total_pnl = 0
            for pos in positions:
                pnl = pos.get('unrealized_pl', 0)
                pnl_pct = pos.get('unrealized_plpc', 0) * 100
                total_pnl += pnl
                message += (
                    f"*{pos['symbol']}*\n"
                    f"- Qty: {pos['quantity']:.4f}\n"
                    f"- Entry: ${pos['entry_price']:,.2f}\n"
                    f"- Current: ${pos['current_price']:,.2f}\n"
                    f"- PnL: ${pnl:,.2f} ({pnl_pct:+.2f}%)\n\n"
                )
            message += f"*Total Unrealized PnL: ${total_pnl:,.2f}*"
        else:
            positions = get_open_positions(asset_type='stock')
            if not positions:
                await update.message.reply_text("No open stock positions (paper).")
                return
            message = "📊 *Stock Positions [PAPER]* 📊\n\n"
            total_pnl = 0
            for pos in positions:
                symbol = pos.get('symbol')
                quantity = pos.get('quantity', 0)
                entry_price = pos.get('entry_price', 0)
                current_price = _get_position_price(symbol) or entry_price
                pnl = (current_price - entry_price) * quantity
                pnl_pct = ((current_price - entry_price) / entry_price * 100) if entry_price > 0 else 0
                total_pnl += pnl
                message += (
                    f"*{symbol}*\n"
                    f"- Qty: {quantity:.4f}\n"
                    f"- Entry: ${entry_price:,.2f}\n"
                    f"- Current: ${current_price:,.2f}\n"
                    f"- PnL: ${pnl:,.2f} ({pnl_pct:+.2f}%)\n\n"
                )
            message += f"*Total Unrealized PnL: ${total_pnl:,.2f}*"

        await update.message.reply_text(message, parse_mode='Markdown')
    except Exception as e:
        log.error(f"Error in /stocks: {e}")
        await update.message.reply_text("Error fetching stock positions.")


@authorized
async def stock_balance_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles the /stock_balance command — shows Alpaca account balance."""
    try:
        stock_settings = app_config.get('settings', {}).get('stock_trading', {})
        broker = stock_settings.get('broker', 'paper_only')

        if broker == 'alpaca':
            balance = get_stock_balance()
            message = (
                f"💰 *Stock Balance [ALPACA]*\n\n"
                f"*Cash:* ${balance.get('cash', 0):,.2f}\n"
                f"*Equity:* ${balance.get('equity', 0):,.2f}\n"
                f"*Portfolio Value:* ${balance.get('portfolio_value', 0):,.2f}\n"
                f"*Buying Power:* ${balance.get('buying_power', 0):,.2f}"
            )
        else:
            balance = get_account_balance(asset_type='stock')
            message = (
                f"💰 *Stock Balance [PAPER]*\n\n"
                f"*Available:* ${balance.get('USDT', 0):,.2f}\n"
                f"*Total:* ${balance.get('total_usd', 0):,.2f}"
            )
        await update.message.reply_text(message, parse_mode='Markdown')
    except Exception as e:
        log.error(f"Error in /stock_balance: {e}")
        await update.message.reply_text("Error fetching stock balance.")


@authorized
async def pdt_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles the /pdt command — shows Pattern Day Trader rule status."""
    try:
        pdt = _check_pdt_rule()
        status_emoji = "🔴" if pdt['is_restricted'] else "🟢"
        message = (
            f"{status_emoji} *PDT Rule Status*\n\n"
            f"*Day trades used:* {pdt['day_trades_used']} / 3\n"
            f"*Remaining:* {pdt['day_trades_remaining']}\n"
            f"*Restricted:* {'YES — no more day trades' if pdt['is_restricted'] else 'No'}\n\n"
            f"_Rolling 5 business day window_"
        )
        await update.message.reply_text(message, parse_mode='Markdown')
    except Exception as e:
        log.error(f"Error in /pdt: {e}")
        await update.message.reply_text("Error checking PDT status.")


@authorized
async def auto_status_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles the /auto_status command — shows auto-bot performance and positions."""
    try:
        auto_cfg = app_config.get('settings', {}).get('auto_trading', {})
        if not auto_cfg.get('enabled', False):
            await update.message.reply_text("Auto-trading shadow bot is disabled.")
            return

        auto_positions = get_open_positions(trading_strategy='auto')
        auto_balance = get_account_balance(trading_strategy='auto')
        auto_stats = await get_trade_summary(hours_ago=24 * 365, trading_strategy='auto')  # all-time

        manual_balance = get_account_balance()
        initial = auto_cfg.get('paper_trading_initial_capital', 10000.0)
        auto_total = auto_balance.get('total_usd', initial)
        manual_total = manual_balance.get('total_usd', 0)
        auto_return = ((auto_total - initial) / initial * 100) if initial > 0 else 0
        manual_initial = app_config.get('settings', {}).get('paper_trading_initial_capital', 10000.0)
        manual_return = ((manual_total - manual_initial) / manual_initial * 100) if manual_initial > 0 else 0

        message = "*Auto-Bot Status*\n\n"
        message += f"*Balance:* ${auto_total:,.2f}\n"
        message += f"*Available:* ${auto_balance.get('USDT', 0):,.2f}\n"
        message += f"*Open Positions:* {len(auto_positions)}\n"
        message += f"*All-time PnL:* ${auto_stats.get('total_pnl', 0):,.2f}\n"
        message += f"*Trades:* {auto_stats.get('total_closed', 0)} ({auto_stats.get('wins', 0)}W / {auto_stats.get('losses', 0)}L)\n"
        message += f"*Win Rate:* {auto_stats.get('win_rate', 0):.1f}%\n\n"

        if auto_positions:
            message += "*Open positions:*\n"
            for pos in auto_positions:
                symbol = pos.get('symbol', '?')
                entry = pos.get('entry_price', 0)
                qty = pos.get('quantity', 0)
                current = _get_position_price(symbol) or entry
                pnl = (current - entry) * qty
                pnl_pct = ((current - entry) / entry * 100) if entry > 0 else 0
                message += f"- *{symbol}*: ${current:,.2f} ({pnl_pct:+.1f}%, ${pnl:,.2f})\n"
            message += "\n"

        message += f"_Manual: {manual_return:+.1f}% vs Auto: {auto_return:+.1f}%_"

        await update.message.reply_text(message, parse_mode='Markdown')
    except Exception as e:
        log.error(f"Error in /auto_status: {e}")
        await update.message.reply_text("Error fetching auto-bot status.")


@authorized
async def market_hours_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles the /market_hours command — shows NYSE open/closed status."""
    try:
        hours = get_market_hours()
        status_emoji = "🟢" if hours['is_open'] else "🔴"
        message = (
            f"{status_emoji} *Market Hours (NYSE)*\n\n"
            f"*Status:* {'OPEN' if hours['is_open'] else 'CLOSED'}\n"
            f"*Next open:* {hours['next_open']}\n"
            f"*Next close:* {hours['next_close']}"
        )
        await update.message.reply_text(message, parse_mode='Markdown')
    except Exception as e:
        log.error(f"Error in /market_hours: {e}")
        await update.message.reply_text("Error checking market hours.")


@authorized
async def regime_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles the /regime command — shows current macro regime."""
    try:
        regime = get_macro_regime()
        regime_emoji = {'RISK_ON': '🟢', 'CAUTION': '🟡', 'RISK_OFF': '🔴'}
        emoji = regime_emoji.get(regime['regime'], '⚪')
        signals = regime.get('signals', {})
        message = (
            f"{emoji} *Macro Regime: {regime['regime']}*\n\n"
            f"*Score:* {regime.get('score', 0)}\n"
            f"*Position multiplier:* {regime['position_size_multiplier']:.1f}x\n"
            f"*Suppress BUYs:* {'Yes' if regime['suppress_buys'] else 'No'}\n\n"
            f"*Signals:*\n"
            f"  VIX level: {signals.get('vix_signal', '?')}\n"
            f"  VIX trend: {signals.get('vix_trend', '?')}\n"
            f"  S&P 500: {signals.get('sp500_trend', '?')}\n"
            f"  10Y yield: {signals.get('yield_direction', '?')}\n"
            f"  BTC trend: {signals.get('btc_trend', '?')}"
        )
        await update.message.reply_text(message, parse_mode='Markdown')
    except Exception as e:
        log.error(f"Error in /regime: {e}")
        await update.message.reply_text("Error fetching macro regime.")


@authorized
async def sectors_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles the /sectors command — shows sector exposure."""
    try:
        crypto_positions = get_open_positions()
        stock_positions = get_open_positions(asset_type='stock')
        all_positions = crypto_positions + stock_positions
        summary = get_sector_exposure_summary(all_positions)

        if not summary:
            await update.message.reply_text("No sector exposure — no open positions.")
            return

        lines = ["*Sector Exposure*\n"]
        for group, data in sorted(summary.items()):
            bar = '█' * data['current'] + '░' * (data['limit'] - data['current'])
            syms = ', '.join(data['symbols'])
            lines.append(f"*{group}* [{data['current']}/{data['limit']}] {bar}\n  {syms}")

        await update.message.reply_text('\n'.join(lines), parse_mode='Markdown')
    except Exception as e:
        log.error(f"Error in /sectors: {e}")
        await update.message.reply_text("Error fetching sector exposure.")


@authorized
async def events_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles the /events command — shows upcoming FOMC, CPI, and earnings."""
    try:
        events = get_upcoming_macro_events(days_ahead=30)
        if not events:
            await update.message.reply_text("No upcoming macro events in the next 30 days.")
            return

        lines = ["*Upcoming Macro Events (30d)*\n"]
        for evt in events[:10]:
            dt = evt['event_date']
            hours = evt['hours_until']
            days = hours / 24
            if days < 1:
                time_str = f"{hours:.0f}h"
            else:
                time_str = f"{days:.1f}d"
            lines.append(
                f"  {evt['event_type']}: {dt.strftime('%b %d %H:%M UTC')} ({time_str})")

        await update.message.reply_text('\n'.join(lines), parse_mode='Markdown')
    except Exception as e:
        log.error(f"Error in /events: {e}")
        await update.message.reply_text("Error fetching events.")


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
            CommandHandler("gcosts", gcosts), CommandHandler("db_schema", db_schema),
            CommandHandler("trading_mode", trading_mode_cmd),
            CommandHandler("livebalance", livebalance),
            CommandHandler("circuitbreaker", circuitbreaker_cmd),
            CommandHandler("stocks", stocks_cmd),
            CommandHandler("stock_balance", stock_balance_cmd),
            CommandHandler("pdt", pdt_cmd),
            CommandHandler("market_hours", market_hours_cmd),
            CommandHandler("auto_status", auto_status_cmd),
            CommandHandler("regime", regime_cmd),
            CommandHandler("sectors", sectors_cmd),
            CommandHandler("events", events_cmd),
            CallbackQueryHandler(_handle_signal_callback),
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
