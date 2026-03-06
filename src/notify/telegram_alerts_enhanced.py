"""Enhanced Telegram alerts: morning briefing, portfolio digest, real-time market alerts."""

from datetime import datetime, timezone
from typing import Optional

from telegram.ext import Application

from src.logger import log
from src.config import app_config
from src.notify.formatting import (
    pnl_emoji, format_position_line, progress_bar, truncate_for_telegram,
)
from src.execution.binance_trader import get_open_positions, get_account_balance
from src.execution.circuit_breaker import get_daily_pnl, get_unrealized_pnl
from src.analysis.macro_regime import get_macro_regime
from src.analysis.event_calendar import get_upcoming_macro_events

# Module-level state for realtime alert change detection
_last_regime: Optional[str] = None
_last_vix: Optional[float] = None


def _get_chat_id() -> str:
    return app_config.get('notification_services', {}).get('telegram', {}).get('chat_id', '')


def _get_position_price(symbol: str) -> float:
    stock_watch_list = app_config.get('settings', {}).get(
        'stock_trading', {}).get('watch_list', [])
    if symbol in stock_watch_list:
        from src.collectors.alpha_vantage_data import get_stock_price
        price_data = get_stock_price(symbol)
        return price_data.get('price', 0) if price_data else 0
    else:
        from src.collectors.binance_data import get_current_price
        price_data = get_current_price(f"{symbol}USDT")
        return float(price_data.get('price', 0)) if price_data else 0


def _enrich_positions(positions: list) -> list:
    enriched = []
    for pos in positions:
        symbol = pos.get('symbol', '')
        entry = pos.get('entry_price', 0)
        current = _get_position_price(symbol) or entry
        qty = pos.get('quantity', 0)
        pnl = (current - entry) * qty
        pnl_pct = ((current - entry) / entry * 100) if entry > 0 else 0
        enriched.append({**pos, 'current_price': current, 'pnl': pnl, 'pnl_pct': pnl_pct})
    return enriched


# --- Morning Briefing ---

def build_morning_briefing() -> str:
    """Build the morning briefing message."""
    now = datetime.now(timezone.utc)
    date_str = now.strftime('%b %d')

    try:
        # Macro regime
        regime = get_macro_regime()
        regime_name = regime.get('regime', '?')
        score = regime.get('score', 0)
        indicators = regime.get('indicators', {})
        vix = indicators.get('vix', 0)

        # Portfolio
        crypto_bal = get_account_balance(asset_type='crypto')
        stock_bal = get_account_balance(asset_type='stock')
        total = crypto_bal.get('total_usd', 0) + stock_bal.get('total_usd', 0)

        # Daily PnL (yesterday's/overnight)
        daily_pnl = get_daily_pnl()
        pnl_sign = '+' if daily_pnl >= 0 else ''

        # Positions
        crypto_pos = get_open_positions(asset_type='crypto')
        stock_pos = get_open_positions(asset_type='stock')
        all_positions = _enrich_positions(crypto_pos + stock_pos)

        # Events
        events = get_upcoming_macro_events(days_ahead=3)
        today_events = [e for e in events if e.get('hours_until', 999) <= 24]

        lines = [
            f"*MORNING BRIEFING ({date_str})*\n",
            f"*Regime:* {regime_name} ({score:+.1f}) | VIX {vix:.1f}",
        ]

        # S&P and BTC from indicators if available
        sp500 = indicators.get('sp500_change_pct')
        btc_price = indicators.get('btc_price')
        if sp500 is not None:
            lines.append(f"S&P {sp500:+.1f}%", )
        if btc_price is not None:
            lines.append(f"BTC ${btc_price:,.0f}")

        lines.append(f"\n*Portfolio:* ${total:,.0f} ({pnl_sign}${daily_pnl:,.0f} overnight)")
        lines.append(f"  {len(crypto_pos)} crypto | {len(stock_pos)} stocks open")

        if today_events:
            lines.append("\n*Events Today:*")
            for evt in today_events[:5]:
                lines.append(f"  {evt.get('event_type', '?')}: {evt.get('hours_until', 0):.0f}h")

        # Top positions by absolute PnL
        if all_positions:
            top = sorted(all_positions, key=lambda p: abs(p.get('pnl_pct', 0)), reverse=True)[:5]
            lines.append("\n*Top Positions:*")
            for pos in top:
                emoji = pnl_emoji(pos['pnl_pct'])
                lines.append(
                    f"  {emoji} {pos['symbol']} {pos['pnl_pct']:+.1f}%"
                )

        return truncate_for_telegram('\n'.join(lines))
    except Exception as e:
        log.error(f"Error building morning briefing: {e}", exc_info=True)
        return f"Morning briefing error: {e}"


