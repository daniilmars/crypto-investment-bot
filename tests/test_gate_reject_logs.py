"""Tests for [GATE_REJECT] structured logging in pre_trade_gates.

Each rejection path must emit a line matching `[GATE_REJECT] symbol=X gate=Y`
so the /check-news diagnostic can aggregate the signal funnel by gate.
"""

from unittest.mock import patch

from src.orchestration.pre_trade_gates import check_buy_gates, _log_gate_reject


def _captured_gate_lines(mock_log) -> list[str]:
    """Extract [GATE_REJECT] lines from a mocked log.info collector."""
    lines = []
    for call in mock_log.info.call_args_list:
        args, _kwargs = call
        if args and isinstance(args[0], str) and args[0].startswith("[GATE_REJECT]"):
            lines.append(args[0])
    return lines


# --- helper ---

def test_log_gate_reject_minimal_format():
    with patch("src.orchestration.pre_trade_gates.log") as mock_log:
        _log_gate_reject("BTC", "sector_limit")
    msg = mock_log.info.call_args.args[0]
    assert msg.startswith("[GATE_REJECT] ")
    assert "symbol=BTC" in msg
    assert "gate=sector_limit" in msg


def test_log_gate_reject_with_optional_fields():
    with patch("src.orchestration.pre_trade_gates.log") as mock_log:
        _log_gate_reject("NVDA", "max_concurrent_positions",
                         strategy="auto", asset_type="stock",
                         max=10, current=10)
    msg = mock_log.info.call_args.args[0]
    assert "strategy=auto" in msg
    assert "asset=stock" in msg
    assert "max=10" in msg
    assert "current=10" in msg


def test_log_gate_reject_skips_empty_fields():
    """None / empty-string values should not be emitted."""
    with patch("src.orchestration.pre_trade_gates.log") as mock_log:
        _log_gate_reject("BTC", "signal_cooldown",
                         strategy="", asset_type=None)
    msg = mock_log.info.call_args.args[0]
    assert "strategy=" not in msg
    assert "asset=" not in msg


# --- check_buy_gates emits [GATE_REJECT] on each rejection path ---

def test_macro_regime_rejection_emits_structured_line():
    with patch("src.orchestration.pre_trade_gates.log") as mock_log:
        allowed, mult, reason = check_buy_gates(
            "BTC", [], max_positions=5,
            suppress_buys=True, macro_multiplier=0.0,
            asset_type="crypto", label="AUTO")
    assert allowed is False
    lines = _captured_gate_lines(mock_log)
    assert any("gate=macro_regime_risk_off" in line for line in lines)
    assert any("symbol=BTC" in line for line in lines)


def test_position_already_open_emits_structured_line():
    positions = [{"symbol": "BTC", "status": "OPEN"}]
    with patch("src.orchestration.pre_trade_gates.log") as mock_log:
        allowed, _mult, _reason = check_buy_gates(
            "BTC", positions, max_positions=5,
            suppress_buys=False, macro_multiplier=1.0,
            asset_type="crypto", label="AUTO")
    assert allowed is False
    lines = _captured_gate_lines(mock_log)
    assert any("gate=position_already_open" in line for line in lines)


def test_max_positions_emits_structured_line():
    positions = [
        {"symbol": f"S{i}", "status": "OPEN"} for i in range(5)
    ]
    with patch("src.database.get_pending_orders", return_value=[]), \
         patch("src.orchestration.pre_trade_gates.log") as mock_log:
        allowed, _mult, _reason = check_buy_gates(
            "NEW", positions, max_positions=5,
            suppress_buys=False, macro_multiplier=1.0,
            asset_type="crypto", label="AUTO")
    assert allowed is False
    lines = _captured_gate_lines(mock_log)
    gate_line = next((l for l in lines
                      if "gate=max_concurrent_positions" in l), None)
    assert gate_line is not None
    assert "max=5" in gate_line
    assert "current=5" in gate_line


def test_allowed_path_emits_no_gate_reject():
    """Happy path: no [GATE_REJECT] line should fire."""
    with patch("src.database.get_pending_orders", return_value=[]), \
         patch("src.orchestration.pre_trade_gates.check_sector_limit",
               return_value=(True, "")), \
         patch("src.orchestration.pre_trade_gates.check_event_gate",
               return_value=("allow", 1.0, "")), \
         patch("src.orchestration.pre_trade_gates.get_asset_class_concentration",
               return_value=1.0), \
         patch("src.orchestration.pre_trade_gates.log") as mock_log:
        allowed, mult, _reason = check_buy_gates(
            "BTC", [], max_positions=5,
            suppress_buys=False, macro_multiplier=1.0,
            asset_type="crypto", label="AUTO")
    assert allowed is True
    assert mult == 1.0
    lines = _captured_gate_lines(mock_log)
    assert not lines  # no gate rejections on happy path
