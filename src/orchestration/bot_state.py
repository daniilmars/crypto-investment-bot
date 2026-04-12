"""Centralized bot state — replaces 6 module-level dicts from main.py.

Assumes single asyncio event loop. No thread safety provided.
Concurrency safety: _sell_lock guards sell operations since both
run_bot_cycle() and execute_confirmed_signal() can sell positions
and interleave at await points.

State is keyed by strategy name ('manual', 'auto', 'momentum', 'conservative', etc.)
to support N parallel trading strategies. Legacy auto_*/manual functions are thin
wrappers for backward compatibility.
"""

import asyncio
from datetime import datetime

from src.logger import log
from src.database import save_trailing_stop_peak as _async_save_peak
from src.database import save_signal_cooldown as _async_save_signal_cd
from src.database import clear_signal_cooldown as _async_clear_signal_cd

# Use the sync version since bot_state functions are called from sync contexts
_db_save_peak = _async_save_peak.sync
_db_save_signal_cd = _async_save_signal_cd.sync
_db_clear_signal_cd = _async_clear_signal_cd.sync

# --- Strategy-Keyed State Dicts ---
# Each maps strategy_name -> {key: value}
_strategy_trailing_peaks: dict[str, dict[str, float]] = {}
_strategy_stoploss_cds: dict[str, dict] = {}
_strategy_signal_cds: dict[str, dict[str, datetime]] = {}
_strategy_analyst_runs: dict[str, dict] = {}
_strategy_flash_runs: dict[str, dict] = {}
_strategy_rotation_cds: dict[str, dict[str, datetime]] = {}
_strategy_streak_state: dict[str, dict] = {}

# --- Last Cycle Timestamp (hung task detection) ---
_last_cycle_at: datetime | None = None

# --- Sell Lock (guards concurrent sell attempts) ---
_sell_lock = asyncio.Lock()


def _get_store(store: dict, strategy: str) -> dict:
    """Get or create the sub-dict for a strategy."""
    if strategy not in store:
        store[strategy] = {}
    return store[strategy]


def set_last_cycle_at(dt: datetime):
    global _last_cycle_at
    _last_cycle_at = dt


def get_last_cycle_at() -> datetime | None:
    return _last_cycle_at


def get_sell_lock() -> asyncio.Lock:
    return _sell_lock


# ============================================================
# Trailing Stop Peaks — generic strategy-keyed API
# ============================================================

def strategy_update_trailing_stop(order_id: str, current_price: float,
                                  strategy: str = 'manual') -> float:
    store = _get_store(_strategy_trailing_peaks, strategy)
    prev_peak = store.get(order_id, current_price)
    new_peak = max(prev_peak, current_price)
    if new_peak > prev_peak:
        store[order_id] = new_peak
        try:
            _db_save_peak(order_id, new_peak)
        except Exception as e:
            log.warning(f"Failed to persist trailing stop peak for {order_id}: {e}")
    elif order_id not in store:
        store[order_id] = new_peak
    return new_peak


def strategy_get_peak(order_id: str, strategy: str = 'manual') -> float | None:
    return _get_store(_strategy_trailing_peaks, strategy).get(order_id)


def strategy_clear_trailing_stop(order_id: str, strategy: str = 'manual'):
    _get_store(_strategy_trailing_peaks, strategy).pop(order_id, None)


def strategy_load_peaks(peaks: dict, strategy: str = 'manual'):
    _get_store(_strategy_trailing_peaks, strategy).update(peaks)


# --- Legacy wrappers (manual) ---

def update_trailing_stop(order_id: str, current_price: float) -> float:
    return strategy_update_trailing_stop(order_id, current_price, 'manual')


def clear_trailing_stop(order_id: str):
    strategy_clear_trailing_stop(order_id, 'manual')


def get_peak(order_id: str) -> float | None:
    return strategy_get_peak(order_id, 'manual')


def load_peaks(peaks: dict):
    strategy_load_peaks(peaks, 'manual')


# --- Legacy wrappers (auto) ---

def auto_update_trailing_stop(order_id: str, current_price: float) -> float:
    return strategy_update_trailing_stop(order_id, current_price, 'auto')


def get_auto_peak(order_id: str) -> float | None:
    return strategy_get_peak(order_id, 'auto')


def auto_clear_trailing_stop(order_id: str):
    strategy_clear_trailing_stop(order_id, 'auto')


