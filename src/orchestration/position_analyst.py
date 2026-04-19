"""Position analyst — unified tri-state analyst (HOLD / INCREASE / SELL) for all positions.

Supports both the new position_analyst (Gemini investment analysis) and the
legacy position_monitor (Gemini health check) as fallback.
"""

from datetime import datetime, timedelta, timezone

from src.analysis.gemini_news_analyzer import (
    analyze_position_health, analyze_position_investment, analyze_position_quick,
)
from src.analysis.news_velocity import compute_news_velocity
from src.config import app_config
from src.database import get_position_additions, get_recent_articles
from src.execution.binance_trader import add_to_position, get_account_balance, place_order
from src.logger import log
from src.notify.telegram_bot import (
    send_position_health_alert, send_telegram_alert,
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
    trading_strategy: str = 'manual',
) -> str | None:
    """Run the position analyst or legacy health monitor for an open position.

    Returns the recommendation ('hold', 'increase', 'sell', 'exit') or None.
    """
    symbol = position['symbol']
    entry_price = position['entry_price']
    pnl_pct = (current_price - entry_price) / entry_price
    order_id = position['order_id']
    is_auto = trading_strategy != 'manual'

    analyst_cfg = settings.get('position_analyst', {})
    if analyst_cfg.get('enabled', False):
        # Run Flash analyst (frequent, cheap) — exit-only
        flash_interval = analyst_cfg.get('quick_check_interval_minutes')
        if flash_interval:
            flash_result = await _run_flash_analyst(
                position, symbol, current_price, entry_price, pnl_pct,
                order_id, market_price_data, analyst_cfg, settings,
                trailing_stop_activation, asset_type, is_auto,
                trading_strategy=trading_strategy,
            )
            if flash_result == 'exit':
                return 'sell'

        # Run Pro analyst (infrequent, deep) — on its own interval
        return await _run_investment_analyst(
            position, symbol, current_price, entry_price, pnl_pct,
            order_id, market_price_data, analyst_cfg, settings,
            trailing_stop_activation, asset_type, is_auto,
            trading_strategy=trading_strategy,
        )

    # Fallback: legacy position_monitor
    if settings.get('position_monitor', {}).get('enabled', False):
        return await _run_legacy_health_monitor(
            position, symbol, current_price, pnl_pct, order_id,
            market_price_data, settings, news_per_symbol,
            trailing_stop_activation, asset_type, is_auto,
        )

    return None


async def _run_investment_analyst(
    position, symbol, current_price, entry_price, pnl_pct,
    order_id, market_price_data, analyst_cfg, settings,
    trailing_stop_activation, asset_type, is_auto=False,
    trading_strategy='manual',
) -> str | None:
    """Run the Gemini investment analyst (tri-state: HOLD/INCREASE/SELL)."""
    now = datetime.now(timezone.utc)
    check_interval = analyst_cfg.get('check_interval_minutes', 30)
    _get_last_run = bot_state.get_auto_analyst_last_run if is_auto else bot_state.get_analyst_last_run
    _set_last_run = bot_state.set_auto_analyst_last_run if is_auto else bot_state.set_analyst_last_run
    last_check = _get_last_run(order_id)
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
            _set_last_run(order_id, now)
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
            order_id, pnl_pct, trailing_stop_activation, is_auto=is_auto)
        max_mult = analyst_cfg.get('max_position_multiplier', 3.0)

        # 3. Call Gemini analyst
        result = analyze_position_investment(
            position, current_price, recent_articles, tech_data,
            velocity, hours_held=age_hours,
            trailing_stop_info=ts_info,
            position_additions=additions,
            max_position_multiplier=max_mult,
            strategy_type=position.get('strategy_type'),
            trade_reason=position.get('trade_reason'),
        )
        _set_last_run(order_id, now)

        if not result:
            return None

        rec = result.get('recommendation', 'hold').strip().lower()
        confidence = max(0.0, min(1.0, float(result.get('confidence', 0))))
        reasoning = result.get('reasoning', '')
        risk_level = result.get('risk_level', 'green')

        # Validate recommendation enum
        if rec not in ('hold', 'increase', 'sell'):
            log.warning(f"[{symbol}] Invalid analyst recommendation: {rec!r}, defaulting to hold")
            rec = 'hold'

        signal_cooldown_hours = app_config.get('settings', {}).get('signal_cooldown_hours', 4)

        # Strategic positions require higher exit confidence
        strategy_type = position.get('strategy_type')
        exit_threshold = analyst_cfg.get('exit_confidence_threshold', 0.8)
        if strategy_type:
            exit_threshold = max(exit_threshold, 0.85)

        if rec == 'increase' and confidence >= analyst_cfg.get('increase_confidence_threshold', 0.75):
            if await check_signal_cooldown(symbol, "INCREASE", signal_cooldown_hours, is_auto=is_auto):
                log.debug(f"[{symbol}] Skipping INCREASE: signal cooldown active.")
            else:
                await _handle_increase(
                    position, symbol, current_price, entry_price,
                    additions, result, reasoning, analyst_cfg, asset_type, is_auto)
                _set_cd = bot_state.set_auto_signal_cooldown if is_auto else bot_state.set_signal_cooldown
                _set_cd(symbol, "INCREASE",
                        datetime.now(timezone.utc) + timedelta(hours=signal_cooldown_hours))

        elif rec == 'sell' and confidence >= exit_threshold:
            await _handle_analyst_sell(
                position, symbol, current_price, order_id,
                confidence, reasoning, asset_type,
                trading_strategy=trading_strategy)

        else:
            log.info(f"[{symbol}] Position analyst: {rec} "
                     f"(confidence={confidence:.2f}, risk={risk_level})")
            if risk_level == 'red':
                await send_position_health_alert(
                    symbol, current_price, pnl_pct * 100, result, position)

        return rec

    except Exception as e:
        log.warning(f"Position analyst error for {symbol}: {e}")
        return None


