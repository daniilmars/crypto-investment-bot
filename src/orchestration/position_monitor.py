"""Position monitor — unified SL/TP/trailing stop for all position types.

Handles: crypto manual, crypto auto, stock manual, stock auto.
Strategic trades use category-specific catastrophic SL only (no TP, no trailing).
Never blocks exits (these are protective exits).
"""

import asyncio
from datetime import datetime, timedelta, timezone

from src.config import app_config
from src.database import save_stoploss_cooldown
from src.execution.binance_trader import place_order
from src.logger import log
from src.notify.telegram_bot import send_telegram_alert
from src.orchestration import bot_state
from src.orchestration.time_stop import should_time_stop
from src.analysis.feedback_loop import process_closed_trade
from src.analysis.recent_assessment import get_recent_bearish_assessment


def _get_strategic_overrides(position: dict) -> dict | None:
    """If position has a strategy_type, return overridden risk params.

    Strategic positions use only a catastrophic stop-loss.
    No take-profit, no trailing stop.
    """
    strategy_type = position.get('strategy_type')
    if not strategy_type:
        return None

    categories = app_config.get('settings', {}).get('strategic_categories', {})
    category = categories.get(strategy_type)
    if not category:
        log.warning(f"Unknown strategy_type '{strategy_type}' — using normal SL/TP")
        return None

    return {
        'stop_loss_pct': category['catastrophic_sl'],
        'take_profit_pct': 999.0,
        'trailing_stop_enabled': False,
        'label': category.get('label', strategy_type),
    }


