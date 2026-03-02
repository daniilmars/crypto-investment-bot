"""Tests for stoploss cooldown persistence (W4)."""

from datetime import datetime, timedelta, timezone

import pytest

from src.database import (
    initialize_database,
    save_stoploss_cooldown,
    load_stoploss_cooldowns,
    clear_stoploss_cooldown,
)


@pytest.fixture(autouse=True)
def _init_db():
    """Ensure database tables exist before each test."""
    initialize_database()


class TestStoplossCooldownPersistence:

    def test_save_and_load_roundtrip(self):
        """Saved cooldown can be loaded back."""
        expires = datetime.now(timezone.utc) + timedelta(hours=6)
        save_stoploss_cooldown.sync('BTC', expires)

        loaded = load_stoploss_cooldowns.sync()
        assert 'BTC' in loaded
        # Allow small time difference due to DB storage precision
        diff = abs((loaded['BTC'] - expires).total_seconds())
        assert diff < 2.0

    def test_expired_not_returned(self):
        """Expired cooldowns are not returned by load."""
        expired = datetime.now(timezone.utc) - timedelta(hours=1)
        save_stoploss_cooldown.sync('EXPIRED_SYM', expired)

        loaded = load_stoploss_cooldowns.sync()
        assert 'EXPIRED_SYM' not in loaded

    def test_clear_removes_cooldown(self):
        """Clearing a cooldown removes it from the database."""
        expires = datetime.now(timezone.utc) + timedelta(hours=6)
        save_stoploss_cooldown.sync('ETH', expires)

        # Verify it's there
        loaded = load_stoploss_cooldowns.sync()
        assert 'ETH' in loaded

        # Clear and verify it's gone
        clear_stoploss_cooldown.sync('ETH')
        loaded = load_stoploss_cooldowns.sync()
        assert 'ETH' not in loaded
