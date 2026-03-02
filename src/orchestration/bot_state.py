"""Centralized bot state — replaces 6 module-level dicts from main.py.

Assumes single asyncio event loop. No thread safety provided.
Concurrency safety: _sell_lock guards sell operations since both
run_bot_cycle() and execute_confirmed_signal() can sell positions
and interleave at await points.
"""

import asyncio
from src.logger import log
from src.database import save_trailing_stop_peak as _async_save_peak

# Use the sync version since bot_state functions are called from sync contexts
_db_save_peak = _async_save_peak.sync

# --- Trailing Stop-Loss State ---
_trailing_stop_peaks: dict[str, float] = {}
_auto_trailing_stop_peaks: dict[str, float] = {}

# --- Stop-Loss Cooldown State ---
_stoploss_cooldowns: dict = {}
_auto_stoploss_cooldowns: dict = {}

# --- Position Analyst Cooldown ---
_analyst_last_run: dict = {}
_auto_analyst_last_run: dict = {}

# --- Sell Lock (guards concurrent sell attempts) ---
_sell_lock = asyncio.Lock()


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
    _auto_trailing_stop_peaks[order_id] = new_peak
    return new_peak


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


# --- Bulk operations ---

def load_peaks(peaks: dict):
    _trailing_stop_peaks.update(peaks)


def load_cooldowns(cooldowns: dict):
    _stoploss_cooldowns.update(cooldowns)


def clear_all():
    """Clears all state. For tests."""
    _trailing_stop_peaks.clear()
    _auto_trailing_stop_peaks.clear()
    _stoploss_cooldowns.clear()
    _auto_stoploss_cooldowns.clear()
    _analyst_last_run.clear()
    _auto_analyst_last_run.clear()