async def _run_flash_analyst(
    position, symbol, current_price, entry_price, pnl_pct,
    order_id, market_price_data, analyst_cfg, settings,
    trailing_stop_activation, asset_type, is_auto=False,
    trading_strategy='manual',
) -> str | None:
    """Run the Flash analyst (exit-only, every 4h, uses gemini-2.5-flash)."""
    now = datetime.now(timezone.utc)
    check_interval = analyst_cfg.get('quick_check_interval_minutes', 240)
    _get_last_run = bot_state.get_auto_flash_analyst_last_run if is_auto else bot_state.get_flash_analyst_last_run
    _set_last_run = bot_state.set_auto_flash_analyst_last_run if is_auto else bot_state.set_flash_analyst_last_run
    last_check = _get_last_run(order_id)
    if last_check and (now - last_check).total_seconds() / 60 < check_interval:
        log.debug(f"[{symbol}] Skipping Flash analyst — last run "
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

        # News velocity gate (same as Pro analyst)
        velocity = compute_news_velocity(symbol)
        if (velocity['articles_last_4h'] == 0
                and not velocity['breaking_detected']
                and velocity['sentiment_trend'] == 'stable'):
            log.debug(f"[{symbol}] Flash analyst: no news activity, default hold")
            _set_last_run(order_id, now)
            return 'hold'

        # Gather data
        recent_articles = await get_recent_articles(symbol, hours=4, limit=10)
        tech_data = {
            'rsi': market_price_data.get('rsi'),
            'sma': market_price_data.get('sma'),
            'regime': 'unknown',
        }
        ts_info = _build_trailing_stop_info(
            order_id, pnl_pct, trailing_stop_activation, is_auto=is_auto)

        # Call Flash analyst
        result = analyze_position_quick(
            position, current_price, recent_articles, tech_data,
            velocity, hours_held=age_hours,
            trailing_stop_info=ts_info,
            strategy_type=position.get('strategy_type'),
            trade_reason=position.get('trade_reason'),
        )
        _set_last_run(order_id, now)

        if not result:
            return None

        action = result.get('action', 'hold').strip().lower()
        confidence = max(0.0, min(1.0, float(result.get('confidence', 0))))

        exit_threshold = analyst_cfg.get('exit_confidence_threshold', 0.8)
        # Strategic positions require higher exit confidence
        if position.get('strategy_type'):
            exit_threshold = max(exit_threshold, 0.85)

        if action == 'exit' and confidence >= exit_threshold:
            reasoning = result.get('reason', 'Flash analyst exit signal')
            await _handle_analyst_sell(
                position, symbol, current_price, order_id,
                confidence, f"Flash: {reasoning}",
                asset_type, trading_strategy=trading_strategy,
                exit_reason='flash_analyst_exit',
            )
            return 'exit'

        return action

    except Exception as e:
        log.warning(f"Flash analyst error for {symbol}: {e}")
        return None


async def _run_legacy_health_monitor(
    position, symbol, current_price, pnl_pct, order_id,
    market_price_data, settings, news_per_symbol,
    trailing_stop_activation, asset_type, is_auto=False,
) -> str | None:
    """Run the legacy position health monitor (binary: hold/exit)."""
    pos_monitor_cfg = settings.get('position_monitor', {})
    now = datetime.now(timezone.utc)
    check_interval = pos_monitor_cfg.get('check_interval_minutes', 60)
    _get_last_run = bot_state.get_auto_analyst_last_run if is_auto else bot_state.get_analyst_last_run
    last_check = _get_last_run(order_id)
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
        except Exception as e:
            log.debug(f"Failed to fetch recent articles for {symbol}: {e}")

        tech_data = {
            'rsi': market_price_data.get('rsi'),
            'sma': market_price_data.get('sma'),
            'regime': 'unknown',
        }
        ts_info = _build_trailing_stop_info(
            order_id, pnl_pct, trailing_stop_activation, is_auto=is_auto)

        health = analyze_position_health(
            position, current_price, pos_headlines, tech_data,
            hours_held=age_hours, trailing_stop_info=ts_info,
        )
        _set_last_run = bot_state.set_auto_analyst_last_run if is_auto else bot_state.set_analyst_last_run
        _set_last_run(order_id, now)

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
    additions, result, reasoning, analyst_cfg, asset_type, is_auto=False,
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
        log.info(f"[{symbol}] INCREASE blocked: would exceed {max_mult}x cap "
                 f"({current_mult:.1f}x → max {max_mult:.1f}x)")
        if not is_auto:
            await send_telegram_alert({
                'signal': 'INFO', 'symbol': symbol,
                'current_price': current_price,
                'reason': f"Analyst recommended INCREASE but position at "
                          f"{current_mult:.1f}x (max {max_mult:.1f}x). {reasoning}",
            })
        return

    trading_strategy = 'auto' if is_auto else 'manual'
    balance = get_account_balance(asset_type=asset_type, trading_strategy=trading_strategy)
    available = balance.get('USDT', 0)
    if add_value > available:
        log.info(f"[{symbol}] INCREASE blocked: need ${add_value:.2f} "
                 f"but only ${available:.2f} available")
        if not is_auto:
            await send_telegram_alert({
                'signal': 'INFO', 'symbol': symbol,
                'current_price': current_price,
                'reason': f"Analyst recommended INCREASE (+${add_value:.0f}) "
                          f"but only ${available:.0f} available.",
            })
        return

    add_to_position(order_id, symbol, add_qty, current_price,
                    reason=reasoning, asset_type=asset_type,
                    trading_strategy=trading_strategy)
    # Reset trailing-stop peak after successful INCREASE so the trailing
    # logic re-baselines from the new average entry price. Without this,
    # the next 2% dip from the OLD peak fires trailing on the whole
    # enlarged position — undoing the increased conviction.
    bot_state.strategy_clear_trailing_stop(order_id, trading_strategy)
    log.info(f"[{symbol}] Trailing peak cleared after INCREASE — will rearm "
             f"at next +activation% over new avg entry")


async def _handle_analyst_sell(
    position, symbol, current_price, order_id,
    confidence, reasoning, asset_type,
    trading_strategy='manual',
    exit_reason='analyst_exit',
):
    """Handle a SELL recommendation from the analyst."""
    is_auto = trading_strategy != 'manual'
    order_kw = {}
    if asset_type == 'stock':
        order_kw['asset_type'] = 'stock'
    place_order(symbol, "SELL", position['quantity'], current_price,
                existing_order_id=order_id, exit_reason=exit_reason,
                trading_strategy=trading_strategy, **order_kw)
    qty = position['quantity']
    entry_price = position['entry_price']
    pnl_pct = (current_price - entry_price) / entry_price
    pnl_dollar = (current_price - entry_price) * qty
    bot_state.strategy_record_trade_outcome(trading_strategy, is_win=(pnl_pct > 0))

    bot_state.strategy_clear_trailing_stop(order_id, trading_strategy)
    if is_auto:
        bot_state.remove_auto_analyst_last_run(order_id)
        bot_state.remove_auto_flash_analyst_last_run(order_id)
    else:
        bot_state.remove_analyst_last_run(order_id)
        bot_state.remove_flash_analyst_last_run(order_id)

    # Short BUY cooldown to prevent immediate re-entry on the same signal
    # the analyst just acted on. Same pattern as trailing_stop / time_stop.
    signal_cooldown_hours = app_config.get('settings', {}).get(
        'signal_cooldown_hours', 4)
    expires = datetime.now(timezone.utc) + timedelta(hours=signal_cooldown_hours)
    if is_auto:
        bot_state.set_auto_signal_cooldown(symbol, "BUY", expires)
    else:
        bot_state.set_signal_cooldown(symbol, "BUY", expires)

    try:
        from src.notify.telegram_periodic_summary import send_trade_alert
        # Compute hold duration
        hold = ""
        entry_ts = position.get('entry_timestamp')
        if entry_ts:
            try:
                dt = _parse_entry_timestamp(entry_ts)
                delta = datetime.now(timezone.utc) - dt
                days = delta.days
                hours = delta.seconds // 3600
                hold = f"{days}d {hours}h" if days > 0 else f"{hours}h"
            except Exception:
                pass
        await send_trade_alert(
            action="SELL", symbol=symbol,
            trading_strategy=trading_strategy,
            entry_price=entry_price, exit_price=current_price,
            quantity=qty, pnl=pnl_dollar, pnl_pct=pnl_pct * 100,
            hold_duration=hold, exit_reason=exit_reason,
            reason=f"Analyst {confidence:.0%}: {reasoning}")
    except Exception:
        pass


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
                               trailing_stop_activation: float,
                               is_auto: bool = False) -> dict | None:
    """Build trailing stop info dict if peak price is tracked."""
    peak_price = bot_state.get_auto_peak(order_id) if is_auto else bot_state.get_peak(order_id)
    if peak_price is None:
        return None
    return {
        'peak_price': peak_price,
        'trailing_active': pnl_pct >= trailing_stop_activation,
        'pnl_percentage': pnl_pct,
        'activation_threshold': trailing_stop_activation,
    }
