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


# --- impact_rank / impact_basis (PR-C surface in Mini App) ------------------

def test_build_rationale_impact_rank_surfaced():
    row = {
        "catalyst_type": "narrative",
        "gemini_direction": "bullish",
        "gemini_confidence": 0.70,
        "impact_rank": 2,
        "impact_basis": "secondary beneficiary",
    }
    rat = _build_rationale(row)
    assert rat["impact_rank"] == 2
    assert rat["impact_basis"] == "secondary beneficiary"


def test_build_rationale_impact_rank_string_coerced():
    """Gemini sometimes returns the number as a string."""
    row = {
        "catalyst_type": "narrative",
        "gemini_direction": "bullish",
        "gemini_confidence": 0.70,
        "impact_rank": "3",
        "impact_basis": "thematic exposure",
    }
    rat = _build_rationale(row)
    assert rat["impact_rank"] == 3


def test_build_rationale_impact_rank_invalid_becomes_none():
    row = {
        "catalyst_type": "narrative",
        "gemini_direction": "bullish",
        "gemini_confidence": 0.70,
        "impact_rank": "not a number",
    }
    rat = _build_rationale(row)
    assert rat["impact_rank"] is None


# --- grounding_urls fallback (PR-B surface in Mini App) ---------------------

def test_grounding_used_when_scraper_sources_empty():
    """When source_names is empty, Gemini grounding URLs become source chips."""
    import json
    row = {
        "catalyst_type": "narrative",
        "gemini_direction": "bullish",
        "gemini_confidence": 0.65,
        "source_names": "",  # scraper missed
        "grounding_urls": json.dumps([
            "https://www.reuters.com/defense/hii-shipyard",
            "https://www.bloomberg.com/news/lhx-q1",
            "https://www.reuters.com/markets/oil-up",  # dup host → dedupe
        ]),
    }
    rat = _build_rationale(row)
    assert rat is not None
    assert "gemini:reuters.com" in rat["sources"]
    assert "gemini:bloomberg.com" in rat["sources"]
    # Dedup means reuters.com only appears once
    assert sum(1 for s in rat["sources"] if "reuters" in s) == 1


def test_scraper_sources_preferred_over_grounding():
    """If scraper found articles, grounding URLs are NOT used."""
    import json
    row = {
        "catalyst_type": "fund_flow",
        "gemini_direction": "bullish",
        "gemini_confidence": 0.75,
        "source_names": "CoinDesk,The Block",
        "grounding_urls": json.dumps(["https://reuters.com/x"]),
    }
    rat = _build_rationale(row)
    assert rat["sources"] == ["CoinDesk", "The Block"]


def test_grounding_fallback_handles_malformed_json():
    row = {
        "catalyst_type": "narrative",
        "gemini_direction": "bullish",
        "gemini_confidence": 0.65,
        "source_names": "",
        "grounding_urls": "not valid json {",
    }
    rat = _build_rationale(row)
    # Should not crash, sources should be empty
    assert rat["sources"] == []


def test_build_rationale_returns_none_with_only_grounding_but_no_signal():
    """If literally only grounding_urls exists with no Gemini fields,
    we still build the rationale block (better than nothing)."""
    import json
    row = {
        "grounding_urls": json.dumps(["https://reuters.com/a"]),
    }
    rat = _build_rationale(row)
    assert rat is not None  # grounding alone is enough to render a block
    assert "gemini:reuters.com" in rat["sources"]


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


# --- recent_trades_data payload shape (exit_reasoning + trailing_stop_peak) ---

import sqlite3
from unittest.mock import patch


