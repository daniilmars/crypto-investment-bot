# tests/test_signal_cooldown.py

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from src.orchestration import bot_state
from src.orchestration.pre_trade_gates import check_signal_cooldown


def run_async(coro):
    """Helper to run async functions in sync tests."""
    return asyncio.run(coro)


@pytest.fixture(autouse=True)
def clear_state():
    """Clear bot state before and after each test."""
    bot_state.clear_all()
    yield
    bot_state.clear_all()


# --- bot_state accessor tests ---

class TestSignalCooldownState:
    def test_get_returns_none_when_not_set(self):
        assert bot_state.get_signal_cooldown("BTC", "BUY") is None

    def test_set_and_get(self):
        expires = datetime.now(timezone.utc) + timedelta(hours=4)
        bot_state.set_signal_cooldown("BTC", "BUY", expires)
        assert bot_state.get_signal_cooldown("BTC", "BUY") == expires

    def test_different_signal_types_are_independent(self):
        buy_expires = datetime.now(timezone.utc) + timedelta(hours=4)
        sell_expires = datetime.now(timezone.utc) + timedelta(hours=2)
        bot_state.set_signal_cooldown("BTC", "BUY", buy_expires)
        bot_state.set_signal_cooldown("BTC", "SELL", sell_expires)
        assert bot_state.get_signal_cooldown("BTC", "BUY") == buy_expires
        assert bot_state.get_signal_cooldown("BTC", "SELL") == sell_expires

    def test_different_symbols_are_independent(self):
        expires = datetime.now(timezone.utc) + timedelta(hours=4)
        bot_state.set_signal_cooldown("BTC", "BUY", expires)
        assert bot_state.get_signal_cooldown("ETH", "BUY") is None

    def test_remove(self):
        expires = datetime.now(timezone.utc) + timedelta(hours=4)
        bot_state.set_signal_cooldown("BTC", "BUY", expires)
        bot_state.remove_signal_cooldown("BTC", "BUY")
        assert bot_state.get_signal_cooldown("BTC", "BUY") is None

    def test_remove_nonexistent_is_safe(self):
        bot_state.remove_signal_cooldown("NONEXISTENT", "BUY")  # no error

    def test_clear_all_clears_signal_cooldowns(self):
        bot_state.set_signal_cooldown("BTC", "BUY",
                                       datetime.now(timezone.utc) + timedelta(hours=4))
        bot_state.set_auto_signal_cooldown("ETH", "SELL",
                                            datetime.now(timezone.utc) + timedelta(hours=2))
        bot_state.clear_all()
        assert bot_state.get_signal_cooldown("BTC", "BUY") is None
        assert bot_state.get_auto_signal_cooldown("ETH", "SELL") is None


class TestAutoSignalCooldownState:
    def test_auto_get_returns_none_when_not_set(self):
        assert bot_state.get_auto_signal_cooldown("BTC", "BUY") is None

    def test_auto_set_and_get(self):
        expires = datetime.now(timezone.utc) + timedelta(hours=4)
        bot_state.set_auto_signal_cooldown("BTC", "BUY", expires)
        assert bot_state.get_auto_signal_cooldown("BTC", "BUY") == expires

    def test_auto_remove(self):
        expires = datetime.now(timezone.utc) + timedelta(hours=4)
        bot_state.set_auto_signal_cooldown("BTC", "BUY", expires)
        bot_state.remove_auto_signal_cooldown("BTC", "BUY")
        assert bot_state.get_auto_signal_cooldown("BTC", "BUY") is None

    def test_manual_and_auto_are_independent(self):
        expires = datetime.now(timezone.utc) + timedelta(hours=4)
        bot_state.set_signal_cooldown("BTC", "BUY", expires)
        assert bot_state.get_auto_signal_cooldown("BTC", "BUY") is None


# --- check_signal_cooldown gate tests ---

class TestCheckSignalCooldown:
    def test_hold_signal_never_blocked(self):
        assert run_async(check_signal_cooldown("BTC", "HOLD", 4)) is False

    def test_increase_signal_can_be_blocked(self):
        bot_state.set_signal_cooldown(
            "BTC", "INCREASE",
            datetime.now(timezone.utc) + timedelta(hours=4))
        assert run_async(check_signal_cooldown("BTC", "INCREASE", 4)) is True

    def test_no_cooldown_allows_signal(self):
        assert run_async(check_signal_cooldown("BTC", "BUY", 4)) is False

    def test_active_cooldown_blocks_signal(self):
        bot_state.set_signal_cooldown(
            "BTC", "BUY",
            datetime.now(timezone.utc) + timedelta(hours=4))
        assert run_async(check_signal_cooldown("BTC", "BUY", 4)) is True

    def test_expired_cooldown_allows_signal(self):
        bot_state.set_signal_cooldown(
            "BTC", "BUY",
            datetime.now(timezone.utc) - timedelta(minutes=1))
        assert run_async(check_signal_cooldown("BTC", "BUY", 4)) is False
        # Expired cooldown should be cleaned up
        assert bot_state.get_signal_cooldown("BTC", "BUY") is None

    def test_cooldown_for_one_type_doesnt_block_other(self):
        bot_state.set_signal_cooldown(
            "BTC", "BUY",
            datetime.now(timezone.utc) + timedelta(hours=4))
        assert run_async(check_signal_cooldown("BTC", "SELL", 4)) is False

    def test_auto_cooldown_blocks_auto_signal(self):
        bot_state.set_auto_signal_cooldown(
            "ETH", "SELL",
            datetime.now(timezone.utc) + timedelta(hours=2))
        assert run_async(check_signal_cooldown("ETH", "SELL", 4, is_auto=True)) is True

    def test_auto_expired_cooldown_allows_signal(self):
        bot_state.set_auto_signal_cooldown(
            "ETH", "SELL",
            datetime.now(timezone.utc) - timedelta(minutes=1))
        assert run_async(check_signal_cooldown("ETH", "SELL", 4, is_auto=True)) is False
        assert bot_state.get_auto_signal_cooldown("ETH", "SELL") is None

    def test_manual_cooldown_doesnt_affect_auto(self):
        bot_state.set_signal_cooldown(
            "BTC", "BUY",
            datetime.now(timezone.utc) + timedelta(hours=4))
        assert run_async(check_signal_cooldown("BTC", "BUY", 4, is_auto=True)) is False
