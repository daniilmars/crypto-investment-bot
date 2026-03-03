"""Position analyst — unified tri-state analyst (HOLD / INCREASE / SELL) for all positions.

Supports both the new position_analyst (Gemini investment analysis) and the
legacy position_monitor (Gemini health check) as fallback.
"""

from datetime import datetime, timedelta, timezone

from src.analysis.gemini_news_analyzer import analyze_position_health, analyze_position_investment
from src.analysis.news_velocity import compute_news_velocity
from src.config import app_config
from src.database import get_position_additions, get_recent_articles
from src.execution.binance_trader import add_to_position, get_account_balance, place_order
from src.logger import log
from src.notify.telegram_bot import (
    is_confirmation_required, send_position_health_alert,
    send_signal_for_confirmation, send_telegram_alert,
)
from src.orchestration import bot_state
from src.orchestration.pre_trade_gates import check_signal_cooldown


async def run_position_analyst(
    position: dict,
    current_price: float,
    market_price_data: dict,
    settings: dict,
    news_per_symbol: dict,
    *,
    trailing_stop_activation: float,
    asset_type: str = 'crypto',
) -> str | None:
    """Run the position analyst or legacy health monitor for an open position.

    Returns the recommendation ('hold', 'increase', 'sell', 'exit') or None.
    """
    symbol = position['symbol']
    entry_price = position['entry_price']
    pnl_pct = (current_price - entry_price) / entry_price
    order_id = position['order_id']

    analyst_cfg = settings.get('position_analyst', {})
    if analyst_cfg.get('enabled', False):
        return await _run_investment_analyst(
            position, symbol, current_price, entry_price, pnl_pct,
            order_id, market_price_data, analyst_cfg, settings,
            trailing_stop_activation, asset_type,
        )

    # Fallback: legacy position_monitor
    if settings.get('position_monitor', {}).get('enabled', False):
        return await _run_legacy_health_monitor(
            position, symbol, current_price, pnl_pct, order_id,
            market_price_data, settings, news_per_symbol,
            trailing_stop_activation, asset_type,
        )

    return None


