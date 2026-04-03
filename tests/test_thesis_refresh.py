"""Tests for midweek thesis refresh triggered by sector conviction spikes."""
import json
from datetime import datetime, timezone, timedelta
from unittest.mock import patch, MagicMock

import pytest

from src.analysis.thesis_generator import (
    _find_unrepresented_spike_sectors,
    _merge_sector_into_thesis,
    check_conviction_spike_refresh,
    load_thesis_into_cache,
    get_thesis_symbols,
)


@pytest.fixture(autouse=True)
def _reset_thesis_cache():
    """Reset module-level thesis cache before each test."""
    import src.analysis.thesis_generator as tg
    tg._thesis_cache = None
    tg._thesis_symbols = set()
    yield
    tg._thesis_cache = None
    tg._thesis_symbols = set()


SAMPLE_THESIS = {
    "sectors": [
        {
            "name": "AI Infrastructure",
            "thesis": "AI demand growing",
            "conviction": 0.85,
            "stocks": [
                {"symbol": "NVDA", "exchange": "NASDAQ", "reasoning": "GPU leader"},
                {"symbol": "AMD", "exchange": "NASDAQ", "reasoning": "AI chips"},
            ]
        },
        {
            "name": "Cybersecurity",
            "thesis": "Cyber threats rising",
            "conviction": 0.75,
            "stocks": [
                {"symbol": "CRWD", "exchange": "NASDAQ", "reasoning": "Endpoint leader"},
            ]
        }
    ],
    "macro_view": "Cautious but sectors are diverging."
}


class TestFindUnrepresentedSpikeSectors:
    """Tests for _find_unrepresented_spike_sectors."""

    def setup_method(self):
        load_thesis_into_cache(json.dumps(SAMPLE_THESIS))

    @patch('src.analysis.sector_limits.get_group_symbols',
           return_value=['XOM', 'CVX', 'BP.L'])
    @patch('src.analysis.sector_review.get_all_sector_convictions',
           return_value={
               'energy': {
                   'score': 0.8, 'momentum': 'accelerating',
                   'review_confidence': 0.7, 'key_catalyst': 'oil rally',
               }
           })
    def test_finds_spike_with_no_thesis_overlap(self, mock_conv, mock_syms):
        cfg = {'min_conviction_score': 0.7, 'require_accelerating': True,
               'min_confidence': 0.6}
        result = _find_unrepresented_spike_sectors(cfg)

        assert len(result) == 1
        assert result[0][0] == 'energy'

    @patch('src.analysis.sector_limits.get_group_symbols',
           return_value=['NVDA', 'AMD', 'INTC'])
    @patch('src.analysis.sector_review.get_all_sector_convictions',
           return_value={
               'semiconductors': {
                   'score': 0.9, 'momentum': 'accelerating',
                   'review_confidence': 0.8,
               }
           })
    def test_skips_sector_already_in_thesis(self, mock_conv, mock_syms):
        cfg = {'min_conviction_score': 0.7, 'require_accelerating': True,
               'min_confidence': 0.6}
        result = _find_unrepresented_spike_sectors(cfg)
        assert len(result) == 0

    @patch('src.analysis.sector_limits.get_group_symbols',
           return_value=['XOM'])
    @patch('src.analysis.sector_review.get_all_sector_convictions',
           return_value={
               'energy': {
                   'score': 0.5, 'momentum': 'accelerating',
                   'review_confidence': 0.7,
               }
           })
    def test_skips_below_threshold(self, mock_conv, mock_syms):
        cfg = {'min_conviction_score': 0.7, 'require_accelerating': True,
               'min_confidence': 0.6}
        result = _find_unrepresented_spike_sectors(cfg)
        assert len(result) == 0

    @patch('src.analysis.sector_limits.get_group_symbols',
           return_value=['XOM'])
    @patch('src.analysis.sector_review.get_all_sector_convictions',
           return_value={
               'energy': {
                   'score': 0.8, 'momentum': 'stable',
                   'review_confidence': 0.7,
               }
           })
    def test_skips_non_accelerating(self, mock_conv, mock_syms):
        cfg = {'min_conviction_score': 0.7, 'require_accelerating': True,
               'min_confidence': 0.6}
        result = _find_unrepresented_spike_sectors(cfg)
        assert len(result) == 0

    @patch('src.analysis.sector_limits.get_group_symbols',
           return_value=['AAVE', 'UNI'])
    @patch('src.analysis.sector_review.get_all_sector_convictions',
           return_value={
               'defi': {
                   'score': 0.9, 'momentum': 'accelerating',
                   'review_confidence': 0.8,
               }
           })
    def test_skips_crypto_groups(self, mock_conv, mock_syms):
        cfg = {'min_conviction_score': 0.7, 'require_accelerating': True,
               'min_confidence': 0.6}
        result = _find_unrepresented_spike_sectors(cfg)
        assert len(result) == 0  # defi is in _CRYPTO_GROUPS

    @patch('src.analysis.sector_limits.get_group_symbols')
    @patch('src.analysis.sector_review.get_all_sector_convictions',
           return_value={
               'energy': {
                   'score': 0.75, 'momentum': 'accelerating',
                   'review_confidence': 0.7,
               },
               'materials': {
                   'score': 0.9, 'momentum': 'accelerating',
                   'review_confidence': 0.8,
               },
           })
    def test_sorts_by_conviction_descending(self, mock_conv, mock_syms):
        mock_syms.side_effect = lambda g: {
            'energy': ['XOM'], 'materials': ['BHP']
        }.get(g, [])

        cfg = {'min_conviction_score': 0.7, 'require_accelerating': True,
               'min_confidence': 0.6}
        result = _find_unrepresented_spike_sectors(cfg)

        assert len(result) == 2
        assert result[0][0] == 'materials'  # 0.9 > 0.75
        assert result[1][0] == 'energy'