def load_auto_peaks(peaks: dict):
    strategy_load_peaks(peaks, 'auto')


# ============================================================
# Stop-Loss Cooldowns — generic strategy-keyed API
# ============================================================

def strategy_get_stoploss_cooldown(symbol: str, strategy: str = 'manual'):
    return _get_store(_strategy_stoploss_cds, strategy).get(symbol)


def strategy_set_stoploss_cooldown(symbol: str, expires_at, strategy: str = 'manual'):
    _get_store(_strategy_stoploss_cds, strategy)[symbol] = expires_at


def strategy_remove_stoploss_cooldown(symbol: str, strategy: str = 'manual'):
    _get_store(_strategy_stoploss_cds, strategy).pop(symbol, None)


def strategy_get_all_stoploss_cooldowns(strategy: str = 'manual') -> dict:
    return _get_store(_strategy_stoploss_cds, strategy)


# --- Legacy wrappers ---

def get_stoploss_cooldown(symbol: str):
    return strategy_get_stoploss_cooldown(symbol, 'manual')


def set_stoploss_cooldown(symbol: str, expires_at):
    strategy_set_stoploss_cooldown(symbol, expires_at, 'manual')


def remove_stoploss_cooldown(symbol: str):
    strategy_remove_stoploss_cooldown(symbol, 'manual')


def get_all_stoploss_cooldowns() -> dict:
    return strategy_get_all_stoploss_cooldowns('manual')


def get_auto_stoploss_cooldown(symbol: str):
    return strategy_get_stoploss_cooldown(symbol, 'auto')


def set_auto_stoploss_cooldown(symbol: str, expires_at):
    strategy_set_stoploss_cooldown(symbol, expires_at, 'auto')


def remove_auto_stoploss_cooldown(symbol: str):
    strategy_remove_stoploss_cooldown(symbol, 'auto')


# ============================================================
# Signal Cooldowns — generic strategy-keyed API
# ============================================================

def strategy_get_signal_cooldown(symbol: str, signal_type: str,
                                 strategy: str = 'manual'):
    return _get_store(_strategy_signal_cds, strategy).get(f"{symbol}:{signal_type}")


def strategy_set_signal_cooldown(symbol: str, signal_type: str, expires_at,
                                 strategy: str = 'manual'):
    _get_store(_strategy_signal_cds, strategy)[f"{symbol}:{signal_type}"] = expires_at
    is_auto = strategy != 'manual'
    try:
        _db_save_signal_cd(symbol, signal_type, expires_at, is_auto)
    except Exception as e:
        log.warning(f"Failed to persist signal cooldown for {symbol}:{signal_type} ({strategy}): {e}")


def strategy_remove_signal_cooldown(symbol: str, signal_type: str,
                                    strategy: str = 'manual'):
    _get_store(_strategy_signal_cds, strategy).pop(f"{symbol}:{signal_type}", None)
    is_auto = strategy != 'manual'
    try:
        _db_clear_signal_cd(symbol, signal_type, is_auto)
    except Exception as e:
        log.warning(f"Failed to clear signal cooldown for {symbol}:{signal_type} ({strategy}): {e}")


# --- Legacy wrappers ---

def get_signal_cooldown(symbol: str, signal_type: str):
    return strategy_get_signal_cooldown(symbol, signal_type, 'manual')


def set_signal_cooldown(symbol: str, signal_type: str, expires_at):
    strategy_set_signal_cooldown(symbol, signal_type, expires_at, 'manual')


def remove_signal_cooldown(symbol: str, signal_type: str):
    strategy_remove_signal_cooldown(symbol, signal_type, 'manual')


def get_auto_signal_cooldown(symbol: str, signal_type: str):
    return strategy_get_signal_cooldown(symbol, signal_type, 'auto')


def set_auto_signal_cooldown(symbol: str, signal_type: str, expires_at):
    strategy_set_signal_cooldown(symbol, signal_type, expires_at, 'auto')


def remove_auto_signal_cooldown(symbol: str, signal_type: str):
    strategy_remove_signal_cooldown(symbol, signal_type, 'auto')


# ============================================================
# Analyst Cooldowns — generic strategy-keyed API
# ============================================================

def strategy_get_analyst_last_run(order_id: str, strategy: str = 'manual'):
    return _get_store(_strategy_analyst_runs, strategy).get(order_id)


