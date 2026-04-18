"""Tests for src/orchestration/fast_path.py."""

from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

from src.orchestration.fast_path import (
    compute_priority, should_trigger, check_and_build_priority_list,
    format_alert_message,
)


def _row(symbol, conf, hours_old, direction="bullish", catalyst="macro"):
    return {
        "symbol": symbol,
        "max_conf": conf,
        "latest_at": datetime.now(timezone.utc) - timedelta(hours=hours_old),
        "direction": direction,
        "catalyst_type": catalyst,
    }


DEFAULT_CFG = {
    "catalyst_half_life_hours": 72,
    "min_effective_confidence": 0.35,
    "max_priority_symbols": 10,
    "idle_gap_hours": 2,
    "exclude_symbols": [],
}


# --- decay math ------------------------------------------------------------

def test_decay_math_12h():
    """0.90 raw, 12h old, 72h half-life → eff ≈ 0.80."""
    rows = [_row("XOM", 0.90, 12)]
    result = compute_priority(rows, ["XOM"], DEFAULT_CFG)
    assert len(result) == 1
    assert result[0]["eff"] == pytest.approx(0.90 * (0.5 ** (12 / 72)), abs=0.01)


def test_decay_math_48h():
    """0.90 raw, 48h old, 72h half-life → eff ≈ 0.57."""
    rows = [_row("XOM", 0.90, 48)]
    result = compute_priority(rows, ["XOM"], DEFAULT_CFG)
    assert result[0]["eff"] == pytest.approx(0.90 * (0.5 ** (48 / 72)), abs=0.01)
    assert 0.55 <= result[0]["eff"] <= 0.60


def test_decay_math_at_one_half_life():
    """0.80 raw at exactly half-life → eff = 0.40 (halved)."""
    rows = [_row("XOM", 0.80, 72)]
    result = compute_priority(rows, ["XOM"], DEFAULT_CFG)
    assert result[0]["eff"] == pytest.approx(0.40, abs=0.001)


def test_decay_math_weekend_hormuz_scenario():
    """Saturday 0.90 signal at Monday open (~52h) with threshold 0.35 passes."""
    rows = [_row("SLB", 0.90, 52)]
    result = compute_priority(rows, ["SLB"], DEFAULT_CFG)
    assert len(result) == 1
    assert result[0]["eff"] > 0.35


def test_threshold_drops_below_min_eff():
    """raw 0.40 at 72h old → eff ≈ 0.20, below 0.35 threshold → dropped."""
    rows = [_row("LOW", 0.40, 72)]
    result = compute_priority(rows, ["LOW"], DEFAULT_CFG)
    assert result == []


def test_cap_honored():
    """50 rows, cap=10 → returns exactly 10."""
    rows = [_row(f"S{i}", 0.9, 6) for i in range(50)]
    watch_list = [f"S{i}" for i in range(50)]
    result = compute_priority(rows, watch_list, DEFAULT_CFG)
    assert len(result) == 10


def test_sorted_descending_by_eff():
    rows = [
        _row("LOW", 0.70, 6),   # eff ≈ 0.64
        _row("HIGH", 0.90, 6),  # eff ≈ 0.82
        _row("MID", 0.80, 6),   # eff ≈ 0.73
    ]
    result = compute_priority(rows, ["LOW", "HIGH", "MID"], DEFAULT_CFG)
    assert [r["symbol"] for r in result] == ["HIGH", "MID", "LOW"]


# --- filters --------------------------------------------------------------

def test_watch_list_filter():
    rows = [_row("IN_LIST", 0.9, 6), _row("NOT_IN_LIST", 0.9, 6)]
    result = compute_priority(rows, ["IN_LIST"], DEFAULT_CFG)
    assert len(result) == 1
    assert result[0]["symbol"] == "IN_LIST"


def test_exclude_symbols():
    cfg = {**DEFAULT_CFG, "exclude_symbols": ["BLOCKED"]}
    rows = [_row("BLOCKED", 0.9, 6), _row("ALLOWED", 0.9, 6)]
    result = compute_priority(rows, ["BLOCKED", "ALLOWED"], cfg)
    assert [r["symbol"] for r in result] == ["ALLOWED"]


