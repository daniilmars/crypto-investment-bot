"""Tests for the Mini App FastAPI routes — auth gating, caching, response shape."""

import hmac
import hashlib
import json
import time
from urllib.parse import urlencode

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient


TEST_TOKEN = "1234567:TEST_TOKEN_VALUE"
TEST_USER_ID = 42


def _sign(fields: dict) -> str:
    data_check_string = "\n".join(f"{k}={v}" for k, v in sorted(fields.items()))
    secret = hmac.new(b"WebAppData", TEST_TOKEN.encode(), hashlib.sha256).digest()
    h = hmac.new(secret, data_check_string.encode(), hashlib.sha256).hexdigest()
    return urlencode({**fields, "hash": h})


def _good_init_data() -> str:
    fields = {
        "auth_date": str(int(time.time())),
        "query_id": "AAH",
        "user": json.dumps({"id": TEST_USER_ID, "first_name": "Test"}),
    }
    return _sign(fields)


@pytest.fixture
def client(monkeypatch):
    # Patch auth to known token + whitelist
    monkeypatch.setattr("src.api.miniapp_auth._bot_token", lambda: TEST_TOKEN)
    monkeypatch.setattr("src.api.miniapp_auth._authorized_ids", lambda: {TEST_USER_ID})

    # Stub out query functions so tests don't depend on DB state
    call_counts = {"summary": 0, "positions": 0, "equity": 0, "recent": 0}

    def fake_summary():
        call_counts["summary"] += 1
        return {"marker": "summary", "realized": {"d1": 0, "d7": 0, "d30": 0, "all": 0}}

    def fake_positions():
        call_counts["positions"] += 1
        return {"marker": "positions", "positions": []}

    def fake_equity(days=30):
        call_counts["equity"] += 1
        return {"marker": "equity", "days": days}

    def fake_recent(limit=10):
        call_counts["recent"] += 1
        return {"marker": "recent_trades", "limit": limit, "trades": []}

    monkeypatch.setattr("src.api.miniapp_routes.summary_data", fake_summary)
    monkeypatch.setattr("src.api.miniapp_routes.positions_data", fake_positions)
    monkeypatch.setattr("src.api.miniapp_routes.equity_data", fake_equity)
    monkeypatch.setattr("src.api.miniapp_routes.recent_trades_data", fake_recent)

    # Clear cache before each test
    from src.api.miniapp_routes import _clear_cache
    _clear_cache()

    from src.api.miniapp_routes import router
    app = FastAPI()
    app.include_router(router, prefix="/api/miniapp")

    tc = TestClient(app)
    tc.call_counts = call_counts
    return tc


def test_summary_requires_init_data(client):
    r = client.get("/api/miniapp/summary")
    assert r.status_code == 401


def test_summary_ok_with_valid_init_data(client):
    r = client.get(
        "/api/miniapp/summary",
        headers={"X-Telegram-Init-Data": _good_init_data()},
    )
    assert r.status_code == 200
    assert r.json()["marker"] == "summary"


def test_unauthorized_user_gets_403(client, monkeypatch):
    monkeypatch.setattr("src.api.miniapp_auth._authorized_ids", lambda: {9999})
    r = client.get(
        "/api/miniapp/summary",
        headers={"X-Telegram-Init-Data": _good_init_data()},
    )
    assert r.status_code == 403


def test_positions_returns_stubbed_payload(client):
    r = client.get(
        "/api/miniapp/positions",
        headers={"X-Telegram-Init-Data": _good_init_data()},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["marker"] == "positions"
    assert body["positions"] == []


def test_equity_accepts_days_param(client):
    r = client.get(
        "/api/miniapp/equity?days=7",
        headers={"X-Telegram-Init-Data": _good_init_data()},
    )
    assert r.status_code == 200
    assert r.json()["days"] == 7


def test_equity_clamps_out_of_range_days(client):
    r = client.get(
        "/api/miniapp/equity?days=9999",
        headers={"X-Telegram-Init-Data": _good_init_data()},
    )
    assert r.status_code == 200
    assert r.json()["days"] == 365


def test_cache_dedupes_repeat_calls(client):
    headers = {"X-Telegram-Init-Data": _good_init_data()}
    client.get("/api/miniapp/summary", headers=headers)
    client.get("/api/miniapp/summary", headers=headers)
    client.get("/api/miniapp/summary", headers=headers)
    assert client.call_counts["summary"] == 1


def test_cache_scopes_equity_by_days(client):
    headers = {"X-Telegram-Init-Data": _good_init_data()}
    client.get("/api/miniapp/equity?days=7", headers=headers)
    client.get("/api/miniapp/equity?days=30", headers=headers)
    client.get("/api/miniapp/equity?days=7", headers=headers)
    # Two distinct cache keys (days=7, days=30); third call is a cache hit.
    assert client.call_counts["equity"] == 2


# --- new /trades/recent endpoint ---

def test_recent_trades_requires_auth(client):
    r = client.get("/api/miniapp/trades/recent")
    assert r.status_code == 401


def test_recent_trades_returns_payload(client):
    r = client.get(
        "/api/miniapp/trades/recent",
        headers={"X-Telegram-Init-Data": _good_init_data()},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["marker"] == "recent_trades"
    assert body["trades"] == []


def test_recent_trades_clamps_limit(client):
    headers = {"X-Telegram-Init-Data": _good_init_data()}
    # Upper bound
    r = client.get("/api/miniapp/trades/recent?limit=9999", headers=headers)
    assert r.status_code == 200
    assert r.json()["limit"] == 50
    # Lower bound (negative) → clamp to 1
    r = client.get("/api/miniapp/trades/recent?limit=-5", headers=headers)
    assert r.status_code == 200
    assert r.json()["limit"] == 1


def test_recent_trades_cache_scopes_by_limit(client):
    headers = {"X-Telegram-Init-Data": _good_init_data()}
    client.get("/api/miniapp/trades/recent?limit=10", headers=headers)
    client.get("/api/miniapp/trades/recent?limit=10", headers=headers)
    client.get("/api/miniapp/trades/recent?limit=25", headers=headers)
    assert client.call_counts["recent"] == 2
