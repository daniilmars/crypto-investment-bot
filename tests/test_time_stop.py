"""Tests for src/orchestration/time_stop.py."""

from datetime import datetime, timedelta, timezone

from src.orchestration.time_stop import should_time_stop, _parse_entry_timestamp


CFG_ON = {
    "enabled": True,
    "dry_run": False,
    "max_hold_days": 14,
    "min_gain_pct": 0.02,
    "max_loss_pct": 0.05,
}


def _pos(entry_price=100.0, days_old=15, strategy_type=None, entry_ts=None):
    now = datetime.now(timezone.utc)
    ts = entry_ts if entry_ts is not None else (now - timedelta(days=days_old)).isoformat()
    return {
        "symbol": "TEST",
        "entry_price": entry_price,
        "entry_timestamp": ts,
        "strategy_type": strategy_type,
    }


# ---- guards ----------------------------------------------------------------

def test_disabled_config_never_fires():
    fire, reason = should_time_stop(_pos(), 100.5, {"enabled": False}, "auto")
    assert fire is False
    assert reason == "disabled"


def test_none_config_never_fires():
    fire, _ = should_time_stop(_pos(), 100.5, None, "auto")
    assert fire is False


def test_empty_dict_never_fires():
    fire, _ = should_time_stop(_pos(), 100.5, {}, "auto")
    assert fire is False


def test_longterm_strategy_exempt():
    fire, reason = should_time_stop(_pos(), 100.5, CFG_ON, "longterm")
    assert fire is False
    assert "longterm" in reason


def test_manual_strategy_exempt():
    fire, reason = should_time_stop(_pos(), 100.5, CFG_ON, "manual")
    assert fire is False
    assert "manual" in reason


def test_strategic_position_exempt():
    fire, reason = should_time_stop(
        _pos(strategy_type="growth"), 100.5, CFG_ON, "auto")
    assert fire is False
    assert "strategic" in reason


def test_missing_entry_price_no_fire():
    p = _pos()
    p["entry_price"] = 0
    assert should_time_stop(p, 100.5, CFG_ON, "auto")[0] is False


def test_missing_entry_timestamp_no_fire():
    p = _pos()
    p["entry_timestamp"] = None
    assert should_time_stop(p, 100.5, CFG_ON, "auto")[0] is False


def test_garbage_entry_timestamp_no_fire():
    p = _pos()
    p["entry_timestamp"] = "not-a-date"
    assert should_time_stop(p, 100.5, CFG_ON, "auto")[0] is False


# ---- age boundary ---------------------------------------------------------

def test_young_position_below_age_threshold():
    fire, reason = should_time_stop(_pos(days_old=13), 100.5, CFG_ON, "auto")
    assert fire is False
    assert "age" in reason


def test_age_above_threshold_fires():
    fire, _ = should_time_stop(_pos(days_old=15), 100.5, CFG_ON, "auto")
    assert fire is True


# ---- pnl band -------------------------------------------------------------

def test_winning_position_above_gain_threshold_exempt():
    # +3% gain — above +2% threshold → would've been trailing-stop territory
    fire, reason = should_time_stop(_pos(days_old=15), 103.0, CFG_ON, "auto")
    assert fire is False
    assert "winning" in reason


def test_losing_position_at_SL_range_exempt():
    # -6% — beyond -5% threshold (SL should have fired, not our job)
    fire, reason = should_time_stop(_pos(days_old=15), 94.0, CFG_ON, "auto")
    assert fire is False
    assert "SL range" in reason


def test_flat_position_fires():
    fire, reason = should_time_stop(_pos(days_old=15), 100.5, CFG_ON, "auto")
    assert fire is True
    assert "time_stop" in reason


def test_mild_loss_in_band_fires():
    # -3% → inside the band (below +2% gain, above -5% loss)
    fire, _ = should_time_stop(_pos(days_old=15), 97.0, CFG_ON, "auto")
    assert fire is True


