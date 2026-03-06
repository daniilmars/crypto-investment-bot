"""Tests for src/notify/formatting.py"""

import pytest
from src.notify.formatting import (
    text_sparkline, progress_bar, pnl_emoji, pnl_sign,
    format_position_line, truncate_for_telegram, escape_md,
    format_region_label,
)


class TestTextSparkline:
    def test_basic_ascending(self):
        result = text_sparkline([1, 2, 3, 4, 5])
        assert len(result) == 5
        assert result[0] == '▁'
        assert result[-1] == '█'

    def test_basic_descending(self):
        result = text_sparkline([5, 4, 3, 2, 1])
        assert result[0] == '█'
        assert result[-1] == '▁'

    def test_flat_values(self):
        result = text_sparkline([5, 5, 5, 5])
        assert len(result) == 4
        # All same char (middle block)
        assert len(set(result)) == 1

    def test_empty_values(self):
        assert text_sparkline([]) == '▁' * 10

    def test_single_value(self):
        assert text_sparkline([42]) == '▁' * 10

    def test_resampling_long_input(self):
        result = text_sparkline(list(range(100)), width=10)
        assert len(result) == 10

    def test_short_input_not_padded(self):
        result = text_sparkline([1, 5], width=10)
        assert len(result) == 2


class TestProgressBar:
    def test_full(self):
        assert progress_bar(10, 10) == '██████████'

    def test_empty(self):
        assert progress_bar(0, 10) == '░░░░░░░░░░'

    def test_half(self):
        assert progress_bar(5, 10) == '█████░░░░░'

    def test_zero_max(self):
        assert progress_bar(5, 0) == '░░░░░░░░░░'

    def test_over_max(self):
        assert progress_bar(15, 10) == '██████████'

    def test_custom_width(self):
        result = progress_bar(3, 6, width=6)
        assert len(result) == 6
        assert result == '███░░░'


class TestPnlEmoji:
    def test_positive(self):
        assert pnl_emoji(2.5) == '🟢'

    def test_negative(self):
        assert pnl_emoji(-3.0) == '🔴'

    def test_neutral(self):
        assert pnl_emoji(0.5) == '⚪'
        assert pnl_emoji(-0.5) == '⚪'


class TestPnlSign:
    def test_positive(self):
        assert pnl_sign(3.2) == '+3.2%'

    def test_negative(self):
        assert pnl_sign(-1.5) == '-1.5%'

    def test_zero(self):
        assert pnl_sign(0.0) == '+0.0%'


class TestFormatPositionLine:
    def test_basic(self):
        line = format_position_line('NVDA', 3.2, 142.50, '▁▃▅▇█')
        assert 'NVDA' in line
        assert '+3.2%' in line
        assert '$142.50' in line
        assert '▁▃▅▇█' in line
        assert '🟢' in line

    def test_negative_pnl(self):
        line = format_position_line('TSLA', -2.1, 178.20)
        assert '🔴' in line
        assert '-2.1%' in line

    def test_no_sparkline(self):
        line = format_position_line('BTC', 0.5, 67230)
        assert '⚪' in line
        assert '▁' not in line


class TestTruncateForTelegram:
    def test_short_text_unchanged(self):
        text = 'Hello world'
        assert truncate_for_telegram(text) == text

    def test_long_text_truncated(self):
        text = 'Line\n' * 2000
        result = truncate_for_telegram(text, max_len=100)
        assert len(result) <= 100
        assert result.endswith('_...truncated_')

    def test_truncates_at_line_boundary(self):
        lines = ['A' * 30 + '\n' for _ in range(10)]
        text = ''.join(lines)
        result = truncate_for_telegram(text, max_len=100)
        assert '...' in result


class TestEscapeMd:
    def test_escapes_special(self):
        assert escape_md('hello_world') == 'hello\\_world'
        assert escape_md('a*b') == 'a\\*b'

    def test_no_special(self):
        assert escape_md('hello') == 'hello'


class TestFormatRegionLabel:
    def test_us_stock(self):
        assert format_region_label('AAPL') == 'US'
        assert format_region_label('NVDA') == 'US'

    def test_eu_stock(self):
        assert format_region_label('SAP.DE') == 'EU'
        assert format_region_label('MC.PA') == 'EU'
        assert format_region_label('ASML.AS') == 'EU'

    def test_asia_stock(self):
        assert format_region_label('7203.T') == 'Asia'
        assert format_region_label('9988.HK') == 'Asia'
