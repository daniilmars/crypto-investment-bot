"""Tests for correlation-aware sector limits."""

import os
from unittest.mock import patch, MagicMock

import pytest

from src.analysis.sector_limits import (
    check_sector_limit,
    get_symbol_group,
    get_group_limit,
    get_sector_exposure_summary,
    reload_sector_groups,
    _sector_config,
    _symbol_to_group,
)


SAMPLE_SECTOR_CONFIG = {
    'default_max_positions_per_group': 2,
    'groups': {
        'l1_major': {
            'max_positions': 3,
            'symbols': ['BTC', 'ETH', 'SOL', 'BNB'],
        },
        'defi': {
            'max_positions': 2,
            'symbols': ['UNI', 'AAVE', 'MKR'],
        },
        'tech_mega': {
            'max_positions': 3,
            'symbols': ['AAPL', 'MSFT', 'GOOGL', 'AMZN', 'META', 'NVDA'],
        },
        'meme': {
            'max_positions': 1,
            'symbols': ['DOGE', 'SHIB', 'PEPE'],
        },
    },
}


@pytest.fixture(autouse=True)
def mock_sector_config():
    """Inject test config instead of loading from file."""
    import src.analysis.sector_limits as sl
    sl._sector_config = SAMPLE_SECTOR_CONFIG.copy()
    sl._symbol_to_group = {}
    # Build reverse lookup
    for group_name, group_data in SAMPLE_SECTOR_CONFIG['groups'].items():
        for sym in group_data['symbols']:
            if sym not in sl._symbol_to_group:
                sl._symbol_to_group[sym] = group_name
    yield
    sl._sector_config = None
    sl._symbol_to_group = {}


class TestGetSymbolGroup:
    def test_known_symbol(self):
        assert get_symbol_group('BTC') == 'l1_major'
        assert get_symbol_group('ETH') == 'l1_major'
        assert get_symbol_group('UNI') == 'defi'
        assert get_symbol_group('AAPL') == 'tech_mega'

    def test_case_insensitive(self):
        assert get_symbol_group('btc') == 'l1_major'
        assert get_symbol_group('Eth') == 'l1_major'

    def test_unknown_symbol(self):
        assert get_symbol_group('RANDOM_TICKER') is None


class TestGetGroupLimit:
    def test_known_group(self):
        assert get_group_limit('l1_major') == 3
        assert get_group_limit('defi') == 2
        assert get_group_limit('meme') == 1

    def test_unknown_group_returns_default(self):
        assert get_group_limit('nonexistent_group') == 2  # default


class TestCheckSectorLimit:
    def test_allows_when_group_has_room(self):
        positions = [
            {'symbol': 'BTC', 'status': 'OPEN'},
        ]
        allowed, reason = check_sector_limit('ETH', positions)
        assert allowed is True

    def test_blocks_when_group_is_full(self):
        positions = [
            {'symbol': 'BTC', 'status': 'OPEN'},
            {'symbol': 'ETH', 'status': 'OPEN'},
            {'symbol': 'SOL', 'status': 'OPEN'},
        ]
        allowed, reason = check_sector_limit('BNB', positions)
        assert allowed is False
        assert 'l1_major' in reason

    def test_closed_positions_not_counted(self):
        positions = [
            {'symbol': 'BTC', 'status': 'OPEN'},
            {'symbol': 'ETH', 'status': 'CLOSED'},
            {'symbol': 'SOL', 'status': 'CLOSED'},
        ]
        allowed, _ = check_sector_limit('BNB', positions)
        assert allowed is True

    def test_meme_limit_one(self):
        positions = [
            {'symbol': 'DOGE', 'status': 'OPEN'},
        ]
        allowed, reason = check_sector_limit('SHIB', positions)
        assert allowed is False
        assert 'meme' in reason

    def test_different_group_positions_dont_count(self):
        positions = [
            {'symbol': 'BTC', 'status': 'OPEN'},
            {'symbol': 'ETH', 'status': 'OPEN'},
            {'symbol': 'SOL', 'status': 'OPEN'},
        ]
        # defi group is separate from l1_major
        allowed, _ = check_sector_limit('UNI', positions)
        assert allowed is True

    def test_ungrouped_symbol_uses_default_limit(self):
        positions = [
            {'symbol': 'RANDOM1', 'status': 'OPEN'},
            {'symbol': 'RANDOM2', 'status': 'OPEN'},
        ]
        allowed, reason = check_sector_limit('RANDOM3', positions)
        assert allowed is False
        assert 'Ungrouped' in reason

    def test_ungrouped_symbol_allowed_when_below_default(self):
        positions = [
            {'symbol': 'RANDOM1', 'status': 'OPEN'},
        ]
        allowed, _ = check_sector_limit('RANDOM2', positions)
        assert allowed is True

    @patch('src.analysis.sector_limits.app_config')
    def test_disabled_always_allows(self, mock_config):
        mock_config.get.return_value = {'sector_limits': {'enabled': False}}
        positions = [
            {'symbol': 'BTC', 'status': 'OPEN'},
            {'symbol': 'ETH', 'status': 'OPEN'},
            {'symbol': 'SOL', 'status': 'OPEN'},
        ]
        allowed, _ = check_sector_limit('BNB', positions)
        assert allowed is True

    def test_empty_positions_always_allows(self):
        allowed, _ = check_sector_limit('BTC', [])
        assert allowed is True


class TestGetSectorExposureSummary:
    def test_empty_positions(self):
        summary = get_sector_exposure_summary([])
        assert summary == {}

    def test_positions_in_one_group(self):
        positions = [
            {'symbol': 'BTC', 'status': 'OPEN'},
            {'symbol': 'ETH', 'status': 'OPEN'},
        ]
        summary = get_sector_exposure_summary(positions)
        assert 'l1_major' in summary
        assert summary['l1_major']['current'] == 2
        assert summary['l1_major']['limit'] == 3
        assert set(summary['l1_major']['symbols']) == {'BTC', 'ETH'}

    def test_positions_across_groups(self):
        positions = [
            {'symbol': 'BTC', 'status': 'OPEN'},
            {'symbol': 'UNI', 'status': 'OPEN'},
            {'symbol': 'AAPL', 'status': 'OPEN'},
        ]
        summary = get_sector_exposure_summary(positions)
        assert len(summary) == 3
        assert summary['l1_major']['current'] == 1
        assert summary['defi']['current'] == 1
        assert summary['tech_mega']['current'] == 1

    def test_ungrouped_positions_in_separate_bucket(self):
        positions = [
            {'symbol': 'RANDOM_IPO', 'status': 'OPEN'},
        ]
        summary = get_sector_exposure_summary(positions)
        assert '_ungrouped' in summary
        assert summary['_ungrouped']['current'] == 1

    def test_closed_positions_excluded(self):
        positions = [
            {'symbol': 'BTC', 'status': 'CLOSED'},
        ]
        summary = get_sector_exposure_summary(positions)
        assert summary == {}