def test_exactly_at_gain_threshold_does_not_fire():
    # Equal to min_gain → treated as winning (>=)
    fire, _ = should_time_stop(_pos(days_old=15), 102.0, CFG_ON, "auto")
    assert fire is False


def test_exactly_at_loss_threshold_does_not_fire():
    # Equal to -max_loss → treated as SL range (<=)
    fire, _ = should_time_stop(_pos(days_old=15), 95.0, CFG_ON, "auto")
    assert fire is False


# ---- determinism ---------------------------------------------------------

def test_now_kwarg_enables_deterministic_check():
    entry = datetime(2026, 4, 1, tzinfo=timezone.utc)
    pos = {
        "symbol": "X",
        "entry_price": 100.0,
        "entry_timestamp": entry.isoformat(),
    }
    now = entry + timedelta(days=15)
    fire, _ = should_time_stop(pos, 100.5, CFG_ON, "auto", now=now)
    assert fire is True


# ---- real-world scenarios from Apr 19 audit ------------------------------

def test_BRK_B_style_slow_drift_fires():
    """BRK-B #10: 39d held, -3.82% at exit, never tripped 10% SL."""
    # Simulate at day 15 with -3.82% pnl
    fire, _ = should_time_stop(_pos(days_old=15), 96.18, CFG_ON, "auto")
    assert fire is True


def test_UBER_style_flat_winner_fires():
    """UBER #42: 30.9d, +1.83% — still flat, would fire."""
    fire, _ = should_time_stop(_pos(days_old=30), 101.83, CFG_ON, "auto")
    assert fire is True


def test_RIVN_style_real_winner_exempt():
    """RIVN #50: 27.1d, +10.41% — clearly winning, should NOT fire."""
    fire, reason = should_time_stop(_pos(days_old=27), 110.41, CFG_ON, "auto")
    assert fire is False
    assert "winning" in reason


# ---- timestamp parsing --------------------------------------------------

def test_parse_iso_with_z_suffix():
    dt = _parse_entry_timestamp("2026-04-05T12:34:56Z")
    assert dt is not None
    assert dt.tzinfo is not None


def test_parse_iso_with_microseconds():
    dt = _parse_entry_timestamp("2026-04-05 12:34:56.123456")
    assert dt is not None


def test_parse_iso_with_offset():
    dt = _parse_entry_timestamp("2026-04-05 12:34:56+00:00")
    assert dt is not None


def test_parse_datetime_passthrough():
    inp = datetime(2026, 4, 5, tzinfo=timezone.utc)
    assert _parse_entry_timestamp(inp) == inp


def test_parse_empty_returns_none():
    assert _parse_entry_timestamp("") is None
    assert _parse_entry_timestamp(None) is None


def test_parse_garbage_returns_none():
    assert _parse_entry_timestamp("not a date") is None


# ---- integration: monitor_position wiring --------------------------------

import asyncio
from unittest.mock import AsyncMock, patch


def _slow_position(days_old=15):
    now = datetime.now(timezone.utc)
    return {
        "symbol": "SLOW",
        "entry_price": 100.0,
        "order_id": "test-time-stop-1",
        "quantity": 1.0,
        "entry_timestamp": (now - timedelta(days=days_old)).isoformat(),
    }


@patch('src.orchestration.position_monitor.place_order')
@patch('src.orchestration.position_monitor.send_telegram_alert', new_callable=AsyncMock)
@patch('src.orchestration.position_monitor._send_trade_exit_alert', new_callable=AsyncMock)
@patch('src.orchestration.position_monitor.bot_state')
def test_integration_dry_run_does_not_close(mock_state, mock_alert, mock_trade_alert, mock_order):
    """dry_run=True: log only, no place_order, returns 'none'."""
    from src.orchestration.position_monitor import monitor_position
    mock_state.update_trailing_stop.return_value = 100.0
    mock_state.auto_update_trailing_stop.return_value = 100.0

    pos = _slow_position(days_old=15)
    cfg = {"enabled": True, "dry_run": True, "max_hold_days": 14,
           "min_gain_pct": 0.02, "max_loss_pct": 0.05}
    result = asyncio.run(monitor_position(
        pos, 100.5,
        stop_loss_pct=0.10, take_profit_pct=0.50,
        trailing_stop_enabled=True, trailing_stop_activation=0.05,
        trailing_stop_distance=0.02,
        trading_strategy='auto', time_stop_cfg=cfg))
    assert result == 'none'
    mock_order.assert_not_called()