async def monitor_position(
    position: dict,
    current_price: float,
    *,
    stop_loss_pct: float,
    take_profit_pct: float,
    trailing_stop_enabled: bool,
    trailing_stop_activation: float,
    trailing_stop_distance: float,
    stoploss_cooldown_hours: int = 0,
    asset_type: str = 'crypto',
    trading_strategy: str = 'manual',
    mode_label: str = 'PAPER',
    time_stop_cfg: dict | None = None,
) -> str:
    """Monitor a single open position for SL/TP/trailing stop triggers.

    Returns 'trailing_stop', 'stop_loss', 'take_profit', 'time_stop', or 'none'.
    """
    symbol = position['symbol']
    entry_price = position['entry_price']
    pnl_pct = (current_price - entry_price) / entry_price
    order_id = position['order_id']
    qty = position['quantity']
    is_auto = trading_strategy == 'auto'

    # Override risk params for strategic trades
    strategic = _get_strategic_overrides(position)
    if strategic:
        stop_loss_pct = strategic['stop_loss_pct']
        take_profit_pct = strategic['take_profit_pct']
        trailing_stop_enabled = strategic['trailing_stop_enabled']

    # Update trailing stop peak tracker
    if is_auto:
        peak_price = bot_state.auto_update_trailing_stop(order_id, current_price)
    else:
        peak_price = bot_state.update_trailing_stop(order_id, current_price)
    drawdown = (peak_price - current_price) / peak_price if peak_price > 0 else 0

    # Build order kwargs
    order_kw = {}
    if is_auto:
        order_kw['trading_strategy'] = 'auto'
    if asset_type == 'stock':
        order_kw['asset_type'] = 'stock'

    # --- Trailing stop (disabled for strategic trades) ---
    if trailing_stop_enabled and pnl_pct >= trailing_stop_activation:
        if drawdown >= trailing_stop_distance:
            # If a recent bearish Gemini assessment exists for this symbol,
            # the trailing exit is "concur with analyst" rather than pure
            # noise — tag it so attribution can distinguish the two.
            recent_bearish = await asyncio.to_thread(
                get_recent_bearish_assessment, symbol, 8)
            exit_reason_tag = 'trailing_stop_analyst_concur' if recent_bearish else 'trailing_stop'
            log.info(f"[{mode_label}] Trailing stop triggered for {symbol} "
                     f"(tag={exit_reason_tag}). Peak: ${peak_price:,.2f}, "
                     f"Current: ${current_price:,.2f}")
            try:
                reasoning = (
                    f"Trailed {trailing_stop_distance:.1%} from peak "
                    f"${peak_price:,.4f} (current ${current_price:,.4f})"
                )
                if recent_bearish and recent_bearish.get('reasoning'):
                    reasoning += (
                        f"; concurring bearish assessment: "
                        f"{recent_bearish.get('reasoning')}"
                    )
            except Exception:
                reasoning = None
            place_order(symbol, "SELL", qty, current_price,
                        existing_order_id=order_id, exit_reason=exit_reason_tag,
                        exit_reasoning=reasoning, **order_kw)
            _cleanup_position_state(order_id, is_auto)
            if asset_type == 'stock':
                from src.execution.stock_trader import _is_same_day_trade, _record_day_trade
                if _is_same_day_trade(position):
                    _record_day_trade()
                    log.info(f"PDT: recorded day trade for {symbol}")
            _resolve_trade_attribution(order_id, pnl_pct, entry_price,
                                       current_price, exit_reason_tag)
            bot_state.strategy_record_trade_outcome(trading_strategy, is_win=(pnl_pct > 0))
            # Short BUY cooldown to prevent immediate re-entry on the same
            # signal that drove the position down past the trailing threshold.
            signal_cooldown_hours = app_config.get('settings', {}).get(
                'signal_cooldown_hours', 4)
            expires = datetime.now(timezone.utc) + timedelta(hours=signal_cooldown_hours)
            if is_auto:
                bot_state.set_auto_signal_cooldown(symbol, "BUY", expires)
            else:
                bot_state.set_signal_cooldown(symbol, "BUY", expires)
            await _send_trade_exit_alert(
                symbol, trading_strategy, entry_price, current_price,
                qty, pnl_pct, position.get('entry_timestamp'), exit_reason_tag)
            # Return value stays 'trailing_stop' — cycle_runner only checks
            # `result != 'none'`, and downstream telemetry consumes the tag
            # via place_order's exit_reason kwarg + the alert payload.
            return 'trailing_stop'

    # --- Stop-loss (catastrophic SL for strategic trades) ---
    if pnl_pct <= -stop_loss_pct:
        sl_label = f"Catastrophic SL ({strategic['label']})" if strategic else "Stop-loss"
        log.info(f"[{mode_label}] {sl_label} hit for {symbol}. Closing position.")
        try:
            sl_reasoning = (
                f"SL: price ${current_price:,.4f} crossed "
                f"-{stop_loss_pct:.1%} from entry ${entry_price:,.4f}"
            )
        except Exception:
            sl_reasoning = None
        place_order(symbol, "SELL", qty, current_price,
                    existing_order_id=order_id, exit_reason='stop_loss',
                    exit_reasoning=sl_reasoning, **order_kw)
        _cleanup_position_state(order_id, is_auto)
        if asset_type == 'stock':
            from src.execution.stock_trader import _is_same_day_trade, _record_day_trade
            if _is_same_day_trade(position):
                _record_day_trade()
                log.info(f"PDT: recorded day trade for {symbol}")
        _resolve_trade_attribution(order_id, pnl_pct, entry_price,
                                   current_price, 'stop_loss')
        if stoploss_cooldown_hours > 0:
            expires_at = datetime.now(timezone.utc) + timedelta(hours=stoploss_cooldown_hours)
            if is_auto:
                bot_state.set_auto_stoploss_cooldown(symbol, expires_at)
            else:
                bot_state.set_stoploss_cooldown(symbol, expires_at)
                await save_stoploss_cooldown(symbol, expires_at)
                log.info(f"[{symbol}] Stop-loss cooldown set for {stoploss_cooldown_hours}h")
        bot_state.strategy_record_trade_outcome(trading_strategy, is_win=False)
        await _send_trade_exit_alert(
            symbol, trading_strategy, entry_price, current_price,
            qty, pnl_pct, position.get('entry_timestamp'), 'stop_loss')
        return 'stop_loss'

    # --- Take profit (disabled for strategic trades via 999% threshold) ---
    if pnl_pct >= take_profit_pct:
        log.info(f"[{mode_label}] Take-profit hit for {symbol}. Closing position.")
        try:
            tp_reasoning = (
                f"TP: price ${current_price:,.4f} hit "
                f"+{take_profit_pct:.1%} from entry ${entry_price:,.4f}"
            )
        except Exception:
            tp_reasoning = None
        place_order(symbol, "SELL", qty, current_price,
                    existing_order_id=order_id, exit_reason='take_profit',
                    exit_reasoning=tp_reasoning, **order_kw)
        _cleanup_position_state(order_id, is_auto)
        if asset_type == 'stock':
            from src.execution.stock_trader import _is_same_day_trade, _record_day_trade
            if _is_same_day_trade(position):
                _record_day_trade()
                log.info(f"PDT: recorded day trade for {symbol}")
        _resolve_trade_attribution(order_id, pnl_pct, entry_price,
                                   current_price, 'take_profit')
        bot_state.strategy_record_trade_outcome(trading_strategy, is_win=True)
        await _send_trade_exit_alert(
            symbol, trading_strategy, entry_price, current_price,
            qty, pnl_pct, position.get('entry_timestamp'), 'take_profit')
        return 'take_profit'

    # --- Time-stop (slow-drift positions; fires last, after all protective exits) ---
    fire_time_stop, ts_reason = should_time_stop(
        position, current_price, time_stop_cfg, trading_strategy)
    if fire_time_stop:
        dry_run = bool(time_stop_cfg.get('dry_run', True))
        if dry_run:
            log.info(
                f"[{mode_label}] TIME-STOP CANDIDATE (dry_run) {symbol} "
                f"{trading_strategy}: {ts_reason}")
            return 'none'

        log.info(f"[{mode_label}] Time-stop triggered for {symbol}. {ts_reason}")
        place_order(symbol, "SELL", qty, current_price,
                    existing_order_id=order_id, exit_reason='time_stop',
                    exit_reasoning=(ts_reason or None), **order_kw)
        _cleanup_position_state(order_id, is_auto)
        if asset_type == 'stock':
            from src.execution.stock_trader import _is_same_day_trade, _record_day_trade
            if _is_same_day_trade(position):
                _record_day_trade()
                log.info(f"PDT: recorded day trade for {symbol}")
        _resolve_trade_attribution(order_id, pnl_pct, entry_price,
                                   current_price, 'time_stop')
        # Short BUY cooldown to prevent immediate re-entry on the same signal.
        signal_cooldown_hours = app_config.get('settings', {}).get(
            'signal_cooldown_hours', 4)
        expires = datetime.now(timezone.utc) + timedelta(hours=signal_cooldown_hours)
        if is_auto:
            bot_state.set_auto_signal_cooldown(symbol, "BUY", expires)
        else:
            bot_state.set_signal_cooldown(symbol, "BUY", expires)
        bot_state.strategy_record_trade_outcome(
            trading_strategy, is_win=(pnl_pct > 0))
        await _send_trade_exit_alert(
            symbol, trading_strategy, entry_price, current_price,
            qty, pnl_pct, position.get('entry_timestamp'), 'time_stop')
        return 'time_stop'

    return 'none'


