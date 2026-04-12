"""Trade executor — executes confirmed signals and BUY/SELL trade logic.

Contains execute_confirmed_signal (callback for Telegram approval),
process_trade_signal (unified signal processing pipeline),
and helpers for the BUY/SELL execution patterns used in bot cycles.
"""

import asyncio
from datetime import datetime, timedelta, timezone

from src.execution.binance_trader import (
    add_to_position, get_open_positions, place_order,
    _get_trading_mode,
)
from src.execution.stock_trader import place_stock_order
from src.logger import log
from src.notify.telegram_bot import (
    is_confirmation_required, send_signal_for_confirmation, send_telegram_alert,
)
from src.orchestration import bot_state
from src.orchestration.pre_trade_gates import (
    check_buy_gates, check_stoploss_cooldown, check_signal_cooldown,
)
from src.config import app_config


def _hold_duration(entry_timestamp) -> str:
    """Format hold duration from entry timestamp to now."""
    if not entry_timestamp:
        return ""
    try:
        s = str(entry_timestamp).replace('Z', '').split('+')[0].split('.')[0]
        dt = datetime.fromisoformat(s).replace(tzinfo=timezone.utc)
        delta = datetime.now(timezone.utc) - dt
        days = delta.days
        hours = delta.seconds // 3600
        if days > 0:
            return f"{days}d {hours}h"
        return f"{hours}h"
    except Exception:
        return ""


def _set_cooldown(symbol, signal_type, cooldown_hours, is_auto):
    """Set signal cooldown via bot_state (auto vs manual)."""
    expires = datetime.now(timezone.utc) + timedelta(hours=cooldown_hours)
    if is_auto:
        bot_state.set_auto_signal_cooldown(symbol, signal_type, expires)
    else:
        bot_state.set_signal_cooldown(symbol, signal_type, expires)


def _record_trade_attribution(symbol, signal, order_id, trading_strategy):
    """Fire-and-forget attribution write. Never blocks, never raises.

    Called right after a successful BUY so each trade has a 1-to-1 row
    in signal_attribution with article_hashes, source_names, and
    trade_order_id populated.
    """
    try:
        from src.analysis.signal_attribution import (
            build_attribution_articles,
            link_attribution_to_order,
            record_signal_attribution,
        )
        articles = build_attribution_articles(symbol)
        gemini = None
        if signal.get('gemini_confidence') is not None:
            gemini = {
                'direction': signal.get('gemini_direction'),
                'confidence': signal.get('gemini_confidence'),
                'catalyst_type': signal.get('catalyst_type'),
            }
        attr_id = record_signal_attribution(
            signal, articles=articles, gemini_assessment=gemini)
        if attr_id:
            link_attribution_to_order(attr_id, order_id)
            log.info(
                f"Attribution #{attr_id} -> order {order_id} "
                f"({trading_strategy}/{symbol}, {len(articles)} articles)")
    except Exception as e:
        log.warning(f"Attribution recording failed for {symbol}/{order_id}: {e}")