@patch('src.orchestration.position_monitor.place_order')
@patch('src.orchestration.position_monitor.send_telegram_alert', new_callable=AsyncMock)
@patch('src.orchestration.position_monitor._send_trade_exit_alert', new_callable=AsyncMock)
@patch('src.orchestration.position_monitor.process_closed_trade')
@patch('src.orchestration.position_monitor.bot_state')
def test_integration_real_run_closes_with_time_stop(
    mock_state, mock_proc, mock_alert, mock_trade_alert, mock_order,
):
    """dry_run=False: place_order called with exit_reason='time_stop'."""
    from src.orchestration.position_monitor import monitor_position
    mock_state.update_trailing_stop.return_value = 100.0
    mock_state.auto_update_trailing_stop.return_value = 100.0

    pos = _slow_position(days_old=15)
    cfg = {"enabled": True, "dry_run": False, "max_hold_days": 14,
           "min_gain_pct": 0.02, "max_loss_pct": 0.05}
    result = asyncio.run(monitor_position(
        pos, 100.5,
        stop_loss_pct=0.10, take_profit_pct=0.50,
        trailing_stop_enabled=True, trailing_stop_activation=0.05,
        trailing_stop_distance=0.02,
        trading_strategy='auto', time_stop_cfg=cfg))
    assert result == 'time_stop'
    mock_order.assert_called_once()
    # exit_reason kwarg must be 'time_stop'
    _, kwargs = mock_order.call_args
    assert kwargs.get('exit_reason') == 'time_stop'


@patch('src.orchestration.position_monitor.place_order')
@patch('src.orchestration.position_monitor.send_telegram_alert', new_callable=AsyncMock)
@patch('src.orchestration.position_monitor._send_trade_exit_alert', new_callable=AsyncMock)
@patch('src.orchestration.position_monitor.bot_state')
def test_integration_sl_preempts_time_stop(mock_state, mock_alert, mock_trade_alert, mock_order):
    """SL check runs before time-stop: a -12% position fires stop_loss, not time_stop."""
    from src.orchestration.position_monitor import monitor_position
    mock_state.update_trailing_stop.return_value = 100.0
    mock_state.auto_update_trailing_stop.return_value = 100.0

    pos = _slow_position(days_old=15)
    cfg = {"enabled": True, "dry_run": False, "max_hold_days": 14,
           "min_gain_pct": 0.02, "max_loss_pct": 0.05}
    result = asyncio.run(monitor_position(
        pos, 88.0,  # -12% pnl
        stop_loss_pct=0.10, take_profit_pct=0.50,
        trailing_stop_enabled=True, trailing_stop_activation=0.05,
        trailing_stop_distance=0.02,
        trading_strategy='auto', time_stop_cfg=cfg))
    assert result == 'stop_loss'
    _, kwargs = mock_order.call_args
    assert kwargs.get('exit_reason') == 'stop_loss'


@patch('src.orchestration.position_monitor.get_recent_bearish_assessment',
       return_value=None)