def test_dedup_symbol_direction_max_wins():
    """Two rows, same (symbol, direction) — max confidence wins."""
    rows = [
        {"symbol": "XOM", "max_conf": 0.6,
         "latest_at": datetime.now(timezone.utc) - timedelta(hours=6),
         "direction": "bullish", "catalyst_type": "macro"},
        {"symbol": "XOM", "max_conf": 0.9,
         "latest_at": datetime.now(timezone.utc) - timedelta(hours=6),
         "direction": "bullish", "catalyst_type": "macro"},
    ]
    result = compute_priority(rows, ["XOM"], DEFAULT_CFG)
    assert len(result) == 1
    assert result[0]["raw"] == 0.9


def test_both_directions_kept_separate():
    rows = [
        _row("XOM", 0.8, 6, direction="bullish"),
        _row("XOM", 0.7, 6, direction="bearish"),
    ]
    result = compute_priority(rows, ["XOM"], DEFAULT_CFG)
    assert len(result) == 2


def test_negative_age_clamped_to_zero():
    """Future-dated row (clock skew) treated as age=0 → eff = raw."""
    rows = [{
        "symbol": "XOM", "max_conf": 0.8,
        "latest_at": datetime.now(timezone.utc) + timedelta(hours=1),
        "direction": "bullish", "catalyst_type": "macro",
    }]
    result = compute_priority(rows, ["XOM"], DEFAULT_CFG)
    assert result[0]["eff"] == pytest.approx(0.8, abs=0.01)


def test_accepts_iso_string_latest_at():
    """latest_at may arrive as an ISO string from the DB."""
    rows = [{
        "symbol": "XOM", "max_conf": 0.9,
        "latest_at": (datetime.now(timezone.utc)
                      - timedelta(hours=6)).isoformat(),
        "direction": "bullish", "catalyst_type": "macro",
    }]
    result = compute_priority(rows, ["XOM"], DEFAULT_CFG)
    assert len(result) == 1


# --- trigger gate ----------------------------------------------------------

@patch("src.orchestration.fast_path.load_bot_state")
def test_should_trigger_when_gap_exceeds_threshold(mock_load):
    # last_cycle 3h ago, last_trigger None → trigger
    mock_load.side_effect = lambda k: (
        (datetime.now(timezone.utc) - timedelta(hours=3)).isoformat()
        if k == "last_stock_cycle_completed_at" else None
    )
    trigger, idle = should_trigger(DEFAULT_CFG)
    assert trigger is True
    assert 2.9 < idle < 3.1


@patch("src.orchestration.fast_path.load_bot_state")
def test_should_not_trigger_when_gap_small(mock_load):
    mock_load.side_effect = lambda k: (
        (datetime.now(timezone.utc) - timedelta(minutes=30)).isoformat()
        if k == "last_stock_cycle_completed_at" else None
    )
    trigger, idle = should_trigger(DEFAULT_CFG)
    assert trigger is False


@patch("src.orchestration.fast_path.load_bot_state")
def test_should_not_retrigger_within_rate_limit(mock_load):
    """Fast-path fired 30 min ago: don't retrigger even if cycle gap is large."""
    def get(k):
        if k == "last_stock_cycle_completed_at":
            return (datetime.now(timezone.utc) - timedelta(hours=3)).isoformat()
        if k == "last_fast_path_triggered_at":
            return (datetime.now(timezone.utc) - timedelta(minutes=30)).isoformat()
        return None
    mock_load.side_effect = get
    trigger, _ = should_trigger(DEFAULT_CFG)
    assert trigger is False


@patch("src.orchestration.fast_path.persist_cycle_complete")
@patch("src.orchestration.fast_path.load_bot_state", return_value=None)
def test_first_run_does_not_trigger(mock_load, mock_persist):
    """No last_cycle stored → don't trigger, seed the baseline instead."""
    trigger, _ = should_trigger(DEFAULT_CFG)
    assert trigger is False
    mock_persist.assert_called_once()