async def process_trade_signal(
    symbol: str,
    signal: dict,
    current_price: float,
    positions: list,
    balance: float,
    risk_pct: float,
    signal_cooldown_hours: float,
    max_positions: int,
    suppress_buys: bool,
    macro_multiplier: float,
    *,
    asset_type: str = 'crypto',
    trading_strategy: str = 'manual',
    broker: str | None = None,
    label: str = '',
    is_auto: bool = False,
    pdt_status: dict | None = None,
    current_prices: dict | None = None,
    dynamic_sl_pct: float | None = None,
    dynamic_tp_pct: float | None = None,
    strategy_config: dict | None = None,
) -> dict | None:
    """Unified signal processing pipeline: cooldowns → gates → execute.

    Replaces duplicated gate→cooldown→execute patterns across all 10 call sites.
    Returns the order result dict, or None if no trade was executed.
    """
    signal_type = signal.get('signal', 'HOLD')

    # 0. Skip BUY if already holding position (avoids noisy cooldown logs)
    if signal_type == "BUY" and any(
        p['symbol'] == symbol and p.get('status', 'OPEN') == 'OPEN'
        for p in positions
    ):
        return None

    # 1. Cooldown checks
    if await check_stoploss_cooldown(symbol, signal_type, is_auto=is_auto):
        log.info(f"Skipping {signal_type} for {symbol}: stop-loss cooldown active.")
        signal['signal'] = 'HOLD'
        signal_type = 'HOLD'

    if await check_signal_cooldown(symbol, signal_type, signal_cooldown_hours, is_auto=is_auto):
        log.debug(f"Skipping {signal_type} for {symbol}: signal cooldown active.")
        signal['signal'] = 'HOLD'
        signal_type = 'HOLD'

    # 2. Auto signal quality gate — block weak signals
    if is_auto and signal_type in ("BUY", "SELL"):
        signal_strength = signal.get('signal_strength', 0)
        # Use per-strategy config if provided, else fall back to global auto_trading
        scfg = strategy_config or app_config.get('settings', {}).get('auto_trading', {})
        regime_cfg = scfg.get('regime_behavior', {})

        if signal_type == "BUY":
            min_strength = scfg.get('min_signal_strength', 0.65)
            # Transition trading: higher bar, but no caution boost
            if scfg.get('_transition_active'):
                min_strength = scfg.get('_transition_min_signal_strength', 0.80)
            elif macro_multiplier < 1.0:
                # Raise bar in CAUTION/RISK_OFF regime
                min_strength += regime_cfg.get('caution_strength_boost',
                                               scfg.get('caution_strength_boost', 0.10))
        else:
            min_strength = scfg.get('min_sell_signal_strength', 0.50)

        if signal_strength < min_strength:
            log.info(f"[{label}] Skipping auto {signal_type} for {symbol}: "
                     f"signal_strength {signal_strength:.2f} < {min_strength:.2f} "
                     f"(macro_mult={macro_multiplier})")
            return None

    # 3. BUY path
    if signal_type == "BUY":
        if pdt_status and pdt_status.get('is_restricted'):
            log.info(f"Skipping BUY for {symbol}: PDT rule — no day trades remaining.")
            return None

        allowed, size_mult, _ = check_buy_gates(
            symbol, positions, max_positions,
            suppress_buys, macro_multiplier,
            asset_type=asset_type, label=label)

        if allowed:
            # Streak-based position sizing
            streak_cfg = (strategy_config or {}).get('streak_sizing', {})
            streak_mult = bot_state.strategy_get_streak_multiplier(
                trading_strategy, streak_cfg)
            if streak_mult != 1.0:
                streak_state = bot_state.strategy_get_streak_state(trading_strategy)
                log.info(f"[{label}] Streak sizing: "
                         f"{streak_state.get('consecutive_wins', 0)} wins "
                         f"-> {streak_mult:.1f}x for {symbol}")

            result = await execute_buy(
                symbol, signal, current_price, balance,
                risk_pct, size_mult * streak_mult,
                asset_type=asset_type, trading_strategy=trading_strategy,
                broker=broker, label=label,
                dynamic_sl_pct=dynamic_sl_pct,
                dynamic_tp_pct=dynamic_tp_pct)
            _set_cooldown(symbol, "BUY", signal_cooldown_hours, is_auto)

            # Consume defensive mode + send alerts after trade executes
            if result and result.get('order_id'):
                _record_trade_attribution(
                    symbol, signal, result['order_id'], trading_strategy)
                if streak_cfg.get('enabled') and \
                   bot_state.strategy_get_streak_state(trading_strategy).get('in_defensive_mode'):
                    bot_state.strategy_consume_defensive_mode(trading_strategy)
                # Enhanced trade alert
                try:
                    from src.notify.telegram_periodic_summary import send_trade_alert
                    ga = signal.get('gemini_assessment') or {}
                    await send_trade_alert(
                        action="BUY", symbol=symbol,
                        trading_strategy=trading_strategy,
                        entry_price=current_price,
                        quantity=result.get('quantity', 0),
                        signal_strength=signal.get('signal_strength', 0),
                        gemini_direction=signal.get('gemini_direction', ''),
                        gemini_confidence=signal.get('gemini_confidence', 0),
                        catalyst_freshness=signal.get('catalyst_freshness', ''),
                        catalyst_type=signal.get('catalyst_type', ''),
                        key_headline=ga.get('key_headline', ''),
                        reason=signal.get('reason', ''),
                        macro_multiplier=macro_multiplier,
                        streak_multiplier=streak_mult,
                        sma_override=signal.get('sma_override', False),
                    )
                except Exception:
                    pass
            return result
        else:
            # Check if rotation is possible when max positions blocked
            if 'Max concurrent positions' in _ and current_prices:
                rotation_result = await _try_rotation(
                    symbol, signal, current_price, positions, balance,
                    risk_pct, macro_multiplier, signal_cooldown_hours,
                    current_prices=current_prices,
                    asset_type=asset_type, trading_strategy=trading_strategy,
                    broker=broker, label=label, is_auto=is_auto)
                if rotation_result:
                    return rotation_result
            signal['signal'] = 'HOLD'
            return None

    # 4. SELL path
    elif signal_type == "SELL":
        position_to_close = next(
            (p for p in positions
             if p['symbol'] == symbol and p.get('status', 'OPEN') == 'OPEN'), None)
        if position_to_close:
            result = await execute_sell(
                symbol, signal, position_to_close, current_price,
                asset_type=asset_type, trading_strategy=trading_strategy,
                broker=broker, label=label)
            _set_cooldown(symbol, "SELL", signal_cooldown_hours, is_auto)
            return result
        else:
            log.info(f"Skipping SELL for {symbol}: No open position found.")
            _set_cooldown(symbol, "SELL", signal_cooldown_hours, is_auto)
            return None

    # 5. HOLD
    else:
        log.info(f"Signal is HOLD for {symbol}. No trade action taken.")
        return None


