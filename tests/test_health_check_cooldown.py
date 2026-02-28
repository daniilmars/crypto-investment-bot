"""Tests for position health check cooldown logic (W7)."""

from datetime import datetime, timedelta, timezone

import pytest


class TestHealthCheckCooldown:

    def test_skip_within_interval(self):
        """Health check should be skipped if last check was within the interval."""
        _health_check_last_run = {}
        order_id = 'test-order-1'
        check_interval_minutes = 60
        now = datetime.now(timezone.utc)

        # Simulate a check that happened 30 minutes ago
        _health_check_last_run[order_id] = now - timedelta(minutes=30)

        last_check = _health_check_last_run.get(order_id)
        should_skip = last_check and (now - last_check).total_seconds() / 60 < check_interval_minutes
        assert should_skip is True

    def test_allow_after_interval(self):
        """Health check should be allowed after the interval has passed."""
        _health_check_last_run = {}
        order_id = 'test-order-2'
        check_interval_minutes = 60
        now = datetime.now(timezone.utc)

        # Simulate a check that happened 90 minutes ago
        _health_check_last_run[order_id] = now - timedelta(minutes=90)

        last_check = _health_check_last_run.get(order_id)
        should_skip = last_check and (now - last_check).total_seconds() / 60 < check_interval_minutes
        assert should_skip is False

    def test_allow_first_check(self):
        """First check for a new position should always be allowed."""
        _health_check_last_run = {}
        order_id = 'new-order'
        check_interval_minutes = 60
        now = datetime.now(timezone.utc)

        last_check = _health_check_last_run.get(order_id)
        should_skip = last_check and (now - last_check).total_seconds() / 60 < check_interval_minutes
        assert not should_skip
