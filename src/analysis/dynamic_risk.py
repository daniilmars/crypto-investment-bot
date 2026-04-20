"""Dynamic risk sizing — ATR-based per-symbol SL/TP computation.

Computes stop-loss and take-profit percentages from the asset's ATR,
clamped within configurable floor/ceiling bounds.
Falls back to static config values when ATR is unavailable.
"""

from src.logger import log
from src.analysis.technical_indicators import calculate_atr


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


def resolve_sl_tp_for_entry(
    symbol: str,
    current_price: float,
    asset_type: str,
    settings: dict,
    cycle_atr_cache: dict | None = None,
    daily_klines: list | None = None,
) -> tuple[float, float, str]:
    """Resolve SL/TP for a new entry. **Never returns None** — config fallback
    is the final backstop so no trade row gets written with NULL SL/TP.

    Resolution order:
        1. ``cycle_atr_cache[symbol]`` (pre-computed this cycle)
        2. Live ATR from ``daily_klines`` if provided
        3. Static config fallback (``stop_loss_percentage`` /
           ``take_profit_percentage`` — possibly per-strategy if caller
           passed overrides in ``settings``)

    Returns ``(sl_pct, tp_pct, source_tag)``. ``source_tag`` ∈
    ``{'atr_cache', 'atr_live', 'config_fallback'}`` — useful for logging.
    """
    dyn_cfg = settings.get('dynamic_risk', {}) if settings else {}
    dyn_enabled = dyn_cfg.get('enabled', True)
    config_sl = float(settings.get('stop_loss_percentage', 0.10)) if settings else 0.10
    config_tp = float(settings.get('take_profit_percentage', 0.50)) if settings else 0.50
    atr_period = int(dyn_cfg.get('atr_period', 14))

    def _finalize(atr_pct: float | None, source: str) -> tuple[float, float, str]:
        if not dyn_enabled or atr_pct is None:
            log.debug(
                "resolve_sl_tp_for_entry(%s) → config_fallback SL=%.4f TP=%.4f "
                "(dyn_enabled=%s, atr_pct=%s)",
                symbol, config_sl, config_tp, dyn_enabled, atr_pct,
            )
            return (config_sl, config_tp, 'config_fallback')
        sl, tp = compute_dynamic_sl_tp(
            atr_pct, config_sl, config_tp,
            sl_atr_mult=dyn_cfg.get('sl_atr_multiplier', 1.5),
            tp_atr_mult=dyn_cfg.get('tp_atr_multiplier', 3.0),
            sl_floor=dyn_cfg.get('sl_floor', 0.02),
            sl_ceiling=dyn_cfg.get('sl_ceiling', 0.07),
            tp_floor=dyn_cfg.get('tp_floor', 0.04),
            tp_ceiling=dyn_cfg.get('tp_ceiling', 0.15),
        )
        return (sl, tp, source)

    # 1) Cycle cache hit — cheapest path.
    if cycle_atr_cache and symbol in cycle_atr_cache:
        cached = cycle_atr_cache.get(symbol)
        if cached is not None:
            return _finalize(cached, 'atr_cache')

    # 2) Live compute from provided klines (crypto/stock-agnostic).
    if daily_klines and len(daily_klines) >= atr_period + 1 and current_price:
        try:
            highs = [k['high'] for k in daily_klines]
            lows = [k['low'] for k in daily_klines]
            closes = [k['close'] for k in daily_klines]
            atr_val = calculate_atr(highs, lows, closes, period=atr_period)
            atr_pct = atr_val / current_price if atr_val else None
            return _finalize(atr_pct, 'atr_live')
        except Exception as e:
            log.debug("resolve_sl_tp_for_entry live-ATR failed for %s: %s", symbol, e)

    # 3) Static config fallback — guaranteed non-None result.
    return _finalize(None, 'config_fallback')
