"""Tests for watch list loading from config/watch_list.yaml."""
import os
import pytest
import tempfile
import yaml
from unittest.mock import patch


class TestWatchListLoading:
    """Tests for _load_watch_list() in src/config.py."""

    def test_load_watch_list_from_yaml(self):
        """Loads crypto and combined stock lists from watch_list.yaml."""
        from src.config import _load_watch_list

        # Create a temp watch_list.yaml
        wl_data = {
            'symbols': ['BTC', 'ETH', 'SOL'],
            'stocks': ['AAPL', 'MSFT'],
            'stocks_europe': ['SAP.DE', 'SHEL.L'],
            'stocks_asia': ['7203.T'],
        }
        with tempfile.NamedTemporaryFile(mode='w', suffix='.yaml', delete=False) as f:
            yaml.dump(wl_data, f)
            tmp_path = f.name

        try:
            settings = {'stock_trading': {'enabled': True}}
            with patch('src.config.os.path.dirname', return_value=os.path.dirname(tmp_path)):
                with patch('src.config.os.path.join', return_value=tmp_path):
                    result = _load_watch_list(settings)

            assert result['watch_list'] == ['BTC', 'ETH', 'SOL']
            assert result['stock_trading']['watch_list'] == ['AAPL', 'MSFT', 'SAP.DE', 'SHEL.L', '7203.T']
        finally:
            os.unlink(tmp_path)

    def test_missing_file_keeps_existing_settings(self):
        """When watch_list.yaml is missing, existing settings are preserved."""
        from src.config import _load_watch_list

        settings = {
            'watch_list': ['BTC'],
            'stock_trading': {'watch_list': ['AAPL']},
        }
        with patch('builtins.open', side_effect=FileNotFoundError):
            result = _load_watch_list(settings)

        assert result['watch_list'] == ['BTC']
        assert result['stock_trading']['watch_list'] == ['AAPL']

    def test_env_var_overrides_yaml(self):
        """WATCH_LIST env var should override watch_list.yaml values."""
        from src.config import _load_settings

        base_config = {
            'settings': {
                'watch_list': ['BTC'],
                'stock_trading': {'enabled': True},
            }
        }

        with patch.dict(os.environ, {'WATCH_LIST': 'DOGE;SHIB'}):
            result = _load_settings(base_config)

        assert result['watch_list'] == ['DOGE', 'SHIB']
