"""Tests for Telegram Mini App initData verification."""

import hmac
import hashlib
import json
import time
from urllib.parse import urlencode

import pytest
from fastapi import HTTPException

from src.api.miniapp_auth import _verify_init_data


TEST_TOKEN = "1234567:TEST_TOKEN_VALUE"
TEST_USER_ID = 42
TEST_OTHER_ID = 99


def _sign(token: str, fields: dict) -> str:
    """Produce a valid initData querystring using Telegram's exact algorithm."""
    data_check_string = "\n".join(f"{k}={v}" for k, v in sorted(fields.items()))
    secret_key = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    h = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()
    return urlencode({**fields, "hash": h})


@pytest.fixture
def patch_config(monkeypatch):
    """Point the auth module at our test token + whitelist."""
    monkeypatch.setattr("src.api.miniapp_auth._bot_token", lambda: TEST_TOKEN)
    monkeypatch.setattr("src.api.miniapp_auth._authorized_ids", lambda: {TEST_USER_ID})


def _good_fields(user_id: int = TEST_USER_ID, auth_date: int | None = None) -> dict:
    return {
        "auth_date": str(auth_date if auth_date is not None else int(time.time())),
        "query_id": "AAH",
        "user": json.dumps({"id": user_id, "first_name": "Test"}),
    }


def test_happy_path_returns_user_dict(patch_config):
    init_data = _sign(TEST_TOKEN, _good_fields())
    user = _verify_init_data(init_data)
    assert user["id"] == TEST_USER_ID
    assert user["first_name"] == "Test"


def test_tampered_hash_rejected(patch_config):
    init_data = _sign(TEST_TOKEN, _good_fields())
    tampered = init_data[:-4] + "dead"
    with pytest.raises(HTTPException) as exc:
        _verify_init_data(tampered)
    assert exc.value.status_code == 401


def test_tampered_field_rejected(patch_config):
    fields = _good_fields()
    init_data = _sign(TEST_TOKEN, fields)
    # Swap in a different user id while keeping the same hash
    bad = init_data.replace(f'id%22%3A+{TEST_USER_ID}', f'id%22%3A+{TEST_OTHER_ID}')
    if bad == init_data:
        # Fallback: append a junk field that wasn't signed
        bad = init_data + "&chat_type=private"
    with pytest.raises(HTTPException) as exc:
        _verify_init_data(bad)
    assert exc.value.status_code == 401


def test_stale_auth_date_rejected(patch_config):
    stale = int(time.time()) - (60 * 60 * 48)  # 48h ago > 24h window
    init_data = _sign(TEST_TOKEN, _good_fields(auth_date=stale))
    with pytest.raises(HTTPException) as exc:
        _verify_init_data(init_data)
    assert exc.value.status_code == 401
    assert "stale" in exc.value.detail.lower()


def test_missing_hash_rejected(patch_config):
    fields = _good_fields()
    no_hash = urlencode(fields)
    with pytest.raises(HTTPException) as exc:
        _verify_init_data(no_hash)
    assert exc.value.status_code == 401


def test_empty_init_data_rejected(patch_config):
    with pytest.raises(HTTPException) as exc:
        _verify_init_data("")
    assert exc.value.status_code == 401


def test_unauthorized_user_rejected(patch_config):
    init_data = _sign(TEST_TOKEN, _good_fields(user_id=TEST_OTHER_ID))
    with pytest.raises(HTTPException) as exc:
        _verify_init_data(init_data)
    assert exc.value.status_code == 403


def test_missing_token_returns_503(monkeypatch):
    monkeypatch.setattr("src.api.miniapp_auth._bot_token", lambda: None)
    with pytest.raises(HTTPException) as exc:
        _verify_init_data("anything")
    assert exc.value.status_code == 503


def test_unicode_first_name_in_user_payload(patch_config):
    fields = _good_fields()
    fields["user"] = json.dumps({"id": TEST_USER_ID, "first_name": "Лев 🌊"})
    init_data = _sign(TEST_TOKEN, fields)
    user = _verify_init_data(init_data)
    assert user["first_name"] == "Лев 🌊"


def test_malformed_auth_date_rejected(patch_config):
    fields = _good_fields()
    fields["auth_date"] = "not-a-number"
    init_data = _sign(TEST_TOKEN, fields)
    with pytest.raises(HTTPException) as exc:
        _verify_init_data(init_data)
    assert exc.value.status_code == 401


def test_empty_whitelist_allows_all(monkeypatch):
    """If authorized_user_ids is empty, gate is disabled (covers a fresh install)."""
    monkeypatch.setattr("src.api.miniapp_auth._bot_token", lambda: TEST_TOKEN)
    monkeypatch.setattr("src.api.miniapp_auth._authorized_ids", lambda: set())
    init_data = _sign(TEST_TOKEN, _good_fields(user_id=12345))
    user = _verify_init_data(init_data)
    assert user["id"] == 12345