async def _run_investment_analyst(
    position, symbol, current_price, entry_price, pnl_pct,
    order_id, market_price_data, analyst_cfg, settings,
    trailing_stop_activation, asset_type,
) -> str | None:
    """Run the Gemini investment analyst (tri-state: HOLD/INCREASE/SELL)."""
    now = datetime.now(timezone.utc)
    check_interval = analyst_cfg.get('check_interval_minutes', 30)
    last_check = bot_state.get_analyst_last_run(order_id)
    if last_check and (now - last_check).total_seconds() / 60 < check_interval:
        log.debug(f"[{symbol}] Skipping analyst — last run "
                  f"{(now - last_check).total_seconds() / 60:.0f}m ago")
        return None

    min_age_hours = analyst_cfg.get('min_position_age_hours', 2)
    entry_ts = position.get('entry_timestamp')
    if not entry_ts:
        return None

    try:
        entry_dt = _parse_entry_timestamp(entry_ts)
        age_hours = (now - entry_dt).total_seconds() / 3600
        if age_hours < min_age_hours:
            return None

        # 1. News velocity gate
        velocity = compute_news_velocity(symbol)
        if (velocity['articles_last_4h'] == 0
                and not velocity['breaking_detected']
                and velocity['sentiment_trend'] == 'stable'):
            log.debug(f"[{symbol}] Position analyst: no news activity, default HOLD")
            bot_state.set_analyst_last_run(order_id, now)
            return 'hold'

        # 2. Gather data
        recent_articles = await get_recent_articles(symbol, hours=48, limit=30)
        additions = await get_position_additions(order_id)

        tech_data = {
            'rsi': market_price_data.get('rsi'),
            'sma': market_price_data.get('sma'),
            'regime': 'unknown',
        }
        ts_info = _build_trailing_stop_info(
            order_id, pnl_pct, trailing_stop_activation)
        max_mult = analyst_cfg.get('max_position_multiplier', 3.0)

        # 3. Call Gemini analyst
        result = analyze_position_investment(
            position, current_price, recent_articles, tech_data,
            velocity, hours_held=age_hours,
            trailing_stop_info=ts_info,
            position_additions=additions,
            max_position_multiplier=max_mult,
        )
        bot_state.set_analyst_last_run(order_id, now)

        if not result:
            return None

        rec = result.get('recommendation', 'hold')
        confidence = result.get('confidence', 0)
        reasoning = result.get('reasoning', '')
        risk_level = result.get('risk_level', 'green')

        signal_cooldown_hours = app_config.get('settings', {}).get('signal_cooldown_hours', 4)

        if rec == 'increase' and confidence >= analyst_cfg.get('increase_confidence_threshold', 0.75):
            if await check_signal_cooldown(symbol, "INCREASE", signal_cooldown_hours):
                log.debug(f"[{symbol}] Skipping INCREASE: signal cooldown active.")
            else:
                await _handle_increase(
                    position, symbol, current_price, entry_price,
                    additions, result, reasoning, analyst_cfg, asset_type)
                bot_state.set_signal_cooldown(
                    symbol, "INCREASE",
                    datetime.now(timezone.utc) + timedelta(hours=signal_cooldown_hours))

        elif rec == 'sell' and confidence >= analyst_cfg.get('exit_confidence_threshold', 0.8):
            await _handle_analyst_sell(
                position, symbol, current_price, order_id,
                confidence, reasoning, asset_type)

        else:
            log.info(f"[{symbol}] Position analyst: {rec} "
                     f"(confidence={confidence:.2f}, risk={risk_level})")
            if risk_level in ('yellow', 'red'):
                await send_position_health_alert(
                    symbol, current_price, pnl_pct * 100, result, position)

        return rec

    except Exception as e:
        log.warning(f"Position analyst error for {symbol}: {e}")
        return None


async def _run_legacy_health_monitor(
    position, symbol, current_price, pnl_pct, order_id,
    market_price_data, settings, news_per_symbol,
    trailing_stop_activation, asset_type,
) -> str | None:
    """Run the legacy position health monitor (binary: hold/exit)."""
    pos_monitor_cfg = settings.get('position_monitor', {})
    now = datetime.now(timezone.utc)
    check_interval = pos_monitor_cfg.get('check_interval_minutes', 60)
    last_check = bot_state.get_analyst_last_run(order_id)
    if last_check and (now - last_check).total_seconds() / 60 < check_interval:
        return None

    min_age_hours = pos_monitor_cfg.get('min_position_age_hours', 4)
    entry_ts = position.get('entry_timestamp')
    if not entry_ts:
        return None

    try:
        entry_dt = _parse_entry_timestamp(entry_ts)
        age_hours = (now - entry_dt).total_seconds() / 3600
        if age_hours < min_age_hours:
            return None

        pos_headlines = []
        sym_news = news_per_symbol.get(symbol, {})
        if sym_news:
            pos_headlines.extend(sym_news.get('headlines', [])[:5])
        try:
            archived = await get_recent_articles(symbol, hours=24)
            pos_headlines.extend([a.get('title', '') for a in archived[:5]])
        except Exception:
            pass

        tech_data = {
            'rsi': market_price_data.get('rsi'),
            'sma': market_price_data.get('sma'),
            'regime': 'unknown',
        }
        ts_info = _build_trailing_stop_info(
            order_id, pnl_pct, trailing_stop_activation)

        health = analyze_position_health(
            position, current_price, pos_headlines, tech_data,
            hours_held=age_hours, trailing_stop_info=ts_info,
        )
        bot_state.set_analyst_last_run(order_id, now)

        if health:
            exit_threshold = pos_monitor_cfg.get('exit_confidence_threshold', 0.8)
            if (health.get('recommendation') == 'exit'
                    and health.get('confidence', 0) >= exit_threshold):
                await send_position_health_alert(
                    symbol, current_price, pnl_pct * 100, health, position)
            return health.get('recommendation')

        return None

    except Exception as e:
        log.warning(f"Position monitor error for {symbol}: {e}")
        return None