async def execute_confirmed_signal(signal: dict) -> dict:
    """Executes a trade after user confirmation via Telegram.

    Called by the telegram_bot callback handler when user taps Approve.
    """
    signal_type = signal.get('signal')
    symbol = signal.get('symbol')
    current_price = signal.get('current_price', 0)
    asset_type = signal.get('asset_type', 'crypto')
    quantity = signal.get('quantity', 0)
    position = signal.get('position')
    order_result = None

    # Re-check for duplicate position (guards against TOCTOU race between
    # signal generation and user approval)
    if signal_type == "BUY":
        open_positions = get_open_positions(asset_type=asset_type)
        if any(p['symbol'] == symbol and p.get('status', 'OPEN') == 'OPEN'
               for p in open_positions):
            log.info(f"Skipping confirmed BUY for {symbol}: "
                     f"position already open.")
            return {"status": "SKIPPED",
                    "message": f"Position already open for {symbol}"}

    trading_mode = _get_trading_mode()
    log.info(f"Executing confirmed {signal_type} for {symbol} "
             f"({asset_type}, {trading_mode})")

    strategy_type = signal.get('strategy_type')
    trade_reason = signal.get('trade_reason') or signal.get('reason', '')

    if asset_type == 'stock':
        settings = app_config.get('settings', {})
        stock_settings = settings.get('stock_trading', {})
        broker = stock_settings.get('broker', 'paper_only')

        if broker == 'alpaca':
            order_result = place_stock_order(
                symbol, signal_type, quantity, current_price)
        else:
            if signal_type == "BUY":
                order_result = place_order(
                    symbol, "BUY", quantity, current_price, asset_type='stock',
                    strategy_type=strategy_type, trade_reason=trade_reason)
            elif signal_type == "SELL" and position:
                order_result = place_order(
                    symbol, "SELL", quantity, current_price,
                    existing_order_id=position.get('order_id'),
                    asset_type='stock')
                bot_state.clear_trailing_stop(position.get('order_id', ''))
                bot_state.remove_analyst_last_run(position.get('order_id', ''))
            elif signal_type == "INCREASE" and position:
                order_result = add_to_position(
                    position.get('order_id'), symbol, quantity, current_price,
                    reason=signal.get('reason', ''), asset_type=asset_type)
    else:
        # Crypto
        if signal_type == "BUY":
            order_result = place_order(symbol, "BUY", quantity, current_price,
                                       strategy_type=strategy_type, trade_reason=trade_reason)
        elif signal_type == "SELL" and position:
            order_result = place_order(
                symbol, "SELL", quantity, current_price,
                existing_order_id=position.get('order_id'))
            bot_state.clear_trailing_stop(position.get('order_id', ''))
            bot_state.remove_analyst_last_run(position.get('order_id', ''))
        elif signal_type == "INCREASE" and position:
            order_result = add_to_position(
                position.get('order_id'), symbol, quantity, current_price,
                reason=signal.get('reason', ''), asset_type=asset_type)

    return order_result or {}