class TestMergeSectorIntoThesis:
    """Tests for _merge_sector_into_thesis."""

    def setup_method(self):
        load_thesis_into_cache(json.dumps(SAMPLE_THESIS))

    @patch('src.database.save_longterm_thesis')
    def test_adds_sector_to_thesis(self, mock_save):
        import src.analysis.thesis_generator as tg

        new_sector = {
            "name": "Energy",
            "thesis": "Oil supply shock",
            "conviction": 0.8,
            "stocks": [
                {"symbol": "XOM", "exchange": "NYSE", "reasoning": "Oil major"},
                {"symbol": "CVX", "exchange": "NYSE", "reasoning": "Integrated"},
            ]
        }

        _merge_sector_into_thesis(new_sector)

        assert len(tg._thesis_cache['sectors']) == 3
        assert 'XOM' in tg._thesis_symbols
        assert 'CVX' in tg._thesis_symbols
        assert 'NVDA' in tg._thesis_symbols  # original still there
        mock_save.assert_called_once()

    @patch('src.database.save_longterm_thesis')
    def test_does_not_mutate_original(self, mock_save):
        import src.analysis.thesis_generator as tg

        original_count = len(SAMPLE_THESIS['sectors'])
        _merge_sector_into_thesis({
            "name": "Test", "thesis": "test", "conviction": 0.5,
            "stocks": [{"symbol": "TST", "exchange": "NYSE", "reasoning": "test"}]
        })

        # Original dict should NOT be mutated (deep copy)
        assert len(SAMPLE_THESIS['sectors']) == original_count


