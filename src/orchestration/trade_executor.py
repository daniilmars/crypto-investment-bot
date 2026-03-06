"""Trade executor — executes confirmed signals and BUY/SELL trade logic.

Contains execute_confirmed_signal (callback for Telegram approval),
process_trade_signal (unified signal processing pipeline),
and helpers for the BUY/SELL execution patterns used in bot cycles.
"""

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


def _set_cooldown(symbol, signal_type, cooldown_hours, is_auto):
    """Set signal cooldown via bot_state (auto vs manual)."""
    expires = datetime.now(timezone.utc) + timedelta(hours=cooldown_hours)
    if is_auto:
        bot_state.set_auto_signal_cooldown(symbol, signal_type, expires)
    else:
        bot_state.set_signal_cooldown(symbol, signal_type, expires)


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

    # 2. BUY path
    if signal_type == "BUY":
        if pdt_status and pdt_status.get('is_restricted'):
            log.info(f"Skipping BUY for {symbol}: PDT rule — no day trades remaining.")
            return None

        allowed, size_mult, _ = check_buy_gates(
            symbol, positions, max_positions,
            suppress_buys, macro_multiplier,
            asset_type=asset_type, label=label)

        if allowed:
            result = await execute_buy(
                symbol, signal, current_price, balance,
                risk_pct, size_mult,
                asset_type=asset_type, trading_strategy=trading_strategy,
                broker=broker, label=label)
            _set_cooldown(symbol, "BUY", signal_cooldown_hours, is_auto)
            return result
        else:
            signal['signal'] = 'HOLD'
            return None

    # 3. SELL path
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

    # 4. HOLD
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
                    symbol, "BUY", quantity, current_price, asset_type='stock')
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
            order_result = place_order(symbol, "BUY", quantity, current_price)
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
):
    """Calculate quantity and execute a BUY signal (or send for confirmation).

    For auto-trading (trading_strategy='auto'), skips confirmation and alerts.
    """
    prefix = f"[{label}] " if label else ""
    is_auto = trading_strategy == 'auto'

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
        if quantity * current_price < 1.00:
            log.warning(f"{prefix}Skipping BUY for {symbol}: notional ${quantity * current_price:.2f} below $1.00 minimum.")
            return None

    if is_auto:
        log.info(f"{prefix}Executing BUY {quantity:.6f} {symbol} "
                 f"(size_mult={size_mult:.2f})")
        order_kw = {'trading_strategy': 'auto'}
        if asset_type == 'stock':
            order_kw['asset_type'] = 'stock'
        return place_order(symbol, "BUY", quantity, current_price, **order_kw)

    # Manual trading
    signal['quantity'] = quantity
    signal['asset_type'] = asset_type

    if broker == 'alpaca':
        if is_confirmation_required("BUY"):
            log.info(f"{prefix}Sending BUY {symbol} (Alpaca) for confirmation.")
            await send_signal_for_confirmation(signal)
            return None
        else:
            log.info(f"{prefix}Executing Alpaca trade: BUY {quantity:.4f} {symbol} "
                     f"(size_mult={size_mult:.2f}).")
            order_result = place_stock_order(symbol, "BUY", quantity, current_price)
            if order_result.get('status') == 'FILLED':
                signal['order_result'] = order_result
            await send_telegram_alert(signal)
            return order_result

    # Paper/live crypto or paper stock
    if is_confirmation_required("BUY"):
        log.info(f"{prefix}Sending BUY {symbol} for confirmation "
                 f"(qty={quantity:.6f}).")
        await send_signal_for_confirmation(signal)
        return None
    else:
        log.info(f"{prefix}Executing trade: BUY {quantity:.6f} {symbol} "
                 f"(risk={risk_pct:.4f}, size_mult={size_mult:.2f}).")
        order_kw = {}
        if asset_type == 'stock':
            order_kw['asset_type'] = 'stock'
        order_result = place_order(symbol, "BUY", quantity, current_price, **order_kw)
        if order_result.get('status') == 'FILLED':
            signal['order_result'] = order_result
        await send_telegram_alert(signal)
        return order_result


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
    is_auto = trading_strategy == 'auto'
    qty = position['quantity']
    order_id = position['order_id']

    signal['quantity'] = qty
    signal['position'] = position
    signal['asset_type'] = asset_type

    if is_auto:
        log.info(f"{prefix}Executing SELL {qty:.6f} {symbol}")
        order_kw = {'trading_strategy': 'auto'}
        if asset_type == 'stock':
            order_kw['asset_type'] = 'stock'
        result = place_order(symbol, "SELL", qty, current_price,
                             existing_order_id=order_id, **order_kw)
        bot_state.auto_clear_trailing_stop(order_id)
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
            await send_telegram_alert(signal)
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
                                   existing_order_id=order_id, **order_kw)
        bot_state.clear_trailing_stop(order_id)
        if order_result.get('status') == 'CLOSED':
            signal['order_result'] = order_result
        await send_telegram_alert(signal)
        return order_result