def _should_use_limit_order(signal: dict, config: dict) -> tuple[bool, float]:
    """Decide whether to use a limit order based on signal strength.

    Returns (use_limit, pullback_pct).
    """
    limit_cfg = config.get('settings', {}).get('limit_orders', {})
    if not limit_cfg.get('enabled', False):
        return (False, 0.0)

    threshold = limit_cfg.get('market_order_threshold', 0.8)
    signal_strength = signal.get('signal_strength', 0)

    # High-confidence signals get immediate market orders
    if signal_strength >= threshold:
        return (False, 0.0)

    # Check max pending orders
    from src.database import get_pending_orders
    pending = get_pending_orders(asset_type=signal.get('asset_type', 'crypto'))
    max_pending = limit_cfg.get('max_pending_orders', 3)
    if len(pending) >= max_pending:
        return (False, 0.0)

    pullback = limit_cfg.get('pullback_pct', 0.005)
    return (True, pullback)


async def execute_buy(
    symbol: str,
    signal: dict,
    current_price: float,
    current_balance: float,
    risk_pct: float,
    size_mult: float,
    *,
    asset_type: str = 'crypto',
    trading_strategy: str = 'manual',
    broker: str | None = None,
    label: str = '',
    dynamic_sl_pct: float | None = None,
    dynamic_tp_pct: float | None = None,
):
    """Calculate quantity and execute a BUY signal (or send for confirmation).

    For auto strategies (any trading_strategy != 'manual'), skips confirmation.
    Manual BUY is disabled entirely — new entries must come from an automated
    strategy. Manual only handles protective exits on existing positions.
    """
    prefix = f"[{label}] " if label else ""
    is_auto = trading_strategy != 'manual'

    if not is_auto:
        log.debug(f"{prefix}Skipping manual BUY for {symbol}: "
                  f"manual strategy is exit-only.")
        return None

    capital_to_risk = current_balance * risk_pct * size_mult
    quantity = capital_to_risk / current_price if current_price > 0 else 0

    if quantity <= 0 or quantity * current_price > current_balance:
        log.warning(f"{prefix}Skipping BUY for {symbol}: Insufficient balance.")
        return None

    # Min trade size guard
    if asset_type == 'stock':
        if quantity < 0.5:
            log.warning(f"{prefix}Skipping BUY for {symbol}: quantity {quantity:.4f} below min 0.5 shares.")
            return None
    else:
        _min_notional = app_config.get('settings', {}).get('min_trade_notional', 5.00)
        if quantity * current_price < _min_notional:
            log.warning(f"{prefix}Skipping BUY for {symbol}: notional ${quantity * current_price:.2f} below ${_min_notional:.2f} minimum.")
            return None

    # --- Limit order decision (crypto only, not for stocks/alpaca) ---
    use_limit = False
    pullback_pct = 0.0
    if asset_type == 'crypto' and broker is None:
        use_limit, pullback_pct = _should_use_limit_order(signal, app_config)

    if is_auto:
        log.info(f"{prefix}Executing BUY {quantity:.6f} {symbol} "
                 f"(size_mult={size_mult:.2f})")

        if use_limit:
            return _place_limit_buy(
                symbol, quantity, current_price, pullback_pct,
                asset_type=asset_type, trading_strategy=trading_strategy,
                dynamic_sl_pct=dynamic_sl_pct, dynamic_tp_pct=dynamic_tp_pct)

        return place_order(symbol, "BUY", quantity, current_price,
                           asset_type=asset_type, trading_strategy=trading_strategy,
                           dynamic_sl_pct=dynamic_sl_pct,
                           dynamic_tp_pct=dynamic_tp_pct)


def _place_limit_buy(
    symbol: str,
    quantity: float,
    current_price: float,
    pullback_pct: float,
    *,
    asset_type: str = 'crypto',
    trading_strategy: str = 'manual',
    dynamic_sl_pct: float | None = None,
    dynamic_tp_pct: float | None = None,
) -> dict | None:
    """Place a PENDING limit buy order in the DB."""
    limit_price = current_price * (1 - pullback_pct)

    result = place_order(
        symbol, "BUY", quantity, limit_price,
        order_type="LIMIT",
        asset_type=asset_type,
        trading_strategy=trading_strategy,
        dynamic_sl_pct=dynamic_sl_pct,
        dynamic_tp_pct=dynamic_tp_pct,
    )
    log.info(f"Limit BUY placed for {symbol}: limit=${limit_price:.4f}, "
             f"pullback={pullback_pct:.1%}")
    return result