async def send_morning_briefing(application: Application):
    """Send the morning briefing via Telegram."""
    chat_id = _get_chat_id()
    if not chat_id:
        return
    try:
        message = build_morning_briefing()

        # Optional AI summary
        cfg = app_config.get('settings', {}).get('telegram_enhancements', {}).get(
            'morning_briefing', {})
        if cfg.get('include_ai_summary', False):
            try:
                from src.analysis.gemini_summary import generate_market_summary
                from src.database import get_price_history_since, get_last_signal
                price_history = get_price_history_since(hours_ago=24)
                last_signal = get_last_signal()
                ai_summary = generate_market_summary(price_history, last_signal)
                if ai_summary:
                    message += f"\n\n_AI Summary:_ {ai_summary[:500]}"
            except Exception as e:
                log.warning(f"AI summary failed: {e}")

        await application.bot.send_message(
            chat_id=chat_id, text=message, parse_mode='Markdown'
        )
        log.info("Morning briefing sent.")
    except Exception as e:
        log.error(f"Error sending morning briefing: {e}", exc_info=True)


# --- Portfolio Digest ---

def build_portfolio_digest() -> str:
    """Build the periodic portfolio digest message."""
    try:
        crypto_bal = get_account_balance(asset_type='crypto')
        stock_bal = get_account_balance(asset_type='stock')
        total = crypto_bal.get('total_usd', 0) + stock_bal.get('total_usd', 0)
        daily_pnl = get_daily_pnl()
        pnl_sign = '+' if daily_pnl >= 0 else ''

        crypto_pos = _enrich_positions(get_open_positions(asset_type='crypto'))
        stock_pos = _enrich_positions(get_open_positions(asset_type='stock'))
        all_pos = crypto_pos + stock_pos

        # Slot usage
        settings = app_config.get('settings', {})
        max_crypto = settings.get('max_open_positions', 5)
        max_stocks = settings.get('stock_trading', {}).get('max_positions', 8)

        # Unrealized PnL
        crypto_unrealized = sum(p.get('pnl', 0) for p in crypto_pos)
        stock_unrealized = sum(p.get('pnl', 0) for p in stock_pos)

        # Best/worst
        sorted_pos = sorted(all_pos, key=lambda p: p.get('pnl_pct', 0), reverse=True)

        # Regime + CB
        regime = get_macro_regime()
        from src.execution.circuit_breaker import get_circuit_breaker_status
        cb = get_circuit_breaker_status()
        cb_label = "HALT" if cb.get('in_cooldown') else "OK"

        lines = [
            "*PORTFOLIO (4h update)*\n",
            f"*Value:* ${total:,.0f} | *Day:* {pnl_sign}${daily_pnl:,.0f}\n",
        ]

        if sorted_pos:
            best = sorted_pos[:2]
            worst = sorted_pos[-2:] if len(sorted_pos) > 2 else []
            best_str = ' | '.join(f"{p['symbol']} {p['pnl_pct']:+.1f}%" for p in best)
            lines.append(f"*Best:*  {best_str}")
            if worst:
                worst_str = ' | '.join(f"{p['symbol']} {p['pnl_pct']:+.1f}%" for p in worst)
                lines.append(f"*Worst:* {worst_str}")

        crypto_bar = progress_bar(len(crypto_pos), max_crypto, width=5)
        stock_bar = progress_bar(len(stock_pos), max_stocks, width=5)
        lines.append(
            f"\n*Slots:* {len(crypto_pos)}/{max_crypto} crypto {crypto_bar}"
            f" | {len(stock_pos)}/{max_stocks} stocks {stock_bar}"
        )
        lines.append(f"*CB:* {cb_label} | *Regime:* {regime.get('regime', '?')}")
        lines.append(
            f"*Unrealized:* ${crypto_unrealized:+,.0f} crypto "
            f"${stock_unrealized:+,.0f} stocks"
        )

        return truncate_for_telegram('\n'.join(lines))
    except Exception as e:
        log.error(f"Error building portfolio digest: {e}", exc_info=True)
        return f"Portfolio digest error: {e}"


