"""Pre-trade gates — consolidates all gate checks before opening a position.

Gate chain:
1. Macro regime (suppress_buys)
2. Duplicate position check
3. Max concurrent positions
4. Sector limit
5. Event calendar (block/reduce)
6. Stop-loss cooldown

Every rejection path ALSO emits a structured `[GATE_REJECT]` log line so the
`check-news` diagnostic can aggregate the 52→N signal funnel by gate.
"""

from datetime import datetime, timezone

from src.analysis.event_calendar import check_event_gate
from src.analysis.sector_limits import check_sector_limit, get_asset_class_concentration
from src.database import clear_stoploss_cooldown
from src.logger import log
from src.orchestration import bot_state


def _log_gate_reject(symbol: str, gate: str, strategy: str = '',
                     asset_type: str = '', **fields) -> None:
    """Emit a structured single-line gate-rejection event.

    Format: [GATE_REJECT] symbol=X gate=Y strategy=Z asset=A ...
    Greppable: `grep -oE '\\[GATE_REJECT\\].*gate=[a-z_]+'`
    """
    parts = [f"symbol={symbol}", f"gate={gate}"]
    if strategy:
        parts.append(f"strategy={strategy}")
    if asset_type:
        parts.append(f"asset={asset_type}")
    for k, v in fields.items():
        if v is None or v == "":
            continue
        parts.append(f"{k}={v}")
    log.info("[GATE_REJECT] " + " ".join(parts))


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
    strat_tag = (label or '').lower()

    # 1. Macro regime
    if suppress_buys:
        reason = f"{prefix}Skipping BUY for {symbol}: Macro regime RISK_OFF."
        log.info(reason)
        _log_gate_reject(symbol, 'macro_regime_risk_off',
                         strategy=strat_tag, asset_type=asset_type)
        return False, 0.0, reason

    # 2. Duplicate position (use .get for status — Alpaca positions may lack it)
    #    Also block if there's a PENDING limit order for the same symbol
    if any(p['symbol'] == symbol and p.get('status', 'OPEN') == 'OPEN'
           for p in open_positions):
        reason = f"{prefix}Skipping BUY for {symbol}: Position already open."
        log.info(reason)
        _log_gate_reject(symbol, 'position_already_open',
                         strategy=strat_tag, asset_type=asset_type)
        return False, 0.0, reason

    try:
        from src.database import get_pending_orders
        # Map Telegram-side label ('AUTO'/'CONSERVATIVE'/'LONGTERM') to the
        # DB strategy column. Legacy manual-loop labels ('paper'/'live'/etc.)
        # fall through to 'auto' since manual trading is removed.
        label_lower = (label or '').lower()
        strategy = label_lower if label_lower in ('auto', 'conservative', 'longterm') else 'auto'
        pending = get_pending_orders(asset_type=asset_type, trading_strategy=strategy)
        if any(p['symbol'] == symbol for p in pending):
            reason = f"{prefix}Skipping BUY for {symbol}: Pending limit order exists."
            log.info(reason)
            _log_gate_reject(symbol, 'pending_limit_order',
                             strategy=strat_tag, asset_type=asset_type)
            return False, 0.0, reason
    except Exception:
        pending = []  # Non-critical — continue with other gates

    # 3. Max concurrent positions (count PENDING orders too)
    pending_count = len(pending)
    if len(open_positions) + pending_count >= max_positions:
        reason = (f"{prefix}Skipping BUY for {symbol}: Max concurrent positions "
                  f"({max_positions}) reached.")
        log.info(reason)
        _log_gate_reject(symbol, 'max_concurrent_positions',
                         strategy=strat_tag, asset_type=asset_type,
                         max=max_positions,
                         current=len(open_positions) + pending_count)
        return False, 0.0, reason

    # 4. Sector limit — strategy is only used for logging context.
    label_lower = (label or '').lower()
    strategy = label_lower if label_lower in ('auto', 'conservative', 'longterm') else None
    sector_ok, sector_msg = check_sector_limit(
        symbol, open_positions, strategy)
    if not sector_ok:
        reason = f"{prefix}Skipping BUY for {symbol}: {sector_msg}"
        log.info(reason)
        _log_gate_reject(symbol, 'sector_limit',
                         strategy=strat_tag, asset_type=asset_type,
                         detail=sector_msg.replace(' ', '_')[:60])
        return False, 0.0, reason

    # 5. Event calendar
    ev_action, ev_mult, ev_reason = check_event_gate(
        symbol, 'BUY', asset_type=asset_type)
    if ev_action == 'block':
        reason = f"{prefix}Skipping BUY for {symbol}: {ev_reason}"
        log.info(reason)
        _log_gate_reject(symbol, 'event_calendar_block',
                         strategy=strat_tag, asset_type=asset_type,
                         detail=str(ev_reason).replace(' ', '_')[:60])
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
                    expires_in_h = (cooldown - now).total_seconds() / 3600
                    _log_gate_reject(
                        symbol, 'stoploss_cooldown',
                        strategy='auto' if is_auto else '',
                        signal=signal_type,
                        expires_in_h=f"{expires_in_h:.1f}")
                    return True
                else:
                    bot_state.remove_auto_stoploss_cooldown(symbol)
        else:
            cooldown = bot_state.get_stoploss_cooldown(symbol)
            if cooldown:
                if now < cooldown:
                    expires_in_h = (cooldown - now).total_seconds() / 3600
                    _log_gate_reject(
                        symbol, 'stoploss_cooldown',
                        strategy='auto' if is_auto else '',
                        signal=signal_type,
                        expires_in_h=f"{expires_in_h:.1f}")
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
            expires_in_h = (expires - now).total_seconds() / 3600
            _log_gate_reject(
                symbol, 'signal_cooldown', strategy='auto',
                signal=signal_type, expires_in_h=f"{expires_in_h:.1f}")
            return True
        elif expires:
            bot_state.remove_auto_signal_cooldown(symbol, signal_type)
    else:
        expires = bot_state.get_signal_cooldown(symbol, signal_type)
        if expires and now < expires:
            expires_in_h = (expires - now).total_seconds() / 3600
            _log_gate_reject(
                symbol, 'signal_cooldown',
                signal=signal_type, expires_in_h=f"{expires_in_h:.1f}")
            return True
        elif expires:
            bot_state.remove_signal_cooldown(symbol, signal_type)
    return False
