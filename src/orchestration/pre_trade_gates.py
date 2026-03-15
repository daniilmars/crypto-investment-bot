"""Pre-trade gates — consolidates all gate checks before opening a position.

Gate chain:
1. Macro regime (suppress_buys)
2. Duplicate position check
3. Max concurrent positions
4. Sector limit
5. Event calendar (block/reduce)
6. Stop-loss cooldown
"""

from datetime import datetime, timezone

from src.analysis.event_calendar import check_event_gate
from src.analysis.sector_limits import check_sector_limit, get_asset_class_concentration
from src.database import clear_stoploss_cooldown
from src.logger import log
from src.orchestration import bot_state


def check_buy_gates(
    symbol: str,
    open_positions: list,
    max_positions: int,
    suppress_buys: bool,
    macro_multiplier: float,
    asset_type: str = 'crypto',
    label: str = '',
) -> tuple:
    """Check all pre-trade gates for a BUY signal.

    Returns (allowed: bool, size_multiplier: float, reason: str).
    If allowed is False, reason explains why.
    """
    prefix = f"[{label}] " if label else ""

    # 1. Macro regime
    if suppress_buys:
        reason = f"{prefix}Skipping BUY for {symbol}: Macro regime RISK_OFF."
        log.info(reason)
        return False, 0.0, reason

    # 2. Duplicate position (use .get for status — Alpaca positions may lack it)
    #    Also block if there's a PENDING limit order for the same symbol
    if any(p['symbol'] == symbol and p.get('status', 'OPEN') == 'OPEN'
           for p in open_positions):
        reason = f"{prefix}Skipping BUY for {symbol}: Position already open."
        log.info(reason)
        return False, 0.0, reason

    try:
        from src.database import get_pending_orders
        strategy = 'auto' if label == 'AUTO' else 'manual'
        pending = get_pending_orders(asset_type=asset_type, trading_strategy=strategy)
        if any(p['symbol'] == symbol for p in pending):
            reason = f"{prefix}Skipping BUY for {symbol}: Pending limit order exists."
            log.info(reason)
            return False, 0.0, reason
    except Exception:
        pass  # Non-critical — continue with other gates

    # 3. Max concurrent positions (count PENDING orders too)
    try:
        pending_count = len(pending) if 'pending' in dir() else 0
    except Exception:
        pending_count = 0
    if len(open_positions) + pending_count >= max_positions:
        reason = (f"{prefix}Skipping BUY for {symbol}: Max concurrent positions "
                  f"({max_positions}) reached.")
        log.info(reason)
        return False, 0.0, reason

    # 4. Sector limit
    strategy = 'auto' if label == 'AUTO' else None
    sector_ok, sector_msg = check_sector_limit(
        symbol, open_positions, strategy)
    if not sector_ok:
        reason = f"{prefix}Skipping BUY for {symbol}: {sector_msg}"
        log.info(reason)
        return False, 0.0, reason

    # 5. Event calendar
    ev_action, ev_mult, ev_reason = check_event_gate(
        symbol, 'BUY', asset_type=asset_type)
    if ev_action == 'block':
        reason = f"{prefix}Skipping BUY for {symbol}: {ev_reason}"
        log.info(reason)
        return False, 0.0, reason

    # 6. Concentration scaling (reduce size as asset class fills up)
    conc_mult = get_asset_class_concentration(symbol, open_positions)

    # Calculate final size multiplier
    size_mult = macro_multiplier * (ev_mult if ev_action == 'reduce' else 1.0) * conc_mult
    if ev_action == 'reduce':
        log.info(f"{prefix}Reducing BUY for {symbol}: {ev_reason} (mult={ev_mult})")
    if conc_mult < 1.0:
        log.info(f"{prefix}Concentration scaling for {symbol}: {conc_mult:.2f}x "
                 f"(asset class filling up)")

    return True, size_mult, ''


async def check_stoploss_cooldown(symbol: str, signal_type: str,
                                   is_auto: bool = False) -> bool:
    """Check and clear expired stop-loss cooldowns.

    Returns True if the signal should be blocked (cooldown active).
    """
    if signal_type not in ("BUY", "SELL"):
        return False

    now = datetime.now(timezone.utc)

    # Check both manual and auto cooldowns — a stop-loss on either
    # bot should prevent re-entry from the other bot too.
    for check_auto in (True, False):
        if check_auto:
            cooldown = bot_state.get_auto_stoploss_cooldown(symbol)
            if cooldown:
                if now < cooldown:
                    return True
                else:
                    bot_state.remove_auto_stoploss_cooldown(symbol)
        else:
            cooldown = bot_state.get_stoploss_cooldown(symbol)
            if cooldown:
                if now < cooldown:
                    return True
                else:
                    bot_state.remove_stoploss_cooldown(symbol)
                    await clear_stoploss_cooldown(symbol)
    return False


async def check_signal_cooldown(symbol: str, signal_type: str,
                                 cooldown_hours: float,
                                 is_auto: bool = False) -> bool:
    """Check if a recent signal cooldown is active for this symbol+type.

    Returns True if the signal should be blocked (cooldown active).
    """
    if signal_type not in ("BUY", "SELL", "INCREASE"):
        return False

    now = datetime.now(timezone.utc)
    if is_auto:
        expires = bot_state.get_auto_signal_cooldown(symbol, signal_type)
        if expires and now < expires:
            return True
        elif expires:
            bot_state.remove_auto_signal_cooldown(symbol, signal_type)
    else:
        expires = bot_state.get_signal_cooldown(symbol, signal_type)
        if expires and now < expires:
            return True
        elif expires:
            bot_state.remove_signal_cooldown(symbol, signal_type)
    return False
