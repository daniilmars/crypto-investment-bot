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
    send_telegram_alert,
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
        # Prefer the per-symbol scored articles already routed through the
        # cycle (signal['attribution_articles']). Falls back to a DB lookup
        # for legacy code paths that don't attach them.
        articles = signal.get('attribution_articles') or build_attribution_articles(symbol)
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


def _record_rotation_attribution(symbol, signal, order_id, trading_strategy,
                                 rotate_sym: str):
    """Attribution write for rotation-entry trades.

    Fallback chain (mirrors _record_trade_attribution but extends it):
        1. signal['attribution_articles'] (if rotation fired from a fresh BUY signal)
        2. build_attribution_articles(symbol) — DB lookup
        3. get_recent_assessment(symbol, 0.5h) — direction-agnostic Gemini row
        4. Empty sources + catalyst_type='rotation_pick' — invariant: row always written
    """
    try:
        from src.analysis.signal_attribution import (
            build_attribution_articles,
            link_attribution_to_order,
            record_signal_attribution,
        )
        articles = signal.get('attribution_articles') or build_attribution_articles(symbol)

        gemini = None
        if signal.get('gemini_confidence') is not None:
            gemini = {
                'direction': signal.get('gemini_direction'),
                'confidence': signal.get('gemini_confidence'),
                'catalyst_type': signal.get('catalyst_type'),
            }
        else:
            # Fallback: look up most recent assessment (any direction)
            try:
                from src.analysis.recent_assessment import get_recent_assessment
                recent = get_recent_assessment(symbol, hours=0.5)
                if recent is not None:
                    gemini = {
                        'direction': recent.get('direction'),
                        'confidence': recent.get('confidence'),
                        'catalyst_type': recent.get('catalyst_type') or 'rotation_pick',
                    }
            except Exception as e:
                log.debug(f"get_recent_assessment fallback failed for {symbol}: {e}")

        # Invariant: write SOMETHING so signal_attribution is 1:1 with trades.
        if gemini is None:
            gemini = {'direction': None, 'confidence': None,
                      'catalyst_type': 'rotation_pick'}

        # Decorate signal with a trade_reason so the stored row shows this
        # entry came from a rotation swap (useful in post-hoc analysis).
        enriched_signal = dict(signal)
        if not enriched_signal.get('trade_reason'):
            enriched_signal['trade_reason'] = f'rotation_from_{rotate_sym}'

        attr_id = record_signal_attribution(
            enriched_signal, articles=articles, gemini_assessment=gemini)
        if attr_id:
            link_attribution_to_order(attr_id, order_id)
            log.info(
                f"Rotation attribution #{attr_id} -> order {order_id} "
                f"({trading_strategy}/{symbol}, {len(articles)} articles, "
                f"catalyst={gemini.get('catalyst_type')})")
    except Exception as e:
        log.warning(f"Rotation attribution failed for {symbol}/{order_id}: {e}")


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
                    broker=broker, label=label, is_auto=is_auto,
                    # Forward dynamic SL/TP + strategy_config so the
                    # rotation entry inherits the same risk budget the
                    # normal BUY path would have used.
                    dynamic_sl_pct=dynamic_sl_pct,
                    dynamic_tp_pct=dynamic_tp_pct,
                    strategy_config=strategy_config)
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
        sig_reason = (signal.get('reason') or '').strip() or None
        result = place_order(symbol, "SELL", qty, current_price,
                             existing_order_id=order_id, exit_reason='signal_sell',
                             exit_reasoning=sig_reason, **order_kw)
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
        log.info(f"{prefix}Executing Alpaca trade: SELL {qty:.4f} {symbol}.")
        order_result = place_stock_order(symbol, "SELL", qty, current_price)
        if order_result.get('status') == 'FILLED':
            signal['order_result'] = order_result
            from src.execution.stock_trader import _is_same_day_trade, _record_day_trade
            if _is_same_day_trade(position):
                _record_day_trade()
                log.info(f"PDT: recorded day trade for {symbol}")
            try:
                from src.notify.telegram_periodic_summary import send_trade_alert
                entry_price = position['entry_price']
                pnl_pct = (current_price - entry_price) / entry_price
                pnl_dollar = (current_price - entry_price) * qty
                hold = _hold_duration(position.get('entry_timestamp'))
                await send_trade_alert(
                    action="SELL", symbol=symbol,
                    trading_strategy=trading_strategy,
                    entry_price=entry_price, exit_price=current_price,
                    quantity=qty, pnl=pnl_dollar, pnl_pct=pnl_pct * 100,
                    hold_duration=hold, exit_reason='signal_sell')
            except Exception:
                pass
        else:
            log.warning(f"SELL order for {symbol} failed: {order_result.get('message')}")
        return order_result

    # Paper/live crypto or paper stock
    log.info(f"{prefix}Executing trade: SELL {qty:.6f} {symbol}.")
    order_kw = {}
    if asset_type == 'stock':
        order_kw['asset_type'] = 'stock'
    sig_reason = (signal.get('reason') or '').strip() or None
    order_result = place_order(symbol, "SELL", qty, current_price,
                               existing_order_id=order_id, exit_reason='signal_sell',
                               exit_reasoning=sig_reason, **order_kw)
    if order_result.get('status') == 'CLOSED':
        bot_state.clear_trailing_stop(order_id)
        signal['order_result'] = order_result
        if asset_type == 'stock':
            from src.execution.stock_trader import _is_same_day_trade, _record_day_trade
            if _is_same_day_trade(position):
                _record_day_trade()
                log.info(f"PDT: recorded day trade for {symbol}")
        try:
            from src.notify.telegram_periodic_summary import send_trade_alert
            entry_price = position['entry_price']
            pnl_pct = (current_price - entry_price) / entry_price
            pnl_dollar = (current_price - entry_price) * qty
            hold = _hold_duration(position.get('entry_timestamp'))
            await send_trade_alert(
                action="SELL", symbol=symbol,
                trading_strategy=trading_strategy,
                entry_price=entry_price, exit_price=current_price,
                quantity=qty, pnl=pnl_dollar, pnl_pct=pnl_pct * 100,
                hold_duration=hold, exit_reason='signal_sell')
        except Exception:
            pass
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
    dynamic_sl_pct: float | None = None,
    dynamic_tp_pct: float | None = None,
    strategy_config: dict | None = None,
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
        try:
            cand_reason = (candidate.get('reason') or '').strip()
            rot_reasoning = (
                f"Swapped {rotate_sym} → {symbol}: {cand_reason}"
                if cand_reason else f"Swapped {rotate_sym} → {symbol}"
            )
        except Exception:
            rot_reasoning = None
        sell_result = place_order(
            rotate_sym, "SELL", rotate_qty, rotate_price,
            existing_order_id=rotate_order_id, exit_reason='rotation',
            exit_reasoning=rot_reasoning, **sell_kw)
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

        # --- Resolve SL/TP with fallback tree so no rotation entry ever writes
        # NULL protection fields. Caller may have pre-computed values; we also
        # pass strategy-scoped settings so conservative/auto get their own
        # stop_loss_percentage / take_profit_percentage where configured. ---
        from src.analysis.dynamic_risk import resolve_sl_tp_for_entry
        resolve_settings = dict(app_config.get('settings', {}))
        if strategy_config:
            # Per-strategy overrides (risk_params block) take precedence
            strat_risk = (strategy_config.get('risk_params')
                          or strategy_config) if isinstance(strategy_config, dict) else {}
            if isinstance(strat_risk, dict):
                if 'stop_loss_percentage' in strat_risk:
                    resolve_settings['stop_loss_percentage'] = strat_risk['stop_loss_percentage']
                if 'take_profit_percentage' in strat_risk:
                    resolve_settings['take_profit_percentage'] = strat_risk['take_profit_percentage']
        cache_seed = {}
        if dynamic_sl_pct is not None and dynamic_tp_pct is not None:
            # Caller already computed — derive atr_pct back from sl for cache seed.
            # compute_dynamic_sl_tp inverts cleanly: atr_pct ≈ sl / sl_atr_mult.
            dyn_cfg = resolve_settings.get('dynamic_risk', {}) or {}
            mult = float(dyn_cfg.get('sl_atr_multiplier', 1.5))
            if mult > 0:
                cache_seed[symbol] = float(dynamic_sl_pct) / mult
        sl_pct, tp_pct, sl_source = resolve_sl_tp_for_entry(
            symbol, current_price, asset_type, resolve_settings,
            cycle_atr_cache=cache_seed or None,
        )
        log.info(f"{prefix}Rotation SL/TP for {symbol}: "
                 f"SL={sl_pct:.2%} TP={tp_pct:.2%} (source={sl_source})")

        buy_kw = {
            'trading_strategy': 'auto',
            'trade_reason': f'rotation_from_{rotate_sym}',
            'dynamic_sl_pct': sl_pct,
            'dynamic_tp_pct': tp_pct,
        }
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

            # Write attribution row with rotation-aware fallback chain.
            try:
                _record_rotation_attribution(
                    symbol, signal, buy_result.get('order_id'),
                    trading_strategy, rotate_sym)
            except Exception as e:
                log.warning(
                    f"{prefix}Rotation attribution wrapper failed for "
                    f"{symbol}/{buy_result.get('order_id')}: {e}")
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
        # Manual confirmation flow removed (Apr 19) — non-auto rotation no
        # longer attempts a separate confirmation step. The auto branch
        # above handles all rotations now; everything else returns None.
        log.debug(f"{prefix}Skipping non-auto rotation {rotate_sym} → {symbol}")
        return None