async def execute_sell(
    symbol: str,
    signal: dict,
    position: dict,
    current_price: float,
    *,
    asset_type: str = 'crypto',
    trading_strategy: str = 'manual',
    broker: str | None = None,
    label: str = '',
):
    """Execute a SELL signal (or send for confirmation).

    For auto-trading (trading_strategy='auto'), skips confirmation and alerts.
    """
    prefix = f"[{label}] " if label else ""
    is_auto = trading_strategy != 'manual'
    qty = position['quantity']
    order_id = position['order_id']

    signal['quantity'] = qty
    signal['position'] = position
    signal['asset_type'] = asset_type

    if is_auto:
        log.info(f"{prefix}Executing SELL {qty:.6f} {symbol}")
        order_kw = {'trading_strategy': trading_strategy}
        if asset_type == 'stock':
            order_kw['asset_type'] = 'stock'
        result = place_order(symbol, "SELL", qty, current_price,
                             existing_order_id=order_id, exit_reason='signal_sell', **order_kw)
        bot_state.strategy_clear_trailing_stop(order_id, trading_strategy)
        entry_price = position['entry_price']
        pnl_pct = (current_price - entry_price) / entry_price
        pnl_dollar = (current_price - entry_price) * qty
        bot_state.strategy_record_trade_outcome(trading_strategy, is_win=(pnl_pct > 0))
        try:
            from src.notify.telegram_periodic_summary import send_trade_alert
            hold = _hold_duration(position.get('entry_timestamp'))
            await send_trade_alert(
                action="SELL", symbol=symbol,
                trading_strategy=trading_strategy,
                entry_price=entry_price, exit_price=current_price,
                quantity=qty, pnl=pnl_dollar, pnl_pct=pnl_pct * 100,
                hold_duration=hold, exit_reason='signal_sell')
        except Exception:
            pass
        return result

    # Manual trading
    if broker == 'alpaca':
        if is_confirmation_required("SELL"):
            log.info(f"{prefix}Sending SELL {symbol} (Alpaca) for confirmation.")
            await send_signal_for_confirmation(signal)
            return None
        else:
            log.info(f"{prefix}Executing Alpaca trade: SELL {qty:.4f} {symbol}.")
            order_result = place_stock_order(symbol, "SELL", qty, current_price)
            if order_result.get('status') == 'FILLED':
                signal['order_result'] = order_result
                from src.execution.stock_trader import _is_same_day_trade, _record_day_trade
                if _is_same_day_trade(position):
                    _record_day_trade()
                    log.info(f"PDT: recorded day trade for {symbol}")
                await send_telegram_alert(signal)
            else:
                log.warning(f"SELL order for {symbol} failed: {order_result.get('message')}")
            return order_result

    # Paper/live crypto or paper stock
    if is_confirmation_required("SELL"):
        log.info(f"{prefix}Sending SELL {symbol} for confirmation.")
        await send_signal_for_confirmation(signal)
        return None
    else:
        log.info(f"{prefix}Executing trade: SELL {qty:.6f} {symbol}.")
        order_kw = {}
        if asset_type == 'stock':
            order_kw['asset_type'] = 'stock'
        order_result = place_order(symbol, "SELL", qty, current_price,
                                   existing_order_id=order_id, exit_reason='signal_sell', **order_kw)
        if order_result.get('status') == 'CLOSED':
            bot_state.clear_trailing_stop(order_id)
            signal['order_result'] = order_result
            if asset_type == 'stock':
                from src.execution.stock_trader import _is_same_day_trade, _record_day_trade
                if _is_same_day_trade(position):
                    _record_day_trade()
                    log.info(f"PDT: recorded day trade for {symbol}")
            await send_telegram_alert(signal)
        else:
            log.warning(f"SELL order for {symbol} failed: {order_result}")
        return order_result


# --- Position Rotation ---