def _closed_trades_conn():
    """Build an in-memory SQLite DB with one CLOSED trade and no related rows."""
    conn = sqlite3.connect(':memory:')
    conn.execute("""
        CREATE TABLE trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol TEXT, order_id TEXT,
            trading_strategy TEXT, asset_type TEXT,
            entry_price REAL, exit_price REAL, quantity REAL, pnl REAL,
            status TEXT,
            entry_timestamp TEXT, exit_timestamp TEXT,
            exit_reason TEXT, exit_reasoning TEXT, trailing_stop_peak REAL,
            dynamic_sl_pct REAL, dynamic_tp_pct REAL, trade_reason TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE signal_attribution (
            id INTEGER, trade_order_id TEXT,
            gemini_direction TEXT, gemini_confidence REAL,
            catalyst_type TEXT, source_names TEXT, signal_timestamp TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE gemini_assessments (
            id INTEGER, symbol TEXT, created_at TEXT,
            reasoning TEXT, key_headline TEXT, risk_factors TEXT,
            catalyst_freshness TEXT, hype_vs_fundamental TEXT, market_mood TEXT,
            impact_rank INTEGER, impact_basis TEXT, grounding_urls TEXT
        )
    """)
    return conn


def test_recent_trades_payload_surfaces_exit_reasoning_and_trailing_peak():
    conn = _closed_trades_conn()
    conn.execute(
        "INSERT INTO trades (symbol, order_id, trading_strategy, asset_type, "
        "entry_price, exit_price, quantity, pnl, status, "
        "entry_timestamp, exit_timestamp, exit_reason, exit_reasoning, "
        "trailing_stop_peak) VALUES "
        "('NVDA','P_1','auto','stock',180,200,5,100,'CLOSED',"
        "'2026-04-08 18:33:28','2026-04-17 18:25:40','trailing_stop',"
        "'Trailed 2.0% from peak $204.00',204.0)")
    conn.commit()

    with patch('src.database.get_db_connection', return_value=conn), \
         patch('src.database.release_db_connection'):
        from src.api.miniapp_queries import recent_trades_data
        result = recent_trades_data(limit=5)

    assert len(result['trades']) == 1
    t = result['trades'][0]
    assert t['exit_reasoning'] == 'Trailed 2.0% from peak $204.00'
    assert t['trailing_stop_peak'] == 204.0
    assert t['exit_reason'] == 'trailing_stop'


def test_recent_trades_payload_null_exit_reasoning_for_pre_pr_trade():
    """Pre-PR trade has NULL exit_reasoning — must survive as None, not crash."""
    conn = _closed_trades_conn()
    conn.execute(
        "INSERT INTO trades (symbol, order_id, trading_strategy, asset_type, "
        "entry_price, exit_price, quantity, pnl, status, "
        "entry_timestamp, exit_timestamp, exit_reason) VALUES "
        "('BTC','P_OLD','auto','crypto',70000,72000,0.01,20,'CLOSED',"
        "'2026-04-01 10:00:00','2026-04-05 12:00:00','trailing_stop')")
    conn.commit()

    with patch('src.database.get_db_connection', return_value=conn), \
         patch('src.database.release_db_connection'):
        from src.api.miniapp_queries import recent_trades_data
        result = recent_trades_data(limit=5)

    t = result['trades'][0]
    assert t['exit_reasoning'] is None
    assert t['trailing_stop_peak'] is None


def test_recent_trades_payload_trailing_stop_peak_coerced_to_float():
    """Trailing peak stored as REAL should come out as Python float."""
    conn = _closed_trades_conn()
    conn.execute(
        "INSERT INTO trades (symbol, order_id, trading_strategy, asset_type, "
        "entry_price, exit_price, quantity, pnl, status, "
        "entry_timestamp, exit_timestamp, exit_reason, trailing_stop_peak) "
        "VALUES ('RIVN','P_2','auto','stock',15.61,17.39,20,24.81,'CLOSED',"
        "'2026-03-23 14:03:02','2026-04-20 14:19:49','trailing_stop',17.80)")
    conn.commit()

    with patch('src.database.get_db_connection', return_value=conn), \
         patch('src.database.release_db_connection'):
        from src.api.miniapp_queries import recent_trades_data
        result = recent_trades_data(limit=5)

    t = result['trades'][0]
    assert isinstance(t['trailing_stop_peak'], float)
    assert t['trailing_stop_peak'] == pytest.approx(17.80)
