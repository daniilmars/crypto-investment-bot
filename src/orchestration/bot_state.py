"""Centralized bot state — replaces 6 module-level dicts from main.py.

Assumes single asyncio event loop. No thread safety provided.
Concurrency safety: _sell_lock guards sell operations since both
run_bot_cycle() and execute_confirmed_signal() can sell positions
and interleave at await points.
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

# --- Trailing Stop-Loss State ---
_trailing_stop_peaks: dict[str, float] = {}
_auto_trailing_stop_peaks: dict[str, float] = {}

# --- Stop-Loss Cooldown State ---
_stoploss_cooldowns: dict = {}
_auto_stoploss_cooldowns: dict = {}

# --- Signal Cooldown State ---
_signal_cooldowns: dict[str, 'datetime'] = {}    # key: "symbol:signal_type"
_auto_signal_cooldowns: dict[str, 'datetime'] = {}

# --- Position Analyst Cooldown ---
_analyst_last_run: dict = {}
_auto_analyst_last_run: dict = {}

# --- Flash Analyst Cooldown ---
_flash_analyst_last_run: dict = {}
_auto_flash_analyst_last_run: dict = {}

# --- Rotation Cooldown (per asset_type) ---
_rotation_cooldowns: dict[str, 'datetime'] = {}     # key: asset_type
_auto_rotation_cooldowns: dict[str, 'datetime'] = {}

# --- Last Cycle Timestamp (hung task detection) ---
_last_cycle_at: datetime | None = None

# --- Sell Lock (guards concurrent sell attempts) ---
_sell_lock = asyncio.Lock()


def set_last_cycle_at(dt: datetime):
    global _last_cycle_at
    _last_cycle_at = dt


def get_last_cycle_at() -> datetime | None:
    return _last_cycle_at


def get_sell_lock() -> asyncio.Lock:
    return _sell_lock


# --- Trailing Stop Peaks ---

def update_trailing_stop(order_id: str, current_price: float) -> float:
    prev_peak = _trailing_stop_peaks.get(order_id, current_price)
    new_peak = max(prev_peak, current_price)
    if new_peak > prev_peak:
        _trailing_stop_peaks[order_id] = new_peak
        try:
            _db_save_peak(order_id, new_peak)
        except Exception as e:
            log.warning(f"Failed to persist trailing stop peak for {order_id}: {e}")
    elif order_id not in _trailing_stop_peaks:
        _trailing_stop_peaks[order_id] = new_peak
    return new_peak


def clear_trailing_stop(order_id: str):
    _trailing_stop_peaks.pop(order_id, None)


def get_peak(order_id: str) -> float | None:
    return _trailing_stop_peaks.get(order_id)


def auto_update_trailing_stop(order_id: str, current_price: float) -> float:
    prev_peak = _auto_trailing_stop_peaks.get(order_id, current_price)
    new_peak = max(prev_peak, current_price)
    if new_peak > prev_peak:
        _auto_trailing_stop_peaks[order_id] = new_peak
        try:
            _db_save_peak(order_id, new_peak)
        except Exception as e:
            log.warning(f"Failed to persist auto trailing stop peak for {order_id}: {e}")
    elif order_id not in _auto_trailing_stop_peaks:
        _auto_trailing_stop_peaks[order_id] = new_peak
    return new_peak


def get_auto_peak(order_id: str) -> float | None:
    return _auto_trailing_stop_peaks.get(order_id)


def auto_clear_trailing_stop(order_id: str):
    _auto_trailing_stop_peaks.pop(order_id, None)


# --- Stop-Loss Cooldowns ---

def get_stoploss_cooldown(symbol: str):
    return _stoploss_cooldowns.get(symbol)


def set_stoploss_cooldown(symbol: str, expires_at):
    _stoploss_cooldowns[symbol] = expires_at


def remove_stoploss_cooldown(symbol: str):
    _stoploss_cooldowns.pop(symbol, None)


def get_all_stoploss_cooldowns() -> dict:
    return _stoploss_cooldowns


def get_auto_stoploss_cooldown(symbol: str):
    return _auto_stoploss_cooldowns.get(symbol)


def set_auto_stoploss_cooldown(symbol: str, expires_at):
    _auto_stoploss_cooldowns[symbol] = expires_at


def remove_auto_stoploss_cooldown(symbol: str):
    _auto_stoploss_cooldowns.pop(symbol, None)


# --- Signal Cooldowns ---

def get_signal_cooldown(symbol: str, signal_type: str):
    return _signal_cooldowns.get(f"{symbol}:{signal_type}")


def set_signal_cooldown(symbol: str, signal_type: str, expires_at):
    _signal_cooldowns[f"{symbol}:{signal_type}"] = expires_at
    try:
        _db_save_signal_cd(symbol, signal_type, expires_at, False)
    except Exception as e:
        log.warning(f"Failed to persist signal cooldown for {symbol}:{signal_type}: {e}")


def remove_signal_cooldown(symbol: str, signal_type: str):
    _signal_cooldowns.pop(f"{symbol}:{signal_type}", None)
    try:
        _db_clear_signal_cd(symbol, signal_type, False)
    except Exception as e:
        log.warning(f"Failed to clear signal cooldown for {symbol}:{signal_type}: {e}")


def get_auto_signal_cooldown(symbol: str, signal_type: str):
    return _auto_signal_cooldowns.get(f"{symbol}:{signal_type}")


def set_auto_signal_cooldown(symbol: str, signal_type: str, expires_at):
    _auto_signal_cooldowns[f"{symbol}:{signal_type}"] = expires_at
    try:
        _db_save_signal_cd(symbol, signal_type, expires_at, True)
    except Exception as e:
        log.warning(f"Failed to persist auto signal cooldown for {symbol}:{signal_type}: {e}")


def remove_auto_signal_cooldown(symbol: str, signal_type: str):
    _auto_signal_cooldowns.pop(f"{symbol}:{signal_type}", None)
    try:
        _db_clear_signal_cd(symbol, signal_type, True)
    except Exception as e:
        log.warning(f"Failed to clear auto signal cooldown for {symbol}:{signal_type}: {e}")


# --- Analyst Cooldowns ---

def get_analyst_last_run(order_id: str):
    return _analyst_last_run.get(order_id)


def set_analyst_last_run(order_id: str, timestamp):
    _analyst_last_run[order_id] = timestamp


def remove_analyst_last_run(order_id: str):
    _analyst_last_run.pop(order_id, None)


def get_auto_analyst_last_run(order_id: str):
    return _auto_analyst_last_run.get(order_id)


def set_auto_analyst_last_run(order_id: str, timestamp):
    _auto_analyst_last_run[order_id] = timestamp


def remove_auto_analyst_last_run(order_id: str):
    _auto_analyst_last_run.pop(order_id, None)


# --- Flash Analyst Cooldowns ---

def get_flash_analyst_last_run(order_id: str):
    return _flash_analyst_last_run.get(order_id)


def set_flash_analyst_last_run(order_id: str, timestamp):
    _flash_analyst_last_run[order_id] = timestamp


def remove_flash_analyst_last_run(order_id: str):
    _flash_analyst_last_run.pop(order_id, None)


def get_auto_flash_analyst_last_run(order_id: str):
    return _auto_flash_analyst_last_run.get(order_id)


def set_auto_flash_analyst_last_run(order_id: str, timestamp):
    _auto_flash_analyst_last_run[order_id] = timestamp


def remove_auto_flash_analyst_last_run(order_id: str):
    _auto_flash_analyst_last_run.pop(order_id, None)


# --- Rotation Cooldowns ---

def get_rotation_cooldown(asset_type: str, is_auto: bool = False):
    store = _auto_rotation_cooldowns if is_auto else _rotation_cooldowns
    return store.get(asset_type)


def set_rotation_cooldown(asset_type: str, expires_at, is_auto: bool = False):
    store = _auto_rotation_cooldowns if is_auto else _rotation_cooldowns
    store[asset_type] = expires_at


def clear_rotation_cooldown(asset_type: str, is_auto: bool = False):
    store = _auto_rotation_cooldowns if is_auto else _rotation_cooldowns
    store.pop(asset_type, None)


# --- Bulk operations ---

def load_peaks(peaks: dict):
    _trailing_stop_peaks.update(peaks)


def load_auto_peaks(peaks: dict):
    _auto_trailing_stop_peaks.update(peaks)


def load_cooldowns(cooldowns: dict):
    _stoploss_cooldowns.update(cooldowns)


def load_signal_cooldown_state(manual: dict, auto: dict):
    _signal_cooldowns.update(manual)
    _auto_signal_cooldowns.update(auto)


def clear_all():
    """Clears all state. For tests."""
    global _last_cycle_at
    _trailing_stop_peaks.clear()
    _auto_trailing_stop_peaks.clear()
    _stoploss_cooldowns.clear()
    _auto_stoploss_cooldowns.clear()
    _signal_cooldowns.clear()
    _auto_signal_cooldowns.clear()
    _analyst_last_run.clear()
    _auto_analyst_last_run.clear()
    _flash_analyst_last_run.clear()
    _auto_flash_analyst_last_run.clear()
    _rotation_cooldowns.clear()
    _auto_rotation_cooldowns.clear()
    _last_cycle_at = None
