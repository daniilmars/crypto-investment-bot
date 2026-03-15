"""Dynamic risk sizing — ATR-based per-symbol SL/TP computation.

Computes stop-loss and take-profit percentages from the asset's ATR,
clamped within configurable floor/ceiling bounds.
Falls back to static config values when ATR is unavailable.
"""

from src.logger import log


def compute_dynamic_sl_tp(
    atr_pct: float | None,
    config_sl: float,
    config_tp: float,
    sl_atr_mult: float = 1.5,
    tp_atr_mult: float = 3.0,
    sl_floor: float = 0.02,
    sl_ceiling: float = 0.07,
    tp_floor: float = 0.04,
    tp_ceiling: float = 0.15,
) -> tuple[float, float]:
    """Compute per-symbol SL/TP from ATR percentage.

    Args:
        atr_pct: ATR as a fraction of current price (e.g. 0.03 = 3%).
                 None triggers fallback to config values.
        config_sl: Static stop-loss from config (fallback).
        config_tp: Static take-profit from config (fallback).
        sl_atr_mult: Multiplier applied to ATR% for stop-loss.
        tp_atr_mult: Multiplier applied to ATR% for take-profit.
        sl_floor: Minimum SL percentage.
        sl_ceiling: Maximum SL percentage.
        tp_floor: Minimum TP percentage.
        tp_ceiling: Maximum TP percentage.

    Returns:
        (sl_pct, tp_pct) as floats (e.g. 0.05 = 5%).
    """
    if atr_pct is None:
        return (config_sl, config_tp)

    sl = max(sl_floor, min(sl_ceiling, atr_pct * sl_atr_mult))
    tp = max(tp_floor, min(tp_ceiling, atr_pct * tp_atr_mult))

    log.debug(f"Dynamic risk: ATR%={atr_pct:.4f} → SL={sl:.4f}, TP={tp:.4f}")
    return (sl, tp)
