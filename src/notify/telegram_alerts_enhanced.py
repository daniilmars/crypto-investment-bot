"""Enhanced Telegram alerts: real-time market alerts (regime change, VIX spike)."""

from typing import Optional

from telegram.ext import Application

from src.logger import log
from src.config import app_config

# Module-level state for realtime alert change detection
_last_regime: Optional[str] = None
_last_vix: Optional[float] = None


def _get_chat_id() -> str:
    return app_config.get('notification_services', {}).get('telegram', {}).get('chat_id', '')


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
    raw_vix = indicators.get('vix')
    current_vix = raw_vix.get('current') if isinstance(raw_vix, dict) else raw_vix

    # Regime change detection
    if cfg.get('regime_change_alert', True) and _last_regime is not None:
        if current_regime != _last_regime:
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
