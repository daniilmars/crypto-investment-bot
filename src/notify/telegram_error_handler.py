"""Telegram error handler — sends ERROR/CRITICAL log messages to Telegram.

Custom logging.Handler that batches error messages and sends them to
the configured Telegram chat. Rate-limited to at most one message per
60 seconds, with deduplication of repeated errors.
"""

import logging
import os
import threading
import time


class TelegramErrorHandler(logging.Handler):
    """Batches ERROR/CRITICAL messages and sends to Telegram periodically."""

    MIN_INTERVAL = 60  # seconds between Telegram sends
    MAX_BATCH = 10     # max errors per message

    # Known recurring errors that are noise — suppress from Telegram alerts
    IGNORE_PATTERNS = [
        'KASUSDT',           # Kaspa not on Binance
        'ALPACA_API_KEY',    # Alpaca not configured
        'Invalid symbol',    # generic invalid symbol noise
        'Failed to parse Gemini grounded news response as JSON',  # empty responses (expected)
        'Gemini grounded search returned empty',  # batch empty response (expected, retried next cycle)
    ]

    def __init__(self, token: str, chat_id: str, level=logging.ERROR):
        super().__init__(level)
        self._token = token
        self._chat_id = chat_id
        self._buffer: list[str] = []
        self._seen: set[str] = set()
        self._lock = threading.Lock()
        self._last_send = 0.0
        self._timer: threading.Timer | None = None

    def emit(self, record: logging.LogRecord):
        try:
            msg = self.format(record)

            # Skip known recurring noise
            for pattern in self.IGNORE_PATTERNS:
                if pattern in msg:
                    return

            # Deduplicate by first 120 chars (ignore timestamps)
            dedup_key = msg[:120]
            with self._lock:
                if dedup_key in self._seen:
                    return
                self._seen.add(dedup_key)
                self._buffer.append(msg)

                # Schedule a flush if not already pending
                if self._timer is None or not self._timer.is_alive():
                    elapsed = time.time() - self._last_send
                    delay = max(0, self.MIN_INTERVAL - elapsed)
                    self._timer = threading.Timer(delay, self._flush)
                    self._timer.daemon = True
                    self._timer.start()
        except Exception:
            self.handleError(record)

    def _flush(self):
        with self._lock:
            if not self._buffer:
                return
            batch = self._buffer[:self.MAX_BATCH]
            self._buffer = self._buffer[self.MAX_BATCH:]
            self._seen.clear()
            self._last_send = time.time()

        text = "*Bot Errors:*\n\n" + "\n---\n".join(batch)
        if len(text) > 4000:
            text = text[:3997] + "..."

        try:
            import requests
            url = f"https://api.telegram.org/bot{self._token}/sendMessage"
            requests.post(url, json={
                "chat_id": self._chat_id,
                "text": text,
                "parse_mode": "Markdown",
            }, timeout=10)
        except Exception as e:
            import sys
            print(f"Telegram error send failed: {e}", file=sys.stderr)

    def close(self):
        if self._timer:
            self._timer.cancel()
        self._flush()
        super().close()


def attach_telegram_error_handler(logger: logging.Logger):
    """Attaches TelegramErrorHandler if Telegram env vars are set."""
    token = os.environ.get('TELEGRAM_BOT_TOKEN')
    chat_id = os.environ.get('TELEGRAM_CHAT_ID')
    if not token or not chat_id:
        return

    handler = TelegramErrorHandler(token, chat_id)
    formatter = logging.Formatter(
        '%(levelname)s - %(module)s - %(message)s'
    )
    handler.setFormatter(formatter)
    logger.addHandler(handler)
