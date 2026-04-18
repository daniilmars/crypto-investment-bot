"""
Fast-path priority evaluation for the first stock cycle after an idle window.

On long idle gaps (weekend, holiday, bot outage) Gemini keeps producing
symbol-level assessments from headlines. By the time markets reopen,
those signals have aged but the information is still valuable — if we
evaluate in watch_list order, the cycle may burn time on noise before
reaching the tickers where the catalyst lives.

This module detects the idle-gap → market-open transition and returns a
priority list of symbols to evaluate first. It does NOT bypass any
existing signal engine, RSI/SMA gate, cooldown, or position limit — it
only changes EVALUATION ORDER.

Priority formula:
    effective_confidence = raw_confidence * exp(-hours_old / half_life)

With the default 72h half-life and 0.35 threshold, a Saturday 0.90
signal still scores ≈0.57 at Monday open, while week-old mid-grade
signals decay below the floor.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

import psycopg2

from src.database import (
    _cursor, get_db_connection, release_db_connection,
    save_bot_state, load_bot_state,
)
from src.logger import log


# --- bot_state_kv keys ---
KEY_LAST_CYCLE = "last_stock_cycle_completed_at"
KEY_LAST_TRIGGER = "last_fast_path_triggered_at"


# --- Time helpers ---

def _parse_iso(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except Exception:
        return None


def _now() -> datetime:
    return datetime.now(timezone.utc)


# --- Core priority computation (pure — easy to unit-test) ---

def compute_priority(rows: list[dict], watch_list: list[str],
                     cfg: dict) -> list[dict]:
    """Apply decay, threshold, exclude, cap — return ordered priority list.

    Each row must have: ``symbol``, ``max_conf`` (float), ``latest_at``
    (datetime or ISO string), ``direction``, ``catalyst_type``.
    """
    half_life = float(cfg.get("catalyst_half_life_hours", 72))
    min_eff = float(cfg.get("min_effective_confidence", 0.35))
    cap = int(cfg.get("max_priority_symbols", 10))
    excludes = set(cfg.get("exclude_symbols") or [])
    watch_set = set(watch_list)
    now = _now()

    scored: list[dict] = []
    seen: dict[tuple[str, str], dict] = {}  # (symbol, direction) → best row

    for r in rows:
        sym = r.get("symbol")
        if not sym or sym not in watch_set or sym in excludes:
            continue
        raw = float(r.get("max_conf") or 0)
        if raw <= 0:
            continue
        latest = r.get("latest_at")
        if not isinstance(latest, datetime):
            latest = _parse_iso(latest)
        if latest is None:
            continue
        age_h = max(0.0, (now - latest).total_seconds() / 3600.0)
        # True half-life: value halves each `half_life` hours.
        eff = raw * (0.5 ** (age_h / half_life)) if half_life > 0 else raw
        if eff < min_eff:
            continue
        key = (sym, r.get("direction") or "")
        candidate = {
            "symbol": sym,
            "eff": eff,
            "raw": raw,
            "age_h": age_h,
            "direction": r.get("direction") or "bullish",
            "catalyst_type": r.get("catalyst_type") or "",
        }
        prev = seen.get(key)
        if prev is None or candidate["eff"] > prev["eff"]:
            seen[key] = candidate

    scored = sorted(seen.values(), key=lambda x: x["eff"], reverse=True)
    return scored[:cap]


# --- DB fetch ---

def fetch_recent_assessments(lookback_hours: int = 72,
                             min_raw_confidence: float = 0.5,
                             limit: int = 200) -> list[dict]:
    """Pull recent Gemini assessments for priority scoring.

    Over-fetches and filters/decays in Python so the SQL stays portable
    across SQLite/Postgres.
    """
    conn = None
    try:
        conn = get_db_connection()
        if not conn:
            return []
        is_pg = isinstance(conn, psycopg2.extensions.connection)
        if is_pg:
            query = """
                SELECT symbol,
                       MAX(confidence) AS max_conf,
                       MAX(created_at) AS latest_at,
                       direction,
                       (ARRAY_AGG(catalyst_type ORDER BY created_at DESC))[1]
                         AS catalyst_type
                FROM gemini_assessments
                WHERE created_at >= NOW() - (%s || ' hours')::interval
                  AND confidence >= %s
                  AND direction IN ('bullish', 'bearish')
                GROUP BY symbol, direction
                ORDER BY max_conf DESC
                LIMIT %s
            """
            params = (str(lookback_hours), min_raw_confidence, limit)
        else:
            query = """
                SELECT symbol,
                       MAX(confidence) AS max_conf,
                       MAX(created_at) AS latest_at,
                       direction,
                       catalyst_type
                FROM gemini_assessments
                WHERE created_at >= datetime('now', '-' || ? || ' hours')
                  AND confidence >= ?
                  AND direction IN ('bullish', 'bearish')
                GROUP BY symbol, direction
                ORDER BY max_conf DESC
                LIMIT ?
            """
            params = (lookback_hours, min_raw_confidence, limit)

        with _cursor(conn) as cur:
            cur.execute(query, params)
            raw_rows = cur.fetchall()
            cols = [d[0] for d in cur.description] if cur.description else []

        out: list[dict] = []
        for row in raw_rows:
            if isinstance(row, dict):
                out.append(dict(row))
            else:
                out.append(dict(zip(cols, row)))
        return out
    except Exception as e:
        log.warning("fast_path.fetch_recent_assessments failed: %s", e)
        return []
    finally:
        if conn is not None:
            release_db_connection(conn)


# --- Trigger gate: idle-gap + rate-limit ---

def should_trigger(cfg: dict) -> tuple[bool, float]:
    """Return (trigger_now, idle_hours). Consults bot_state_kv."""
    idle_threshold = float(cfg.get("idle_gap_hours", 2))
    last_cycle = _parse_iso(load_bot_state(KEY_LAST_CYCLE))
    if last_cycle is None:
        # First run ever — persist current time so we have a baseline, don't trigger
        persist_cycle_complete()
        return False, 0.0
    now = _now()
    idle_h = (now - last_cycle).total_seconds() / 3600.0
    if idle_h < idle_threshold:
        return False, idle_h
    # Rate-limit: don't retrigger if already fired recently
    last_trigger = _parse_iso(load_bot_state(KEY_LAST_TRIGGER))
    if last_trigger is not None:
        since_trigger_h = (now - last_trigger).total_seconds() / 3600.0
        if since_trigger_h < idle_threshold:
            return False, idle_h
    return True, idle_h


def apply_priority_order(priority_symbols: list[str],
                         watch_list: list[str]) -> list[str]:
    """Pure prepend: priority symbols first (in their given order), followed
    by the rest of ``watch_list`` with duplicates removed while preserving
    original order."""
    seen = set(priority_symbols)
    tail = [s for s in watch_list if s not in seen]
    return list(priority_symbols) + tail


def check_and_build_priority_list(watch_list: list[str],
                                  cfg: dict) -> tuple[list[dict], float]:
    """Full trigger check + priority list. Returns ([], 0.0) when not firing."""
    trigger, idle_h = should_trigger(cfg)
    if not trigger:
        return [], idle_h
    rows = fetch_recent_assessments(
        lookback_hours=int(cfg.get("lookback_hours", 72)),
        # Over-fetch slightly below threshold so decay math is the gate
        min_raw_confidence=0.5,
    )
    priority = compute_priority(rows, watch_list, cfg)
    return priority, idle_h


# --- State persistence ---

def persist_trigger() -> None:
    try:
        save_bot_state(KEY_LAST_TRIGGER, _now().isoformat())
    except Exception as e:
        log.warning("fast_path.persist_trigger failed: %s", e)


def persist_cycle_complete() -> None:
    try:
        save_bot_state(KEY_LAST_CYCLE, _now().isoformat())
    except Exception as e:
        log.debug("fast_path.persist_cycle_complete failed: %s", e)


# --- Telegram alert format ---

def format_alert_message(priority: list[dict], idle_hours: float,
                         dry_run: bool = False) -> str:
    prefix = "[DRY-RUN] " if dry_run else ""
    verb = "would prioritize on next cycle" if dry_run else "prioritizing"
    lines = [
        f"{prefix}Weekend fast-path ready ({idle_hours:.1f}h idle gap)",
        f"Symbols {verb}:",
    ]
    for i, p in enumerate(priority[:8], 1):
        lines.append(
            f"  {i}. {p['symbol']}  eff {p['eff']:.2f} "
            f"(raw {p['raw']:.2f}, {p['age_h']:.0f}h, "
            f"{p['catalyst_type'] or '-'})"
        )
    if len(priority) > 8:
        lines.append(f"  ... and {len(priority) - 8} more")
    lines.append("All existing gates still apply.")
    return "\n".join(lines)
