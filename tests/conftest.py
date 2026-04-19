"""Global pytest fixtures.

Backstop against tests accidentally sending real Telegram messages.

Apr 19 incident: tests/test_trading_improvements.py and
tests/test_execution_pipeline_fixes.py called monitor_position with mocks
for `send_telegram_alert` but missed `_send_trade_exit_alert`. The latter
constructs `Bot(token=...)` and calls `send_message` — in CI the bot token
is in the environment, so fake AAPL/BTC trade alerts went to the real chat.

The autouse fixture here replaces `Bot` in every notify module with a
no-op stub so even a missed mock can't reach Telegram.

Individual tests should still patch at the right call site (it's clearer
and makes assertion-on-args possible). This fixture is a safety net only.
"""

from unittest.mock import AsyncMock

import pytest


@pytest.fixture(autouse=True)
def _block_real_telegram(monkeypatch):
    class _FakeBot:
        def __init__(self, *args, **kwargs):
            self.send_message = AsyncMock(return_value=_FakeMessage())
            self.edit_message_text = AsyncMock(return_value=_FakeMessage())
            self.set_webhook = AsyncMock(return_value=True)
            self.delete_webhook = AsyncMock(return_value=True)
            self.get_webhook_info = AsyncMock(return_value=_FakeWebhookInfo())
            self.send_chat_action = AsyncMock()
            self.pin_chat_message = AsyncMock()
            self.unpin_chat_message = AsyncMock()
            self.get_chat = AsyncMock()

    class _FakeMessage:
        message_id = 1
        chat_id = 1

    class _FakeWebhookInfo:
        url = ""
        pending_update_count = 0
        allowed_updates = []

    # Patch every notify module that imports `Bot` from `telegram`.
    notify_modules = [
        "src.notify.telegram_bot",
        "src.notify.telegram_periodic_summary",
        "src.notify.telegram_live_dashboard",
        "src.notify.telegram_dashboard",
        "src.notify.telegram_chat",
        "src.notify.telegram_alerts_enhanced",
    ]
    for mod_path in notify_modules:
        try:
            monkeypatch.setattr(f"{mod_path}.Bot", _FakeBot, raising=False)
        except (ImportError, AttributeError):
            pass

    # Patch the canonical import too, so Bot() constructed elsewhere
    # (e.g. main.py, ad-hoc scripts) still gets the fake.
    monkeypatch.setattr("telegram.Bot", _FakeBot, raising=False)
