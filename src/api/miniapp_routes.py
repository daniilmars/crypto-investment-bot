"""
FastAPI routes for the Telegram Mini App dashboard.

All API endpoints require a valid ``X-Telegram-Init-Data`` header, verified
by ``miniapp_auth``. Responses are cached in-process for a short TTL (default
20 s) so the UI's 30 s refresh doesn't hammer SQLite.
"""

import asyncio
import os
import time
from pathlib import Path

from fastapi import APIRouter, Depends
from fastapi.staticfiles import StaticFiles

from src.api.miniapp_auth import miniapp_auth
from src.api.miniapp_queries import (
    summary_data, positions_data, equity_data, recent_trades_data,
)


router = APIRouter()

_CACHE_TTL_SEC = int(os.environ.get("MINIAPP_API_CACHE_TTL_SEC", "20"))
_cache: dict[str, tuple[float, object]] = {}


def _cached(key: str, ttl: int, compute):
    now = time.time()
    hit = _cache.get(key)
    if hit and now - hit[0] < ttl:
        return hit[1]
    val = compute()
    _cache[key] = (now, val)
    return val


def _clear_cache():
    _cache.clear()


@router.get("/summary")
async def summary(_user: dict = Depends(miniapp_auth)) -> dict:
    return await asyncio.to_thread(
        _cached, "summary", _CACHE_TTL_SEC, summary_data
    )


@router.get("/positions")
async def positions(_user: dict = Depends(miniapp_auth)) -> dict:
    return await asyncio.to_thread(
        _cached, "positions", _CACHE_TTL_SEC, positions_data
    )


@router.get("/equity")
async def equity(days: int = 30, _user: dict = Depends(miniapp_auth)) -> dict:
    days = max(1, min(int(days or 30), 365))
    return await asyncio.to_thread(
        _cached, f"equity:{days}", _CACHE_TTL_SEC, lambda: equity_data(days=days)
    )


@router.get("/trades/recent")
async def trades_recent(limit: int = 10,
                         _user: dict = Depends(miniapp_auth)) -> dict:
    limit = max(1, min(int(limit or 10), 50))
    # Closed-trade set changes slowly; cache longer than positions.
    return await asyncio.to_thread(
        _cached, f"recent_trades:{limit}", _CACHE_TTL_SEC * 3,
        lambda: recent_trades_data(limit=limit),
    )


# ---------------------------------------------------------------- static files


def mount_miniapp_static(app) -> None:
    """Mount the Mini App static bundle at ``/miniapp``.

    Called from ``main.py`` during startup. Does nothing if the static
    directory doesn't exist (useful for unit tests and minimal deployments).
    """
    static_dir = Path(__file__).resolve().parents[2] / "static" / "miniapp"
    if not static_dir.is_dir():
        return
    app.mount(
        "/miniapp",
        StaticFiles(directory=static_dir, html=True),
        name="miniapp",
    )


def miniapp_url_configured() -> bool:
    """Whether the bot should surface the Mini App button in Telegram."""
    return bool(os.environ.get("MINIAPP_BASE_URL"))