async def send_portfolio_digest(application: Application):
    """Send the portfolio digest via Telegram."""
    chat_id = _get_chat_id()
    if not chat_id:
        return
    try:
        message = build_portfolio_digest()
        await application.bot.send_message(
            chat_id=chat_id, text=message, parse_mode='Markdown'
        )
        log.info("Portfolio digest sent.")
    except Exception as e:
        log.error(f"Error sending portfolio digest: {e}", exc_info=True)


# --- Real-time Market Alerts ---

def check_realtime_alerts(macro_regime_result: dict) -> list[str]:
    """Check for regime changes and VIX spikes. Returns list of alert messages.

    Called from run_bot_cycle() after macro regime fetch.
    """
    global _last_regime, _last_vix

    cfg = app_config.get('settings', {}).get('telegram_enhancements', {}).get(
        'realtime_alerts', {})
    if not cfg.get('enabled', True):
        return []

    alerts = []
    current_regime = macro_regime_result.get('regime', '')
    indicators = macro_regime_result.get('indicators', {})
    current_vix = indicators.get('vix')

    # Regime change detection
    if cfg.get('regime_change_alert', True) and _last_regime is not None:
        if current_regime != _last_regime:
            old_mult = 1.0  # We don't have old multiplier, so just show new
            new_mult = macro_regime_result.get('position_size_multiplier', 1.0)
            new_score = macro_regime_result.get('score', 0)
            suppress = macro_regime_result.get('suppress_buys', False)
            signals = macro_regime_result.get('signals', {})

            trigger_parts = []
            for key, val in signals.items():
                trigger_parts.append(f"{key}: {val}")
            trigger_str = ', '.join(trigger_parts[:3]) if trigger_parts else 'multiple factors'

            alert = (
                f"*REGIME CHANGE: {_last_regime} -> {current_regime}*\n\n"
                f"*Score:* {new_score:+.1f}\n"
                f"*Multiplier:* {new_mult:.1f}x\n"
                f"*Suppress BUYs:* {'Yes' if suppress else 'No'}\n"
                f"*Trigger:* {trigger_str}"
            )
            alerts.append(alert)

    # VIX spike detection
    vix_threshold = cfg.get('vix_spike_threshold', 3.0)
    if current_vix is not None and _last_vix is not None:
        vix_change = current_vix - _last_vix
        if abs(vix_change) >= vix_threshold:
            direction = "increasing" if vix_change > 0 else "decreasing"
            advice = "Consider reducing exposure" if vix_change > 0 else "Volatility easing"
            alert = (
                f"*VIX SPIKE: {_last_vix:.1f} -> {current_vix:.1f} ({vix_change:+.1f})*\n\n"
                f"Market fear {direction}\n"
                f"{advice}"
            )
            alerts.append(alert)

    # Update state
    _last_regime = current_regime
    if current_vix is not None:
        _last_vix = current_vix

    return alerts


async def send_realtime_alerts(application: Application, alerts: list[str]):
    """Send realtime alert messages via Telegram."""
    chat_id = _get_chat_id()
    if not chat_id or not alerts:
        return
    for alert in alerts:
        try:
            await application.bot.send_message(
                chat_id=chat_id, text=alert, parse_mode='Markdown'
            )
        except Exception as e:
            log.error(f"Error sending realtime alert: {e}")


def reset_alert_state():
    """Reset module-level state (useful for testing)."""
    global _last_regime, _last_vix
    _last_regime = None
    _last_vix = None
