"""Time-stop helper: closes slow-drift positions that never trigger SL/TP/trailing.

Fires when a position has been held >= max_hold_days AND pnl has stayed in the
narrow band (-max_loss_pct, min_gain_pct). Intended to free capital from
positions that won't hit the strategy's normal exits.

Pure function module — no side effects. Callers handle logging, DB writes,
Telegram alerts, cooldowns.
"""

from datetime import datetime, timezone


def _parse_entry_timestamp(entry_timestamp):
    """Parse trade entry_timestamp to UTC datetime. Returns None on failure."""
    if not entry_timestamp:
        return None
    try:
        if isinstance(entry_timestamp, datetime):
            dt = entry_timestamp
        else:
            s = str(entry_timestamp).replace('Z', '').split('+')[0].split('.')[0]
            dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        return None


def should_time_stop(
    position: dict,
    current_price: float,
    cfg: dict | None,
    trading_strategy: str,
    *,
    now: datetime | None = None,
) -> tuple[bool, str]:
    """Decide whether a position should be closed by time-stop.

    Returns (should_close, reason). When should_close is False the reason
    describes which guard blocked it (useful for debug logs).
    """
    if not cfg or not cfg.get('enabled', False):
        return False, "disabled"
    if trading_strategy in ('longterm', 'manual'):
        return False, f"exempt strategy: {trading_strategy}"
    if position.get('strategy_type'):
        return False, f"strategic: {position.get('strategy_type')}"

    entry_price = position.get('entry_price')
    if not entry_price or entry_price <= 0:
        return False, "missing entry_price"
    entry_ts = _parse_entry_timestamp(position.get('entry_timestamp'))
    if entry_ts is None:
        return False, "missing entry_timestamp"

    now = now or datetime.now(timezone.utc)
    age_days = (now - entry_ts).total_seconds() / 86400.0
    max_days = float(cfg.get('max_hold_days', 14))
    if age_days < max_days:
        return False, f"age {age_days:.1f}d < {max_days}d"

    pnl_pct = (current_price - entry_price) / entry_price
    min_gain = float(cfg.get('min_gain_pct', 0.02))
    max_loss = float(cfg.get('max_loss_pct', 0.05))
    if pnl_pct >= min_gain:
        return False, f"pnl {pnl_pct*100:+.2f}% >= {min_gain*100:.2f}% (winning)"
    if pnl_pct <= -max_loss:
        return False, f"pnl {pnl_pct*100:+.2f}% <= -{max_loss*100:.2f}% (SL range)"

    return True, (f"time_stop: held {age_days:.1f}d, pnl "
                  f"{pnl_pct*100:+.2f}% in band "
                  f"(-{max_loss*100:.1f}%, +{min_gain*100:.1f}%)")