# --- check_and_build integration (logic only) ------------------------------

@patch("src.orchestration.fast_path.fetch_recent_assessments")
@patch("src.orchestration.fast_path.should_trigger")
def test_check_and_build_returns_empty_when_not_triggered(mock_trigger, mock_fetch):
    mock_trigger.return_value = (False, 1.0)
    priority, idle = check_and_build_priority_list(["XOM"], DEFAULT_CFG)
    assert priority == []
    mock_fetch.assert_not_called()


@patch("src.orchestration.fast_path.fetch_recent_assessments")
@patch("src.orchestration.fast_path.should_trigger")
def test_check_and_build_returns_priority_when_triggered(mock_trigger, mock_fetch):
    mock_trigger.return_value = (True, 50.0)
    mock_fetch.return_value = [_row("XOM", 0.9, 6)]
    priority, idle = check_and_build_priority_list(["XOM", "CVX"], DEFAULT_CFG)
    assert len(priority) == 1
    assert priority[0]["symbol"] == "XOM"
    assert idle == 50.0


# --- alert formatter -------------------------------------------------------

def test_format_alert_message_dry_run_prefix():
    priority = [
        {"symbol": "XOM", "eff": 0.63, "raw": 0.9, "age_h": 14.0,
         "catalyst_type": "macro"},
        {"symbol": "SLB", "eff": 0.59, "raw": 0.85, "age_h": 16.0,
         "catalyst_type": "macro"},
    ]
    msg = format_alert_message(priority, 52.3, dry_run=True)
    assert "[DRY-RUN]" in msg
    assert "52.3h" in msg
    assert "XOM" in msg and "SLB" in msg
    assert "would prioritize" in msg


def test_format_alert_message_live_no_prefix():
    priority = [{"symbol": "XOM", "eff": 0.63, "raw": 0.9, "age_h": 14.0,
                 "catalyst_type": "macro"}]
    msg = format_alert_message(priority, 50.0)
    assert "[DRY-RUN]" not in msg
    assert "prioritizing" in msg


def test_format_alert_message_truncates_after_eight():
    priority = [
        {"symbol": f"S{i}", "eff": 0.5, "raw": 0.8, "age_h": 10.0,
         "catalyst_type": "macro"}
        for i in range(12)
    ]
    msg = format_alert_message(priority, 50.0)
    assert "and 4 more" in msg


# --- apply_priority_order — the splice logic ------------------------------

def test_apply_priority_order_prepends_and_dedupes():
    from src.orchestration.fast_path import apply_priority_order
    priority = ["XOM", "SLB"]
    watch = ["AAPL", "NVDA", "XOM", "MSFT", "SLB", "GOOG"]
    result = apply_priority_order(priority, watch)
    assert result == ["XOM", "SLB", "AAPL", "NVDA", "MSFT", "GOOG"]
    # Priority symbols appear exactly once, at the front, in priority order
    assert result[:2] == priority
    # Every original watch-list member is still present
    for s in watch:
        assert s in result


def test_apply_priority_order_empty_priority_is_noop():
    from src.orchestration.fast_path import apply_priority_order
    watch = ["AAPL", "NVDA", "MSFT"]
    assert apply_priority_order([], watch) == watch


def test_apply_priority_order_priority_not_in_watch_list():
    """Priority symbol not in watch_list still prepended — caller's
    responsibility; compute_priority already filters to watch_list."""
    from src.orchestration.fast_path import apply_priority_order
    result = apply_priority_order(["FOO"], ["AAPL", "NVDA"])
    assert result == ["FOO", "AAPL", "NVDA"]


def test_apply_priority_order_preserves_tail_order():
    from src.orchestration.fast_path import apply_priority_order
    watch = ["A", "B", "C", "D", "E"]
    priority = ["D"]
    assert apply_priority_order(priority, watch) == ["D", "A", "B", "C", "E"]
