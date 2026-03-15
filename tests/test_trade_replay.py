"""Tests for the trade replay backtester."""

from src.analysis.trade_replay import (
    replay_trade, ExitParams, ReplayResult,
    format_sweep_report, format_quality_report,
)


def _make_prices(values):
    """Helper: create price path from a list of floats."""
    return [{'price': p, 'timestamp': f'2026-01-01T{i:02d}:00:00'} for i, p in enumerate(values)]


# --- replay_trade tests ---

def test_replay_stop_loss():
    """Trade should exit at stop loss when price drops below threshold."""
    prices = _make_prices([100, 99, 98, 97, 96, 95])  # steadily declining
    params = ExitParams(stop_loss_pct=0.03, take_profit_pct=0.10)
    result = replay_trade(100.0, 1.0, prices, params)

    assert result.replay_exit_reason == 'stop_loss'
    assert result.replay_exit_price == 97.0  # -3%
    assert result.replay_pnl < 0


def test_replay_take_profit():
    """Trade should exit at take profit when price rises above threshold."""
    prices = _make_prices([100, 102, 104, 106, 108, 110, 112])
    params = ExitParams(stop_loss_pct=0.03, take_profit_pct=0.10, trailing_enabled=False)
    result = replay_trade(100.0, 1.0, prices, params)

    assert result.replay_exit_reason == 'take_profit'
    assert result.replay_exit_price == 110.0  # +10%
    assert result.replay_pnl > 0


def test_replay_trailing_stop():
    """Trailing stop should trigger after activation when price pulls back."""
    # Price goes up 5%, then pulls back 2%
    prices = _make_prices([100, 102, 104, 105, 104, 103])
    params = ExitParams(
        stop_loss_pct=0.10,  # wide SL so it doesn't trigger
        take_profit_pct=0.20,  # wide TP so it doesn't trigger
        trailing_enabled=True,
        trailing_activation=0.03,  # activates at +3%
        trailing_distance=0.015,  # trails at 1.5%
    )
    result = replay_trade(100.0, 1.0, prices, params)

    assert result.replay_exit_reason == 'trailing_stop'
    # Peak is 105, trailing triggers at 105 * (1 - 0.015) = 103.425
    # Price 103 < 103.425, so triggers
    assert result.replay_exit_price == 103.0
    assert result.replay_pnl > 0  # Still profitable


def test_replay_no_exit():
    """If no exit condition met, should use last price."""
    prices = _make_prices([100, 101, 100.5, 101.5, 100.8])
    params = ExitParams(stop_loss_pct=0.10, take_profit_pct=0.20, trailing_enabled=False)
    result = replay_trade(100.0, 1.0, prices, params)

    assert result.replay_exit_reason == 'end_of_data'
    assert result.replay_exit_price == 100.8


def test_replay_empty_prices():
    """Empty price path should return no_price_data."""
    result = replay_trade(100.0, 1.0, [], ExitParams())
    assert result.replay_exit_reason == 'no_price_data'


def test_replay_mfe_mae_tracked():
    """MFE and MAE should be correctly tracked."""
    prices = _make_prices([100, 105, 103, 108, 95])  # up, down, up more, crash
    params = ExitParams(stop_loss_pct=0.06, take_profit_pct=0.20, trailing_enabled=False)
    result = replay_trade(100.0, 1.0, prices, params)

    # MFE should be at 108: (108-100)/100 = 0.08
    assert result.max_favorable_excursion == 0.08
    # MAE should be at 95: (95-100)/100 = -0.05
    assert result.max_adverse_excursion == -0.05


def test_replay_sl_before_trailing():
    """SL should trigger before trailing if price drops immediately."""
    prices = _make_prices([100, 97, 95])
    params = ExitParams(
        stop_loss_pct=0.03,
        take_profit_pct=0.10,
        trailing_enabled=True,
        trailing_activation=0.02,
        trailing_distance=0.015,
    )
    result = replay_trade(100.0, 1.0, prices, params)

    assert result.replay_exit_reason == 'stop_loss'
    assert result.replay_exit_price == 97.0


def test_replay_fee_deducted():
    """Fees should be deducted from PnL."""
    prices = _make_prices([100, 110])  # +10%
    params = ExitParams(stop_loss_pct=0.05, take_profit_pct=0.09, trailing_enabled=False)
    result = replay_trade(100.0, 1.0, prices, params)

    # PnL = (110 - 100) * 1.0 - (110 * 1.0 * 0.001) = 10 - 0.11 = 9.89
    assert result.replay_exit_reason == 'take_profit'
    assert abs(result.replay_pnl - 9.89) < 0.01


# --- format tests ---

def test_format_sweep_report():
    """Sweep report should format without errors."""
    sweep = {
        'trade_count': 10,
        'current': {'total_pnl': 50.0, 'win_rate': 60.0, 'profit_factor': 1.5,
                    'avg_mfe': 3.2, 'avg_mae': -2.1, 'exit_reasons': {'stop_loss': 4, 'take_profit': 6}},
        'best': {'params': {'stop_loss_pct': 0.03, 'take_profit_pct': 0.08,
                            'trailing_activation': 0.02, 'trailing_distance': 0.015},
                 'total_pnl': 75.0, 'win_rate': 70.0, 'profit_factor': 2.0,
                 'exit_reasons': {'trailing_stop': 5, 'take_profit': 5}},
        'sweep': [],
    }
    report = format_sweep_report(sweep)
    assert 'Exit Parameter Sweep' in report
    assert '$75.00' in report


def test_format_quality_report():
    """Quality report should format without errors."""
    quality = {
        'total_signals': 20,
        'by_catalyst': {'regulatory': {'count': 5, 'win_rate': 80.0, 'avg_pnl': 10.0, 'total_pnl': 50.0}},
        'by_confidence': {'0.7-0.8': {'count': 10, 'win_rate': 60.0, 'avg_pnl': 5.0, 'total_pnl': 50.0}},
        'by_exit_reason': {'stop_loss': {'count': 8, 'win_rate': 0.0, 'avg_pnl': -5.0}},
        'optimal_threshold': {'threshold': 0.65, 'total_pnl': 80.0, 'trades': 15, 'win_rate': 66.7},
    }
    report = format_quality_report(quality)
    assert 'Signal Quality Analysis' in report
    assert 'regulatory' in report
    assert '0.65' in report
