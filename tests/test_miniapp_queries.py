"""Tests for src/api/miniapp_queries.py — pure helpers only."""

import pytest

from src.api.miniapp_queries import (
    _parse_risk_factors, _build_rationale, _sl_tp_distances,
)


# --- _parse_risk_factors ----------------------------------------------------

def test_parse_risk_factors_none_and_empty():
    assert _parse_risk_factors(None) == []
    assert _parse_risk_factors("") == []
    assert _parse_risk_factors("null") == []


def test_parse_risk_factors_already_list():
    assert _parse_risk_factors(["regulatory", "macro"]) == ["regulatory", "macro"]


def test_parse_risk_factors_json_array_string():
    assert _parse_risk_factors('["regulatory", "macro"]') == ["regulatory", "macro"]


def test_parse_risk_factors_json_quoted_string():
    # Old rows may wrap a single risk as a JSON string, not an array
    assert _parse_risk_factors('"single risk"') == ["single risk"]


def test_parse_risk_factors_plain_text_comma():
    assert _parse_risk_factors("regulatory, macro, earnings") == [
        "regulatory", "macro", "earnings"]


def test_parse_risk_factors_plain_text_semicolon():
    assert _parse_risk_factors("risk1; risk2") == ["risk1", "risk2"]


def test_parse_risk_factors_single_plain_string():
    assert _parse_risk_factors("just one risk") == ["just one risk"]


# --- _build_rationale ------------------------------------------------------

def test_build_rationale_all_fields_populated():
    row = {
        "catalyst_type": "macro",
        "gemini_direction": "bullish",
        "gemini_confidence": 0.85,
        "key_headline": "Hormuz reopens",
        "reasoning": "Strait reopening frees tanker flow",
        "source_names": "Bloomberg Markets,Reuters,WSJ",
        "risk_factors": '["regulatory","macro"]',
        "catalyst_freshness": "recent",
        "hype_vs_fundamental": "fundamental",
        "market_mood": "cautiously optimistic",
        "signal_timestamp": "2026-04-19T10:00:00+00:00",
        "trade_reason": None,
    }
    rat = _build_rationale(row)
    assert rat is not None
    assert rat["gemini_direction"] == "bullish"
    assert rat["gemini_confidence"] == 0.85
    assert rat["catalyst_type"] == "macro"
    assert rat["key_headline"] == "Hormuz reopens"
    assert rat["sources"] == ["Bloomberg Markets", "Reuters", "WSJ"]
    assert rat["risk_factors"] == ["regulatory", "macro"]


def test_build_rationale_returns_none_when_everything_empty():
    empty = {k: None for k in (
        "catalyst_type", "gemini_direction", "gemini_confidence",
        "key_headline", "reasoning", "source_names", "risk_factors",
        "trade_reason")}
    empty["source_names"] = ""
    assert _build_rationale(empty) is None


def test_build_rationale_caps_sources_at_10_and_risks_at_5():
    row = {
        "catalyst_type": "macro",
        "gemini_direction": "bullish",
        "gemini_confidence": 0.7,
        "source_names": ",".join(f"src{i}" for i in range(20)),
        "risk_factors": ",".join(f"risk{i}" for i in range(15)),
    }
    rat = _build_rationale(row)
    assert len(rat["sources"]) == 10
    assert len(rat["risk_factors"]) == 5


def test_build_rationale_legacy_only_trade_reason():
    """Pre-WS1 trade with nothing joined except trade_reason fallback text."""
    row = {"trade_reason": "Gemini bullish (0.72): catalyst=macro"}
    rat = _build_rationale(row)
    assert rat is not None
    assert rat["trade_reason"] == "Gemini bullish (0.72): catalyst=macro"


# --- _sl_tp_distances ------------------------------------------------------

def test_sl_tp_distances_both_populated():
    # Entry $100, SL 10%, TP 50% → SL=$90, TP=$150
    # Current $110 → SL is $20 below (-18% from current), TP is $40 above (+36%)
    d = _sl_tp_distances(100.0, 110.0, 0.10, 0.50)
    assert d["sl_price"] == pytest.approx(90.0)
    assert d["tp_price"] == pytest.approx(150.0)
    # Distance AWAY from stop (positive = safe)
    assert d["sl_distance_pct"] == pytest.approx(
        (110 - 90) / 110 * 100, abs=0.1)
    # Distance TO take-profit (positive = still climbing to target)
    assert d["tp_distance_pct"] == pytest.approx(
        (150 - 110) / 110 * 100, abs=0.1)


def test_sl_tp_distances_missing_pcts():
    d = _sl_tp_distances(100.0, 110.0, None, None)
    assert d == {"sl_price": None, "tp_price": None,
                 "sl_distance_pct": None, "tp_distance_pct": None}


def test_sl_tp_distances_zero_entry_safe():
    """Degenerate input shouldn't crash."""
    d = _sl_tp_distances(0.0, 100.0, 0.1, 0.5)
    assert d["sl_price"] is None
    assert d["tp_price"] is None


def test_sl_tp_distances_past_stop_loss_negative_distance():
    """Current price below stop-loss → negative distance (alert-worthy)."""
    d = _sl_tp_distances(100.0, 85.0, 0.10, 0.50)  # SL=$90, current $85
    # sl_distance = (85 - 90) / 85 * 100 = negative
    assert d["sl_distance_pct"] < 0