def strategy_set_analyst_last_run(order_id: str, timestamp, strategy: str = 'manual'):
    _get_store(_strategy_analyst_runs, strategy)[order_id] = timestamp


def strategy_remove_analyst_last_run(order_id: str, strategy: str = 'manual'):
    _get_store(_strategy_analyst_runs, strategy).pop(order_id, None)


# --- Legacy wrappers ---

def get_analyst_last_run(order_id: str):
    return strategy_get_analyst_last_run(order_id, 'manual')


def set_analyst_last_run(order_id: str, timestamp):
    strategy_set_analyst_last_run(order_id, timestamp, 'manual')


def remove_analyst_last_run(order_id: str):
    strategy_remove_analyst_last_run(order_id, 'manual')


def get_auto_analyst_last_run(order_id: str):
    return strategy_get_analyst_last_run(order_id, 'auto')


def set_auto_analyst_last_run(order_id: str, timestamp):
    strategy_set_analyst_last_run(order_id, timestamp, 'auto')


def remove_auto_analyst_last_run(order_id: str):
    strategy_remove_analyst_last_run(order_id, 'auto')


# ============================================================
# Flash Analyst Cooldowns — generic strategy-keyed API
# ============================================================

def strategy_get_flash_analyst_last_run(order_id: str, strategy: str = 'manual'):
    return _get_store(_strategy_flash_runs, strategy).get(order_id)


def strategy_set_flash_analyst_last_run(order_id: str, timestamp,
                                        strategy: str = 'manual'):
    _get_store(_strategy_flash_runs, strategy)[order_id] = timestamp


def strategy_remove_flash_analyst_last_run(order_id: str, strategy: str = 'manual'):
    _get_store(_strategy_flash_runs, strategy).pop(order_id, None)


# --- Legacy wrappers ---

def get_flash_analyst_last_run(order_id: str):
    return strategy_get_flash_analyst_last_run(order_id, 'manual')


def set_flash_analyst_last_run(order_id: str, timestamp):
    strategy_set_flash_analyst_last_run(order_id, timestamp, 'manual')


def remove_flash_analyst_last_run(order_id: str):
    strategy_remove_flash_analyst_last_run(order_id, 'manual')


def get_auto_flash_analyst_last_run(order_id: str):
    return strategy_get_flash_analyst_last_run(order_id, 'auto')


def set_auto_flash_analyst_last_run(order_id: str, timestamp):
    strategy_set_flash_analyst_last_run(order_id, timestamp, 'auto')


def remove_auto_flash_analyst_last_run(order_id: str):
    strategy_remove_flash_analyst_last_run(order_id, 'auto')


# ============================================================
# Rotation Cooldowns — generic strategy-keyed API
# ============================================================

def strategy_get_rotation_cooldown(asset_type: str, strategy: str = 'manual'):
    return _get_store(_strategy_rotation_cds, strategy).get(asset_type)


def strategy_set_rotation_cooldown(asset_type: str, expires_at,
                                   strategy: str = 'manual'):
    _get_store(_strategy_rotation_cds, strategy)[asset_type] = expires_at


def strategy_clear_rotation_cooldown(asset_type: str, strategy: str = 'manual'):
    _get_store(_strategy_rotation_cds, strategy).pop(asset_type, None)


# --- Legacy wrappers ---

def get_rotation_cooldown(asset_type: str, is_auto: bool = False):
    strategy = 'auto' if is_auto else 'manual'
    return strategy_get_rotation_cooldown(asset_type, strategy)


def set_rotation_cooldown(asset_type: str, expires_at, is_auto: bool = False):
    strategy = 'auto' if is_auto else 'manual'
    strategy_set_rotation_cooldown(asset_type, expires_at, strategy)


def clear_rotation_cooldown(asset_type: str, is_auto: bool = False):
    strategy = 'auto' if is_auto else 'manual'
    strategy_clear_rotation_cooldown(asset_type, strategy)


# ============================================================
# Bulk Operations
# ============================================================

def load_cooldowns(cooldowns: dict):
    _get_store(_strategy_stoploss_cds, 'manual').update(cooldowns)


def load_signal_cooldown_state(manual: dict, auto: dict):
    _get_store(_strategy_signal_cds, 'manual').update(manual)
    _get_store(_strategy_signal_cds, 'auto').update(auto)