def _cleanup_position_state(order_id: str, is_auto: bool):
    """Clear trailing stop and analyst state for a closed position."""
    if is_auto:
        bot_state.auto_clear_trailing_stop(order_id)
        bot_state.remove_auto_analyst_last_run(order_id)
        bot_state.remove_auto_flash_analyst_last_run(order_id)
    else:
        bot_state.clear_trailing_stop(order_id)
        bot_state.remove_analyst_last_run(order_id)
        bot_state.remove_flash_analyst_last_run(order_id)


def _resolve_trade_attribution(order_id, pnl_pct, entry_price, exit_price,
                                exit_reason):
    """Resolve signal attribution for a closed trade (best-effort)."""
    try:
        pnl_usd = (exit_price - entry_price)  # per-unit, simplified
        process_closed_trade(
            order_id, pnl=pnl_usd, pnl_pct=pnl_pct,
            exit_reason=exit_reason)
    except Exception as e:
        log.debug(f"Attribution resolution skipped for {order_id}: {e}")


async def _send_exit_alert(symbol: str, current_price: float,
                           asset_type: str, reason: str):
    """Send a SELL alert via Telegram for manual positions (legacy)."""
    alert = {
        "signal": "SELL", "symbol": symbol,
        "current_price": current_price, "reason": reason,
    }
    if asset_type == 'stock':
        alert['asset_type'] = 'stock'
    await send_telegram_alert(alert)


async def _send_trade_exit_alert(symbol, strategy, entry_price, exit_price,
                                 qty, pnl_pct, entry_timestamp, exit_reason):
    """Send enhanced exit alert for all strategies."""
    try:
        from src.notify.telegram_periodic_summary import send_trade_alert
        pnl_dollar = (exit_price - entry_price) * qty
        # Compute hold duration
        hold = ""
        if entry_timestamp:
            try:
                from datetime import datetime, timezone
                s = str(entry_timestamp).replace('Z', '').split('+')[0].split('.')[0]
                dt = datetime.fromisoformat(s).replace(tzinfo=timezone.utc)
                delta = datetime.now(timezone.utc) - dt
                days = delta.days
                hours = delta.seconds // 3600
                hold = f"{days}d {hours}h" if days > 0 else f"{hours}h"
            except Exception:
                pass
        await send_trade_alert(
            action="SELL", symbol=symbol, trading_strategy=strategy,
            entry_price=entry_price, exit_price=exit_price,
            quantity=qty, pnl=pnl_dollar, pnl_pct=pnl_pct * 100,
            hold_duration=hold, exit_reason=exit_reason)
    except Exception:
        pass