async def _try_rotation(
    symbol: str,
    signal: dict,
    current_price: float,
    positions: list,
    balance: float,
    risk_pct: float,
    macro_multiplier: float,
    signal_cooldown_hours: float,
    *,
    current_prices: dict,
    asset_type: str = 'crypto',
    trading_strategy: str = 'manual',
    broker: str | None = None,
    label: str = '',
    is_auto: bool = False,
) -> dict | None:
    """Attempt position rotation when max positions blocks a BUY.

    Evaluates the weakest position and compares it against the new signal.
    If rotation is warranted, sells the weakest and buys the new symbol.
    """
    from src.orchestration.position_rotation import (
        evaluate_rotation_candidate, format_rotation_message,
    )

    # Check rotation cooldown
    now = datetime.now(timezone.utc)
    cooldown_expires = bot_state.get_rotation_cooldown(asset_type, is_auto=is_auto)
    if cooldown_expires and now < cooldown_expires:
        return None

    candidate = evaluate_rotation_candidate(positions, signal, current_prices)
    if not candidate:
        return None

    rotate_out = candidate['rotate_out']
    rotate_sym = rotate_out.get('symbol', '?')
    rotate_qty = rotate_out.get('quantity', 0)
    rotate_order_id = rotate_out.get('order_id', '')
    rotate_price = current_prices.get(rotate_sym, 0)
    prefix = f"[{label}] " if label else ""

    if is_auto:
        # Auto mode: execute immediately (sell then buy)
        log.info(f"{prefix}Rotation: selling {rotate_sym} to buy {symbol}")

        # Sell the weakest
        sell_kw = {'trading_strategy': 'auto'}
        if asset_type == 'stock':
            sell_kw['asset_type'] = 'stock'
        sell_result = place_order(
            rotate_sym, "SELL", rotate_qty, rotate_price,
            existing_order_id=rotate_order_id, exit_reason='rotation', **sell_kw)
        bot_state.auto_clear_trailing_stop(rotate_order_id)

        if sell_result.get('status') not in ('CLOSED', 'FILLED'):
            log.warning(f"{prefix}Rotation sell failed for {rotate_sym}: {sell_result}")
            return None

        # Buy the new symbol
        # Recalculate balance after sell
        from src.execution.binance_trader import get_account_balance
        new_balance = (await asyncio.to_thread(
            get_account_balance, asset_type=asset_type, trading_strategy='auto')
        ).get('USDT', 0) or (await asyncio.to_thread(
            get_account_balance, asset_type=asset_type, trading_strategy='auto')
        ).get('total_usd', 0)

        size_mult = macro_multiplier
        capital_to_risk = new_balance * risk_pct * size_mult
        quantity = capital_to_risk / current_price if current_price > 0 else 0

        if quantity <= 0:
            log.warning(f"{prefix}Rotation buy skipped: insufficient balance after sell")
            return sell_result

        buy_kw = {'trading_strategy': 'auto'}
        if asset_type == 'stock':
            buy_kw['asset_type'] = 'stock'
        buy_result = place_order(symbol, "BUY", quantity, current_price, **buy_kw)

        # Only set cooldown if buy succeeded
        if buy_result.get('status') in ('FILLED', 'OPEN'):
            rotation_cfg = app_config.get('settings', {}).get('position_rotation', {})
            cooldown_hours = rotation_cfg.get('rotation_cooldown_hours', 4)
            bot_state.set_rotation_cooldown(
                asset_type,
                now + timedelta(hours=cooldown_hours),
                is_auto=is_auto)
            _set_cooldown(symbol, "BUY", signal_cooldown_hours, is_auto)
        else:
            log.warning(f"{prefix}Rotation buy failed for {symbol}: {buy_result} — no cooldown set")

        # Send notification
        msg = format_rotation_message(candidate, signal)
        try:
            await send_telegram_alert({
                'signal': 'ROTATION',
                'symbol': symbol,
                'reason': msg,
                'asset_type': asset_type,
            })
        except Exception:
            pass

        log.info(f"{prefix}Rotation complete: {rotate_sym} → {symbol}")
        return buy_result

    else:
        # Manual mode: send rotation for confirmation
        signal['rotation_candidate'] = candidate
        signal['quantity'] = 0  # will be calculated on confirm
        signal['asset_type'] = asset_type
        log.info(f"{prefix}Sending rotation {rotate_sym} → {symbol} for confirmation")
        await send_signal_for_confirmation(signal)
        return None