class TestCheckConvictionSpikeRefresh:
    """Tests for the top-level check_conviction_spike_refresh."""

    @patch('src.config.app_config', {
        'settings': {'strategies': {'longterm': {'thesis_review': {
            'midweek_refresh': {'enabled': False}}}}}})
    def test_returns_none_when_disabled(self):
        result = check_conviction_spike_refresh()
        assert result is None

    @patch('src.config.app_config', {
        'settings': {'strategies': {'longterm': {'thesis_review': {
            'midweek_refresh': {'enabled': True}}}}}})
    def test_returns_none_without_thesis(self):
        result = check_conviction_spike_refresh()
        assert result is None

    @patch('src.database.load_bot_state')
    @patch('src.config.app_config', {
        'settings': {'strategies': {'longterm': {'thesis_review': {
            'midweek_refresh': {
                'enabled': True, 'cooldown_hours': 72,
            }}}}}})
    def test_respects_cooldown(self, mock_load_state):
        load_thesis_into_cache(json.dumps(SAMPLE_THESIS))
        # Last refresh was 1 hour ago
        mock_load_state.return_value = (
            datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()

        result = check_conviction_spike_refresh()
        assert result is None

    @patch('src.database.save_bot_state')
    @patch('src.database.load_bot_state', return_value=None)
    @patch('src.database.save_longterm_thesis')
    @patch('src.analysis.sector_limits.get_group_symbols', return_value=['XOM'])
    @patch('src.analysis.sector_review.get_all_sector_convictions',
           return_value={
               'energy': {
                   'score': 0.8, 'momentum': 'accelerating',
                   'review_confidence': 0.7,
               }
           })
    @patch('src.config.app_config', {
        'settings': {'strategies': {'longterm': {'thesis_review': {
            'midweek_refresh': {
                'enabled': True, 'cooldown_hours': 72,
                'min_conviction_score': 0.7, 'require_accelerating': True,
                'min_confidence': 0.6, 'max_sectors_per_refresh': 1,
            }}}}}})
    def test_no_spike_when_gemini_unavailable(self, mock_conv, mock_syms,
                                               mock_save_thesis,
                                               mock_load, mock_save):
        """When Gemini fails (no GCP_PROJECT_ID), returns None without saving cooldown."""
        load_thesis_into_cache(json.dumps(SAMPLE_THESIS))

        result = check_conviction_spike_refresh()
        # generate_sector_addendum returns None without GCP_PROJECT_ID
        assert result is None
        mock_save.assert_not_called()

    @patch('src.database.save_bot_state')
    @patch('src.database.load_bot_state', return_value=None)
    @patch('src.config.app_config', {
        'settings': {'strategies': {'longterm': {'thesis_review': {
            'midweek_refresh': {
                'enabled': True, 'cooldown_hours': 72,
                'min_conviction_score': 0.7, 'require_accelerating': True,
                'min_confidence': 0.6, 'max_sectors_per_refresh': 1,
            }}}}}})
    def test_returns_none_when_no_spikes(self, mock_load, mock_save):
        load_thesis_into_cache(json.dumps(SAMPLE_THESIS))

        with patch('src.analysis.sector_review.get_all_sector_convictions',
                    return_value={}):
            result = check_conviction_spike_refresh()

        assert result is None
        mock_save.assert_not_called()


class TestGetGroupSymbols:
    """Tests for the new get_group_symbols helper."""

    @patch('src.analysis.sector_limits._sector_config', {
        'groups': {
            'energy': {'symbols': ['XOM', 'CVX', 'BP.L'], 'max_positions': 3},
        }
    })
    def test_returns_symbols(self):
        from src.analysis.sector_limits import get_group_symbols
        result = get_group_symbols('energy')
        assert result == ['XOM', 'CVX', 'BP.L']

    @patch('src.analysis.sector_limits._sector_config', {
        'groups': {'energy': {'symbols': ['xom'], 'max_positions': 1}}
    })
    def test_uppercases_symbols(self):
        from src.analysis.sector_limits import get_group_symbols
        result = get_group_symbols('energy')
        assert result == ['XOM']

    @patch('src.analysis.sector_limits._sector_config', {'groups': {}})
    def test_returns_empty_for_unknown_group(self):
        from src.analysis.sector_limits import get_group_symbols
        result = get_group_symbols('nonexistent')
        assert result == []
