"""
Telegram Mini App initData verification.

When a user opens the Mini App inside Telegram, the client passes a signed
``initData`` string. We verify the HMAC-SHA256 signature against the bot
token, enforce a freshness window, and gate on the existing
``authorized_user_ids`` whitelist.

Spec: https://core.telegram.org/bots/webapps#validating-data-received-via-the-mini-app
"""

import hmac
import hashlib
import json
import os
import time
from urllib.parse import parse_qsl

from fastapi import Header, HTTPException

from src.config import app_config
from src.logger import log


def _telegram_config() -> dict:
    return app_config.get('notification_services', {}).get('telegram', {}) or {}


def _bot_token() -> str | None:
    return _telegram_config().get('token')


def _authorized_ids() -> set[int]:
    ids = _telegram_config().get('authorized_user_ids') or []
    return {int(x) for x in ids}


def _max_age_seconds() -> int:
    return int(os.environ.get('MINIAPP_AUTH_MAX_AGE_SEC', '86400'))


def _verify_init_data(init_data: str) -> dict:
    """Validate ``init_data``; return the parsed ``user`` dict on success.

    Raises ``HTTPException`` on any validation failure. Errors are logged at
    WARNING level so failures are visible without leaking signatures.
    """
    token = _bot_token()
    if not token:
        log.error("Mini App auth: TELEGRAM_BOT_TOKEN is not set")
        raise HTTPException(status_code=503, detail="bot token not configured")

    if not init_data:
        raise HTTPException(status_code=401, detail="missing initData")

    try:
        pairs = dict(parse_qsl(init_data, keep_blank_values=True))
    except Exception:
        raise HTTPException(status_code=401, detail="malformed initData")

    received_hash = pairs.pop('hash', None)
    if not received_hash:
        raise HTTPException(status_code=401, detail="missing hash")

    # data_check_string: key=value pairs (excluding hash), sorted, joined by \n
    data_check_string = "\n".join(f"{k}={v}" for k, v in sorted(pairs.items()))

    secret_key = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    computed = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()

    if not hmac.compare_digest(computed, received_hash):
        log.warning("Mini App auth: hash mismatch")
        raise HTTPException(status_code=401, detail="bad hash")

    # Freshness / replay window
    try:
        auth_date = int(pairs.get('auth_date', '0'))
    except ValueError:
        raise HTTPException(status_code=401, detail="malformed auth_date")

    if auth_date <= 0 or time.time() - auth_date > _max_age_seconds():
        log.warning("Mini App auth: stale initData (auth_date=%s)", auth_date)
        raise HTTPException(status_code=401, detail="stale initData")

    # Whitelist
    try:
        user = json.loads(pairs.get('user', '{}'))
    except json.JSONDecodeError:
        raise HTTPException(status_code=401, detail="malformed user payload")

    uid = user.get('id')
    if uid is None:
        raise HTTPException(status_code=401, detail="missing user.id")

    allowed = _authorized_ids()
    if allowed and uid not in allowed:
        log.warning("Mini App auth: user %s not authorized", uid)
        raise HTTPException(status_code=403, detail="not authorized")

    return user


async def miniapp_auth(
    x_telegram_init_data: str | None = Header(default=None, alias="X-Telegram-Init-Data"),
) -> dict:
    """FastAPI dependency: verify initData header and return the user dict."""
    return _verify_init_data(x_telegram_init_data or "")