@patch('src.orchestration.position_monitor.place_order')
@patch('src.orchestration.position_monitor.send_telegram_alert', new_callable=AsyncMock)
@patch('src.orchestration.position_monitor._send_trade_exit_alert', new_callable=AsyncMock)
@patch('src.orchestration.position_monitor.bot_state')
def test_integration_trailing_stop_no_bearish_uses_plain_tag(
    mock_state, mock_alert, mock_trade_alert, mock_order, mock_lookup,
):
    """Feature 5.3: with no recent bearish assessment, exit_reason stays
    'trailing_stop'."""
    from src.orchestration.position_monitor import monitor_position
    mock_state.auto_update_trailing_stop.return_value = 110.0
    mock_state.update_trailing_stop.return_value = 110.0

    pos = {
        "symbol": "TEST", "entry_price": 100.0, "order_id": "ts-c1",
        "quantity": 1.0,
        "entry_timestamp": datetime.now(timezone.utc).isoformat(),
    }
    asyncio.run(monitor_position(
        pos, 107.8,
        stop_loss_pct=0.10, take_profit_pct=0.50,
        trailing_stop_enabled=True, trailing_stop_activation=0.05,
        trailing_stop_distance=0.02,
        trading_strategy='auto'))
    _, kwargs = mock_order.call_args
    assert kwargs.get('exit_reason') == 'trailing_stop'


@patch('src.orchestration.position_monitor.get_recent_bearish_assessment',
       return_value={'id': 1, 'symbol': 'TEST', 'direction': 'bearish',
                      'confidence': 0.85, 'created_at': '2026-04-19 12:00:00'})
@patch('src.orchestration.position_monitor.place_order')
@patch('src.orchestration.position_monitor.send_telegram_alert', new_callable=AsyncMock)
@patch('src.orchestration.position_monitor._send_trade_exit_alert', new_callable=AsyncMock)
@patch('src.orchestration.position_monitor.bot_state')
def test_integration_trailing_stop_with_bearish_uses_concur_tag(
    mock_state, mock_alert, mock_trade_alert, mock_order, mock_lookup,
):
    """Feature 5.3: recent bearish assessment → exit_reason becomes
    'trailing_stop_analyst_concur'."""
    from src.orchestration.position_monitor import monitor_position
    mock_state.auto_update_trailing_stop.return_value = 110.0
    mock_state.update_trailing_stop.return_value = 110.0

    pos = {
        "symbol": "TEST", "entry_price": 100.0, "order_id": "ts-c2",
        "quantity": 1.0,
        "entry_timestamp": datetime.now(timezone.utc).isoformat(),
    }
    result = asyncio.run(monitor_position(
        pos, 107.8,
        stop_loss_pct=0.10, take_profit_pct=0.50,
        trailing_stop_enabled=True, trailing_stop_activation=0.05,
        trailing_stop_distance=0.02,
        trading_strategy='auto'))
    # Return value contract preserved (cycle_runner only checks != 'none')
    assert result == 'trailing_stop'
    # But the persisted/alerted tag carries the concur info
    _, kwargs = mock_order.call_args
    assert kwargs.get('exit_reason') == 'trailing_stop_analyst_concur'
    # _send_trade_exit_alert (decorator-order: maps to `mock_alert`) receives
    # the tag as the last positional arg.
    pos_args = mock_alert.call_args[0]
    assert pos_args[-1] == 'trailing_stop_analyst_concur'


@patch('src.orchestration.position_monitor.get_recent_bearish_assessment',
       return_value=None)
