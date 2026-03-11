"""Live dashboard: pinned auto-updating message + daily recap.

Replaces the flood of periodic Telegram messages (morning briefing,
portfolio digest, performance report, auto-bot summary) with a single
pinned message that updates in-place every cycle.
"""

from datetime import datetime, timezone

from telegram.error import BadRequest
from telegram.ext import Application

from src.config import app_config
from src.database import load_bot_state, save_bot_state, get_trades_closed_today
from src.logger import log
from src.notify.formatting import truncate_for_telegram
from src.orchestration import bot_state

# Module-level state
_dashboard_message_id: int | None = None
_last_dashboard_text: str | None = None


def _get_chat_id() -> str:
    return app_config.get('notification_services', {}).get(
        'telegram', {}).get('chat_id', '')


async def load_dashboard_state():
    """Load persisted dashboard message ID from DB. Called once at startup."""
    global _dashboard_message_id
    try:
        stored = load_bot_state('dashboard_message_id')
        if stored:
            _dashboard_message_id = int(stored)
            log.info(f"Loaded dashboard message ID: {_dashboard_message_id}")
    except Exception as e:
        log.warning(f"Could not load dashboard state: {e}")


def _format_hold_duration(entry_ts) -> str:
    """Format hold duration as compact string like '2d 4h' or '6h'."""
    if not entry_ts:
        return '?'
    try:
        if isinstance(entry_ts, str):
            dt = datetime.fromisoformat(entry_ts.replace('Z', '+00:00'))
        else:
            dt = entry_ts
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        delta = datetime.now(timezone.utc) - dt
        total_hours = delta.total_seconds() / 3600
        if total_hours >= 24:
            days = int(total_hours // 24)
            hours = int(total_hours % 24)
            return f"{days}d {hours}h"
        return f"{int(total_hours)}h"
    except Exception:
        return '?'


def _get_strategy_label(position: dict) -> str:
    """Return a label emoji for strategic positions."""
    strategy = position.get('strategy_type', '')
    if not strategy:
        return ''
    labels = {
        'growth': 'growth',
        'sector_thesis': 'sector',
        'value': 'value',
        'macro_trend': 'macro',
        'speculative': 'spec',
    }
    label = labels.get(strategy, strategy)
    return f' \U0001f4cc {label}'


def build_live_dashboard(cycle_data: dict) -> str:
    """Build the live dashboard text from cycle data.

    Args:
        cycle_data: dict with keys: crypto_positions, stock_positions,
            auto_positions, crypto_balance, stock_balance, daily_pnl,
            regime, cb_status, events, prices, last_signals, auto_summary
    """
    now = datetime.now(timezone.utc)
    time_str = now.strftime('%H:%M UTC')

    # Balances
    crypto_bal = cycle_data.get('crypto_balance', {})
    stock_bal = cycle_data.get('stock_balance', {})
    total = crypto_bal.get('total_usd', 0) + stock_bal.get('total_usd', 0)

    daily_pnl = cycle_data.get('daily_pnl', 0)
    pnl_sign = '+' if daily_pnl >= 0 else ''
    pnl_pct = (daily_pnl / (total - daily_pnl) * 100) if (
        total - daily_pnl) > 0 else 0
    pnl_pct_sign = '+' if pnl_pct >= 0 else ''

    # Regime
    regime = cycle_data.get('regime', {})
    regime_name = regime.get('regime', '?')
    regime_mult = regime.get('position_size_multiplier', 1.0)

    # Circuit breaker
    cb = cycle_data.get('cb_status', {})
    cb_label = 'HALT' if cb.get('in_cooldown') else 'OK'

    lines = [
        f"\U0001f4ca DASHBOARD                          "
        f"\U0001f504 {time_str}",
        '',
        f"\U0001f4b0 ${total:,.0f}  ({pnl_sign}${daily_pnl:,.0f} today, "
        f"{pnl_pct_sign}{pnl_pct:.1f}%)",
        f"\u26a1 {regime_name} ({regime_mult}x) | CB: {cb_label}",
    ]

    # Positions
    crypto_pos = cycle_data.get('crypto_positions', [])
    stock_pos = cycle_data.get('stock_positions', [])
    prices = cycle_data.get('prices', {})

    settings = app_config.get('settings', {})
    max_crypto = settings.get('max_concurrent_positions', 5)
    max_stocks = settings.get('stock_trading', {}).get(
        'max_concurrent_positions', 8)

    lines.append('')
    lines.append(
        f"\u2500\u2500 Positions ({len(crypto_pos)}/{max_crypto} crypto, "
        f"{len(stock_pos)}/{max_stocks} stocks) \u2500\u2500"
    )

    def _format_position(pos):
        symbol = pos.get('symbol', '?')
        entry = pos.get('entry_price', 0)
        qty = pos.get('quantity', 0)
        current = prices.get(symbol, entry)
        pnl = (current - entry) * qty
        pnl_pct_val = ((current - entry) / entry * 100) if entry > 0 else 0
        emoji = '\U0001f7e2' if pnl_pct_val >= 0 else '\U0001f534'
        hold = _format_hold_duration(pos.get('entry_timestamp'))

        # Trailing stop indicator
        order_id = pos.get('order_id', '')
        peak = bot_state.get_peak(order_id) if order_id else None
        ts_indicator = ''
        if peak is not None:
            activation = settings.get('trailing_stop_activation', 0.02)
            if entry > 0 and (current - entry) / entry >= activation:
                ts_indicator = ' \u27f2 trailing'

        strategy = _get_strategy_label(pos)
        return (
            f" {emoji} {symbol:<6} {pnl_pct_val:+.1f}%   "
            f"${pnl:+,.0f}   {hold}{ts_indicator}{strategy}"
        )

    for pos in crypto_pos:
        if pos.get('status') == 'OPEN':
            lines.append(_format_position(pos))

    if crypto_pos and stock_pos:
        lines.append(' \u2500 \u2500 \u2500')

    for pos in stock_pos:
        if pos.get('status') == 'OPEN':
            lines.append(_format_position(pos))

    if not crypto_pos and not stock_pos:
        lines.append(' No open positions')

    # Events
    events = cycle_data.get('events', [])
    if events:
        lines.append('')
        lines.append('\u2500\u2500 Events \u2500\u2500')
        for evt in events[:4]:
            hours = evt.get('hours_until', 0)
            evt_type = evt.get('event_type', '?')
            if hours <= 24:
                lines.append(f' \u26a0\ufe0f  {evt_type} in {hours:.0f}h')
            else:
                days = hours / 24
                lines.append(f'     {evt_type} in {days:.0f}d')

    # Auto-bot
    auto_pos = cycle_data.get('auto_positions', [])
    auto_summary = cycle_data.get('auto_summary', {})
    auto_open = [p for p in auto_pos if p.get('status') == 'OPEN']
    if auto_summary or auto_open:
        lines.append('')
        lines.append('\u2500\u2500 Auto-Bot \u2500\u2500')
        auto_pnl = auto_summary.get('total_pnl', 0)
        auto_closed = auto_summary.get('total_closed', 0)
        auto_win = auto_summary.get('win_rate', 0)
        lines.append(
            f' {len(auto_open)} open | '
            f'${auto_pnl:+,.0f} today | '
            f'{auto_win:.0f}% win ({auto_closed} trades)'
        )

    # Last signals
    last_signals = cycle_data.get('last_signals', [])
    if last_signals:
        lines.append('')
        lines.append('\u2500\u2500 Last Signals \u2500\u2500')
        for sig in last_signals[:3]:
            sig_type = sig.get('signal_type', '?')
            symbol = sig.get('symbol', '?')
            price = sig.get('price', 0)
            emoji = '\u2705' if sig_type == 'BUY' else (
                '\u274c' if sig_type == 'SELL' else '\u2139\ufe0f')
            lines.append(
                f' {emoji} {sig_type} {symbol} ${price:,.0f}'
            )

    return truncate_for_telegram('\n'.join(lines))


async def update_live_dashboard(application: Application,
                                cycle_data: dict):
    """Update (or create) the pinned live dashboard message."""
    global _dashboard_message_id, _last_dashboard_text

    cfg = app_config.get('settings', {}).get('live_dashboard', {})
    if not cfg.get('enabled', True):
        return

    chat_id = _get_chat_id()
    if not chat_id:
        return

    text = build_live_dashboard(cycle_data)

    # Skip edit if text unchanged
    if text == _last_dashboard_text:
        log.debug("Dashboard unchanged — skipping edit.")
        return

    # Try editing existing message
    if _dashboard_message_id:
        try:
            await application.bot.edit_message_text(
                chat_id=chat_id,
                message_id=_dashboard_message_id,
                text=text,
            )
            _last_dashboard_text = text
            log.debug("Dashboard updated in-place.")
            return
        except BadRequest as e:
            if 'message to edit not found' in str(e).lower() or \
               'message is not modified' in str(e).lower():
                log.info(f"Dashboard message lost ({e}), creating new one.")
                _dashboard_message_id = None
            else:
                log.warning(f"Dashboard edit failed: {e}")
                return
        except Exception as e:
            log.warning(f"Dashboard edit error: {e}")
            return

    # Send new message and pin it
    try:
        msg = await application.bot.send_message(
            chat_id=chat_id, text=text,
        )
        _dashboard_message_id = msg.message_id
        _last_dashboard_text = text
        save_bot_state('dashboard_message_id', str(msg.message_id))

        try:
            await application.bot.pin_chat_message(
                chat_id=chat_id,
                message_id=msg.message_id,
                disable_notification=True,
            )
        except Exception as e:
            log.warning(f"Could not pin dashboard: {e}")

        log.info(f"Dashboard created and pinned (msg_id={msg.message_id}).")
    except Exception as e:
        log.error(f"Dashboard send failed: {e}")


def build_daily_recap() -> str:
    """Build the end-of-day recap message. Returns empty string if no trades."""
    manual_trades = get_trades_closed_today()
    auto_trades = get_trades_closed_today('auto')

    if not manual_trades and not auto_trades:
        return ''

    now = datetime.now(timezone.utc)
    date_str = now.strftime('%b %d')

    lines = [f'\U0001f4cb DAILY RECAP ({date_str})', '']

    def _format_trade(t):
        symbol = t.get('symbol', '?')
        pnl = t.get('pnl', 0)
        entry = t.get('entry_price', 0)
        exit_p = t.get('exit_price', 0)
        pnl_pct = ((exit_p - entry) / entry * 100) if entry > 0 else 0
        reason = t.get('exit_reason', '')
        hold = _format_hold_duration(t.get('entry_timestamp'))
        emoji = '\U0001f7e2' if pnl >= 0 else '\U0001f534'
        reason_str = f' ({reason})' if reason else ''
        pnl_sign = '+' if pnl >= 0 else '-'
        return (
            f' {emoji} {symbol}  {pnl_pct:+.1f}%  {pnl_sign}${abs(pnl):,.2f}  '
            f'{hold}{reason_str}'
        )

    if manual_trades:
        lines.append('\u2500\u2500 Manual \u2500\u2500')
        total_pnl = 0
        for t in manual_trades:
            lines.append(_format_trade(t))
            total_pnl += t.get('pnl', 0)
        tp_sign = '+' if total_pnl >= 0 else '-'
        lines.append(f' Total: {tp_sign}${abs(total_pnl):,.2f}')

    if auto_trades:
        lines.append('')
        lines.append('\u2500\u2500 Auto-Bot \u2500\u2500')
        total_auto_pnl = 0
        for t in auto_trades:
            lines.append(_format_trade(t))
            total_auto_pnl += t.get('pnl', 0)
        ta_sign = '+' if total_auto_pnl >= 0 else '-'
        lines.append(f' Total: {ta_sign}${abs(total_auto_pnl):,.2f}')

    return truncate_for_telegram('\n'.join(lines))


async def send_daily_recap(application: Application):
    """Send end-of-day recap as a new (non-pinned) message."""
    chat_id = _get_chat_id()
    if not chat_id:
        return
    try:
        text = build_daily_recap()
        if not text:
            log.debug("No trades closed today — skipping daily recap.")
            return
        await application.bot.send_message(chat_id=chat_id, text=text)
        log.info("Daily recap sent.")
    except Exception as e:
        log.error(f"Daily recap send failed: {e}")


def reset_dashboard_state():
    """Reset module-level state (for testing)."""
    global _dashboard_message_id, _last_dashboard_text
    _dashboard_message_id = None
    _last_dashboard_text = None
