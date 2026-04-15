# tests/test_trend_alignment.py
"""Tests for multi-timeframe trend alignment scoring."""

from src.analysis.trend_alignment import compute_trend_alignment


def _rising(n: int, start: float = 100.0, step: float = 1.0) -> list[float]:
    return [start + i * step for i in range(n)]


def _falling(n: int, start: float = 200.0, step: float = 1.0) -> list[float]:
    return [start - i * step for i in range(n)]


class TestComputeTrendAlignment:
    """compute_trend_alignment: agreement scoring across timeframes."""

    def test_all_three_bullish_aligned(self):
        daily = _rising(40)      # last > SMA20
        weekly = _rising(40)
        monthly = _rising(20)
        result = compute_trend_alignment(daily, 'bullish', weekly, monthly)
        assert result['score'] == 1.0
        assert result['agreement_count'] == 3
        assert result['timeframes_evaluated'] == 3
        assert result['details'] == {'daily': True, 'weekly': True, 'monthly': True}

    def test_all_three_bearish_aligned(self):
        daily = _falling(40)
        weekly = _falling(40)
        monthly = _falling(20)
        result = compute_trend_alignment(daily, 'bearish', weekly, monthly)
        assert result['score'] == 1.0
        assert result['agreement_count'] == 3

    def test_partial_agreement_two_of_three(self):
        # Bullish signal — daily and weekly rising, monthly falling
        daily = _rising(40)
        weekly = _rising(40)
        monthly = _falling(20)
        result = compute_trend_alignment(daily, 'bullish', weekly, monthly)
        assert result['agreement_count'] == 2
        assert result['timeframes_evaluated'] == 3
        assert abs(result['score'] - 2 / 3) < 1e-9
        assert result['details']['monthly'] is False

    def test_only_daily_available_skips_others(self):
        # Insufficient weekly/monthly data → those timeframes skipped, not failed
        daily = _rising(30)
        result = compute_trend_alignment(daily, 'bullish',
                                          weekly_closes=None, monthly_closes=[100.0])
        assert result['timeframes_evaluated'] == 1
        assert result['score'] == 1.0
        assert result['details']['weekly'] is None
        assert result['details']['monthly'] is None

    def test_no_data_returns_neutral_score(self):
        # No timeframe has enough data → don't penalize, score = 1.0
        result = compute_trend_alignment([100.0], 'bullish')
        assert result['timeframes_evaluated'] == 0
        assert result['score'] == 1.0
        assert result['agreement_count'] == 0

    def test_disagreement_lowers_score(self):
        # Bullish signal, all timeframes falling → score 0.0
        daily = _falling(40)
        weekly = _falling(40)
        monthly = _falling(20)
        result = compute_trend_alignment(daily, 'bullish', weekly, monthly)
        assert result['score'] == 0.0
        assert result['agreement_count'] == 0

    def test_invalid_direction_returns_skipped(self):
        # Neither 'bullish' nor 'bearish' → all timeframes skipped
        daily = _rising(40)
        result = compute_trend_alignment(daily, 'neutral')
        assert result['timeframes_evaluated'] == 0
        assert result['score'] == 1.0

    def test_custom_periods_respected(self):
        # 12 data points — too few for default 20-period daily SMA → skipped
        daily = _rising(12)
        result = compute_trend_alignment(daily, 'bullish')
        assert result['details']['daily'] is None
        # But with daily_period=10, it should evaluate
        result2 = compute_trend_alignment(daily, 'bullish', daily_period=10)
        assert result2['details']['daily'] is True
