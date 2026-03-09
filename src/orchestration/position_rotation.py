"""Position rotation — rotates underperforming positions for stronger signals.

When max positions are reached and a new strong BUY signal fires, evaluates
whether the weakest existing position should be closed to make room.

PnL velocity (% gain per day held) is the core metric for comparison.
Strategic positions (growth, sector_thesis, etc.) are never rotated.
"""

from datetime import datetime, timezone

from src.config import app_config
from src.logger import log


def _get_rotation_config() -> dict:
    """Load rotation config from settings."""
    return app_config.get('settings', {}).get('position_rotation', {})


def compute_pnl_velocity(position: dict, current_price: float) -> float:
    """Compute PnL velocity: % gain per day held.

    Returns a float where positive = gaining, negative = losing.
    Floor at 1 hour held to avoid division by tiny numbers.
    """
    entry_price = position.get('entry_price', 0)
    if entry_price <= 0 or current_price <= 0:
        return 0.0

    pnl_pct = (current_price - entry_price) / entry_price

    entry_ts = position.get('entry_timestamp')
    if entry_ts:
        if isinstance(entry_ts, str):
            # Parse timestamp string (DB format)
            try:
                entry_dt = datetime.fromisoformat(entry_ts.replace('Z', '+00:00'))
                if entry_dt.tzinfo is None:
                    entry_dt = entry_dt.replace(tzinfo=timezone.utc)
            except ValueError:
                return pnl_pct  # fallback: treat as 1-day hold
        elif isinstance(entry_ts, datetime):
            entry_dt = entry_ts if entry_ts.tzinfo else entry_ts.replace(tzinfo=timezone.utc)
        else:
            return pnl_pct

        days_held = max(
            (datetime.now(timezone.utc) - entry_dt).total_seconds() / 86400,
            1 / 24,  # floor: 1 hour
        )
        return pnl_pct / days_held

    return pnl_pct  # no timestamp: treat as 1-day hold


def evaluate_rotation_candidate(
    open_positions: list,
    new_signal: dict,
    current_prices: dict,
    config: dict | None = None,
) -> dict | None:
    """Evaluate whether an existing position should be rotated out.

    Args:
        open_positions: list of open position dicts
        new_signal: the incoming BUY signal dict (must have 'signal_strength')
        current_prices: {symbol: price} dict for computing PnL velocity
        config: rotation config override (default: from settings)

    Returns:
        dict with rotation recommendation, or None if no rotation warranted.
        Keys: rotate_out, pnl_velocity, signal_strength, reason
    """
    cfg = config or _get_rotation_config()
    if not cfg.get('enabled', False):
        return None

    signal_strength = new_signal.get('signal_strength', 0)
    min_signal = cfg.get('min_signal_strength', 0.6)
    if signal_strength < min_signal:
        return None

    min_hold_hours = cfg.get('min_hold_hours', 24)
    min_velocity = cfg.get('min_pnl_velocity_threshold', -0.005)
    min_advantage = cfg.get('min_strength_advantage', 0.15)

    now = datetime.now(timezone.utc)
    candidates = []

    for pos in open_positions:
        # Never rotate strategic positions
        if pos.get('strategy_type'):
            continue

        # Check minimum hold time
        entry_ts = pos.get('entry_timestamp')
        if entry_ts:
            if isinstance(entry_ts, str):
                try:
                    entry_dt = datetime.fromisoformat(entry_ts.replace('Z', '+00:00'))
                    if entry_dt.tzinfo is None:
                        entry_dt = entry_dt.replace(tzinfo=timezone.utc)
                except ValueError:
                    continue
            elif isinstance(entry_ts, datetime):
                entry_dt = entry_ts if entry_ts.tzinfo else entry_ts.replace(tzinfo=timezone.utc)
            else:
                continue

            hours_held = (now - entry_dt).total_seconds() / 3600
            if hours_held < min_hold_hours:
                continue

        sym = pos.get('symbol', '')
        price = current_prices.get(sym)
        if not price:
            continue

        velocity = compute_pnl_velocity(pos, price)
        candidates.append((pos, velocity))

    if not candidates:
        return None

    # Find weakest position by PnL velocity
    candidates.sort(key=lambda x: x[1])
    weakest_pos, weakest_velocity = candidates[0]

    # Only rotate if weakest is below threshold
    if weakest_velocity > min_velocity:
        return None

    # New signal must be meaningfully better
    if signal_strength - abs(weakest_velocity) < min_advantage:
        return None

    sym = weakest_pos.get('symbol', '?')
    entry = weakest_pos.get('entry_price', 0)
    price = current_prices.get(sym, 0)
    pnl_pct = ((price - entry) / entry * 100) if entry > 0 else 0

    reason = (
        f"Rotating out {sym} (PnL {pnl_pct:+.1f}%, velocity {weakest_velocity:+.4f}/day) "
        f"for {new_signal.get('symbol', '?')} (signal strength {signal_strength:.2f})"
    )
    log.info(f"Position rotation: {reason}")

    return {
        'rotate_out': weakest_pos,
        'pnl_velocity': weakest_velocity,
        'signal_strength': signal_strength,
        'reason': reason,
    }


def format_rotation_message(candidate: dict, new_signal: dict) -> str:
    """Format a human-readable rotation summary for Telegram."""
    pos = candidate['rotate_out']
    sym_out = pos.get('symbol', '?')
    sym_in = new_signal.get('symbol', '?')
    velocity = candidate['pnl_velocity']

    entry = pos.get('entry_price', 0)
    qty = pos.get('quantity', 0)

    lines = [
        f"🔄 *Position Rotation*",
        f"",
        f"📤 *Closing:* {sym_out}",
        f"   Entry: ${entry:,.2f}, Qty: {qty:.4f}",
        f"   PnL velocity: {velocity:+.4f}%/day",
        f"",
        f"📥 *Opening:* {sym_in}",
        f"   Signal strength: {candidate['signal_strength']:.2f}",
        f"   Reason: {new_signal.get('reason', 'N/A')[:100]}",
    ]
    return "\n".join(lines)