@patch('src.orchestration.position_monitor.place_order')
@patch('src.orchestration.position_monitor.send_telegram_alert', new_callable=AsyncMock)
@patch('src.orchestration.position_monitor._send_trade_exit_alert', new_callable=AsyncMock)
@patch('src.orchestration.position_monitor.bot_state')
def test_integration_trailing_stop_sets_buy_cooldown_auto(
    mock_state, mock_alert, mock_trade_alert, mock_order, mock_lookup,
):
    """Feature 5.2: trailing-stop fire sets a 4h BUY cooldown to prevent
    immediate re-entry on the same signal."""
    from src.orchestration.position_monitor import monitor_position
    # Peak 110, current 107.8 → 2% drawdown ≥ trailing_stop_distance.
    mock_state.auto_update_trailing_stop.return_value = 110.0
    mock_state.update_trailing_stop.return_value = 110.0

    pos = {
        "symbol": "TEST", "entry_price": 100.0, "order_id": "ts-1",
        "quantity": 1.0,
        "entry_timestamp": datetime.now(timezone.utc).isoformat(),
    }
    result = asyncio.run(monitor_position(
        pos, 107.8,
        stop_loss_pct=0.10, take_profit_pct=0.50,
        trailing_stop_enabled=True, trailing_stop_activation=0.05,
        trailing_stop_distance=0.02,
        trading_strategy='auto'))
    assert result == 'trailing_stop'
    mock_state.set_auto_signal_cooldown.assert_called_once()
    args = mock_state.set_auto_signal_cooldown.call_args[0]
    assert args[0] == 'TEST'
    assert args[1] == 'BUY'
    mock_state.set_signal_cooldown.assert_not_called()


@patch('src.orchestration.position_monitor.get_recent_bearish_assessment',
       return_value=None)
@patch('src.orchestration.position_monitor.place_order')
@patch('src.orchestration.position_monitor.send_telegram_alert', new_callable=AsyncMock)
@patch('src.orchestration.position_monitor._send_trade_exit_alert', new_callable=AsyncMock)
@patch('src.orchestration.position_monitor.bot_state')
def test_integration_trailing_stop_sets_buy_cooldown_manual(
    mock_state, mock_alert, mock_trade_alert, mock_order, mock_lookup,
):
    """Same as above but for manual strategy → uses non-auto cooldown setter."""
    from src.orchestration.position_monitor import monitor_position
    mock_state.update_trailing_stop.return_value = 110.0
    mock_state.auto_update_trailing_stop.return_value = 110.0

    pos = {
        "symbol": "TEST", "entry_price": 100.0, "order_id": "ts-2",
        "quantity": 1.0,
        "entry_timestamp": datetime.now(timezone.utc).isoformat(),
    }
    result = asyncio.run(monitor_position(
        pos, 107.8,
        stop_loss_pct=0.10, take_profit_pct=0.50,
        trailing_stop_enabled=True, trailing_stop_activation=0.05,
        trailing_stop_distance=0.02,
        trading_strategy='manual'))
    assert result == 'trailing_stop'
    mock_state.set_signal_cooldown.assert_called_once()
    args = mock_state.set_signal_cooldown.call_args[0]
    assert args[0] == 'TEST'
    assert args[1] == 'BUY'
    mock_state.set_auto_signal_cooldown.assert_not_called()


@patch('src.orchestration.position_monitor.place_order')
@patch('src.orchestration.position_monitor.send_telegram_alert', new_callable=AsyncMock)
@patch('src.orchestration.position_monitor._send_trade_exit_alert', new_callable=AsyncMock)
@patch('src.orchestration.position_monitor.bot_state')
def test_integration_longterm_exempt_in_monitor(
    mock_state, mock_alert, mock_trade_alert, mock_order,
):
    """Even with dry_run=False and all criteria met, longterm never closes."""
    from src.orchestration.position_monitor import monitor_position
    mock_state.update_trailing_stop.return_value = 100.0
    mock_state.auto_update_trailing_stop.return_value = 100.0

    pos = _slow_position(days_old=30)
    cfg = {"enabled": True, "dry_run": False, "max_hold_days": 14,
           "min_gain_pct": 0.02, "max_loss_pct": 0.05}
    result = asyncio.run(monitor_position(
        pos, 100.5,
        stop_loss_pct=0.25, take_profit_pct=9.99,
        trailing_stop_enabled=False, trailing_stop_activation=0.0,
        trailing_stop_distance=0.0,
        trading_strategy='longterm', time_stop_cfg=cfg))
    assert result == 'none'
    mock_order.assert_not_called()