async def _handle_increase(
    position, symbol, current_price, entry_price,
    additions, result, reasoning, analyst_cfg, asset_type,
):
    """Handle an INCREASE recommendation from the analyst."""
    order_id = position['order_id']
    hint = result.get('increase_sizing_hint', 'small')
    hint_fractions = {'small': 0.25, 'medium': 0.50, 'large': 0.75}
    fraction = hint_fractions.get(hint, 0.25)
    original_value = entry_price * position['quantity']
    total_added = sum(
        a.get('addition_price', 0) * a.get('addition_quantity', 0)
        for a in additions
    )
    estimated_original = (original_value - total_added
                          if total_added < original_value else original_value)
    add_value = estimated_original * fraction
    add_qty = add_value / current_price if current_price > 0 else 0

    max_mult = analyst_cfg.get('max_position_multiplier', 3.0)
    new_total_value = original_value + add_value
    current_mult = new_total_value / estimated_original if estimated_original > 0 else 999
    if current_mult > max_mult:
        log.info(f"[{symbol}] INCREASE blocked: would exceed {max_mult}x cap")
        return

    balance = get_account_balance(asset_type=asset_type)
    available = balance.get('USDT', 0)
    if add_value > available:
        log.info(f"[{symbol}] INCREASE blocked: need ${add_value:.2f} "
                 f"but only ${available:.2f} available")
        return

    if is_confirmation_required("INCREASE"):
        await send_signal_for_confirmation({
            'signal': 'INCREASE', 'symbol': symbol,
            'current_price': current_price,
            'quantity': add_qty,
            'reason': reasoning,
            'asset_type': asset_type,
            'position': position,
        })
    else:
        add_to_position(order_id, symbol, add_qty, current_price,
                        reason=reasoning, asset_type=asset_type)


async def _handle_analyst_sell(
    position, symbol, current_price, order_id,
    confidence, reasoning, asset_type,
):
    """Handle a SELL recommendation from the analyst."""
    if is_confirmation_required("SELL"):
        await send_signal_for_confirmation({
            'signal': 'SELL', 'symbol': symbol,
            'current_price': current_price,
            'quantity': position['quantity'],
            'reason': reasoning,
            'asset_type': asset_type,
            'position': position,
        })
    else:
        order_kw = {}
        if asset_type == 'stock':
            order_kw['asset_type'] = 'stock'
        place_order(symbol, "SELL", position['quantity'], current_price,
                    existing_order_id=order_id, **order_kw)
        bot_state.clear_trailing_stop(order_id)
        bot_state.remove_analyst_last_run(order_id)
        alert = {
            "signal": "SELL", "symbol": symbol,
            "current_price": current_price,
            "reason": f"Position analyst sell ({confidence:.0%}): {reasoning}",
        }
        if asset_type == 'stock':
            alert['asset_type'] = 'stock'
        await send_telegram_alert(alert)


def _parse_entry_timestamp(entry_ts) -> datetime:
    """Parse entry_timestamp from position dict into tz-aware datetime."""
    if isinstance(entry_ts, str):
        dt = datetime.fromisoformat(entry_ts.replace('Z', '+00:00'))
    else:
        dt = entry_ts
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _build_trailing_stop_info(order_id: str, pnl_pct: float,
                               trailing_stop_activation: float) -> dict | None:
    """Build trailing stop info dict if peak price is tracked."""
    peak_price = bot_state.get_peak(order_id)
    if peak_price is None:
        return None
    return {
        'peak_price': peak_price,
        'trailing_active': pnl_pct >= trailing_stop_activation,
        'pnl_percentage': pnl_pct,
        'activation_threshold': trailing_stop_activation,
    }
