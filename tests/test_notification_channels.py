"""Tests for the notifications-channel guard system.

Verifies that ``should_send(channel)`` reads ``settings.notifications.<channel>``
and that the new defaults in settings.yaml are correct (background = off,
trade/critical = on).
"""

import pytest
from unittest.mock import patch


# --- should_send helper ---

def test_should_send_default_true_for_unknown_channel():
    """Unknown channels default to True so adding a new alert site never
    silently breaks."""
    from src.notify.telegram_bot import should_send
    with patch("src.notify.telegram_bot.app_config",
               {"settings": {"notifications": {}}}):
        assert should_send("brand_new_channel_never_seen") is True


def test_should_send_default_override():
    """default=False is honored when the channel isn't in config."""
    from src.notify.telegram_bot import should_send
    with patch("src.notify.telegram_bot.app_config",
               {"settings": {"notifications": {}}}):
        assert should_send("brand_new", default=False) is False


def test_should_send_reads_explicit_false():
    from src.notify.telegram_bot import should_send
    with patch("src.notify.telegram_bot.app_config",
               {"settings": {"notifications": {"foo": False}}}):
        assert should_send("foo") is False


def test_should_send_reads_explicit_true():
    from src.notify.telegram_bot import should_send
    with patch("src.notify.telegram_bot.app_config",
               {"settings": {"notifications": {"foo": True}}}):
        assert should_send("foo") is True


def test_should_send_handles_missing_settings_block():
    """No notifications block at all → use the default param."""
    from src.notify.telegram_bot import should_send
    with patch("src.notify.telegram_bot.app_config", {"settings": {}}):
        assert should_send("anything", default=False) is False
        assert should_send("anything", default=True) is True


def test_should_send_coerces_truthy():
    """Non-bool values get bool-coerced (defensive against config typos)."""
    from src.notify.telegram_bot import should_send
    with patch("src.notify.telegram_bot.app_config",
               {"settings": {"notifications": {"foo": "yes"}}}):
        assert should_send("foo") is True
    with patch("src.notify.telegram_bot.app_config",
               {"settings": {"notifications": {"bar": 0}}}):
        assert should_send("bar") is False


# --- Live config defaults ---

def test_live_config_critical_channels_on():
    """Real settings.yaml must keep critical channels enabled."""
    from src.config import app_config
    notif = app_config.get("settings", {}).get("notifications", {})
    assert notif.get("trade_alerts") is True, "trade_alerts must be ON"
    assert notif.get("circuit_breaker") is True, "circuit_breaker must be ON"
    assert notif.get("realtime_macro_alerts") is True
    assert notif.get("limit_order_filled") is True


def test_live_config_background_channels_off():
    """Background channels must default OFF — Mini App + /dashboard cover them."""
    from src.config import app_config
    notif = app_config.get("settings", {}).get("notifications", {})
    background_channels = [
        "periodic_summary", "daily_recap", "sector_review_alert",
        "weekly_self_review", "midweek_thesis_refresh",
        "weekly_thesis_review", "fast_path_alert",
        "ipo_watchlist_updates", "source_deactivation",
        "position_analyst_info",
    ]
    for ch in background_channels:
        assert notif.get(ch) is False, f"{ch} should be OFF by default"


# --- Per-channel guard call sites ---

def test_periodic_summary_loop_short_circuits_when_disabled():
    """periodic_summary_loop returns early when notifications.periodic_summary
    is False, even if periodic_summary.enabled is True."""
    import asyncio
    from unittest.mock import AsyncMock, patch as p

    fake_send = AsyncMock()
    cfg = {"settings": {
        "periodic_summary": {"enabled": True, "interval_hours": 4,
                             "startup_delay_minutes": 10},
        "notifications": {"periodic_summary": False},
    }}
    with p("main.app_config", cfg), \
         p("src.notify.telegram_bot.app_config", cfg), \
         p("src.notify.telegram_periodic_summary.send_periodic_summary",
           fake_send):
        from main import periodic_summary_loop
        # Loop should exit before sleeping (short-circuit on the gate)
        asyncio.run(asyncio.wait_for(periodic_summary_loop(), timeout=2.0))
    fake_send.assert_not_called()


def test_daily_recap_skips_when_disabled():
    """send_daily_recap returns early when notifications.daily_recap=False."""
    import asyncio
    from unittest.mock import AsyncMock, MagicMock, patch as p

    cfg = {"settings": {"notifications": {"daily_recap": False}}}
    fake_app = MagicMock()
    fake_app.bot.send_message = AsyncMock()
    with p("src.notify.telegram_bot.app_config", cfg):
        from src.notify.telegram_live_dashboard import send_daily_recap
        asyncio.run(send_daily_recap(fake_app))
    fake_app.bot.send_message.assert_not_called()


def test_daily_recap_sends_when_enabled():
    """send_daily_recap proceeds when notifications.daily_recap=True."""
    import asyncio
    from unittest.mock import AsyncMock, MagicMock, patch as p

    cfg = {"settings": {"notifications": {"daily_recap": True}}}
    fake_app = MagicMock()
    fake_app.bot.send_message = AsyncMock()
    with p("src.notify.telegram_bot.app_config", cfg), \
         p("src.notify.telegram_live_dashboard._get_chat_id",
           return_value="123"), \
         p("src.notify.telegram_live_dashboard.build_daily_recap",
           return_value="recap text"):
        from src.notify.telegram_live_dashboard import send_daily_recap
        asyncio.run(send_daily_recap(fake_app))
    fake_app.bot.send_message.assert_called_once()