# --- Streak-Based Position Sizing ---

def strategy_get_streak_state(strategy: str) -> dict:
    """Returns {'consecutive_wins': int, 'in_defensive_mode': bool}."""
    state = _strategy_streak_state.get(strategy, {})
    return {
        'consecutive_wins': state.get('consecutive_wins', 0),
        'in_defensive_mode': state.get('in_defensive_mode', False),
    }


def strategy_record_trade_outcome(strategy: str, is_win: bool,
                                  min_streak_for_defense: int = 3):
    """Update streak counter after a trade closes."""
    state = _strategy_streak_state.get(strategy, {
        'consecutive_wins': 0, 'in_defensive_mode': False})

    if is_win:
        state['consecutive_wins'] = state.get('consecutive_wins', 0) + 1
        state['in_defensive_mode'] = False
    else:
        was_in_streak = state.get('consecutive_wins', 0) >= min_streak_for_defense
        state['in_defensive_mode'] = was_in_streak
        state['consecutive_wins'] = 0

    _strategy_streak_state[strategy] = state
    _persist_streak_state(strategy, state)


def strategy_get_streak_multiplier(strategy: str, config: dict) -> float:
    """Compute position size multiplier from streak state and config."""
    if not config.get('enabled', False):
        return 1.0
    state = strategy_get_streak_state(strategy)
    if state.get('in_defensive_mode'):
        return config.get('defense_multiplier', 0.7)
    min_wins = config.get('min_consecutive_wins', 3)
    if state.get('consecutive_wins', 0) >= min_wins:
        boost = config.get('boost_multiplier', 1.3)
        cap = config.get('max_boost_multiplier', 1.5)
        return min(boost, cap)
    return 1.0


def strategy_consume_defensive_mode(strategy: str):
    """Clear defensive mode after one trade. Called after a BUY executes."""
    state = _strategy_streak_state.get(strategy)
    if state and state.get('in_defensive_mode'):
        state['in_defensive_mode'] = False
        _persist_streak_state(strategy, state)


def strategy_load_streak_state(strategy: str, state: dict):
    """Load streak state from DB on startup."""
    _strategy_streak_state[strategy] = state


def _persist_streak_state(strategy: str, state: dict):
    """Best-effort persist to bot_state_kv."""
    try:
        import json
        from src.database import save_bot_state
        save_bot_state(f'streak_state:{strategy}', json.dumps(state))
    except Exception:
        pass


def clear_all():
    """Clears all state. For tests.

    Clears each strategy sub-dict in place (rather than clearing the parent)
    so that module-level aliases like _trailing_stop_peaks remain valid.
    """
    global _last_cycle_at
    for store in (_strategy_trailing_peaks, _strategy_stoploss_cds,
                  _strategy_signal_cds, _strategy_analyst_runs,
                  _strategy_flash_runs, _strategy_rotation_cds,
                  _strategy_streak_state):
        for sub_dict in store.values():
            sub_dict.clear()
    _last_cycle_at = None


# ============================================================
# Backward-compatible dict aliases (used by tests that import internals)
# ============================================================
# These are live references to the strategy sub-dicts, so direct
# mutations (e.g. _trailing_stop_peaks['x'] = 1) work correctly.

_trailing_stop_peaks = _get_store(_strategy_trailing_peaks, 'manual')
_auto_trailing_stop_peaks = _get_store(_strategy_trailing_peaks, 'auto')
_stoploss_cooldowns = _get_store(_strategy_stoploss_cds, 'manual')
_auto_stoploss_cooldowns = _get_store(_strategy_stoploss_cds, 'auto')
_signal_cooldowns = _get_store(_strategy_signal_cds, 'manual')
_auto_signal_cooldowns = _get_store(_strategy_signal_cds, 'auto')
_analyst_last_run = _get_store(_strategy_analyst_runs, 'manual')
_auto_analyst_last_run = _get_store(_strategy_analyst_runs, 'auto')
_flash_analyst_last_run = _get_store(_strategy_flash_runs, 'manual')
_auto_flash_analyst_last_run = _get_store(_strategy_flash_runs, 'auto')
_rotation_cooldowns = _get_store(_strategy_rotation_cds, 'manual')
_auto_rotation_cooldowns = _get_store(_strategy_rotation_cds, 'auto')
