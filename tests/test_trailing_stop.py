# tests/test_trailing_stop.py
"""Tests for trailing stop persistence integration via bot_state."""

import pytest
from unittest.mock import patch, MagicMock, AsyncMock

from src.orchestration import bot_state


class TestUpdateTrailingStop:
    """Tests for update_trailing_stop() DB persistence."""

    def setup_method(self):
        """Clear in-memory state before each test."""
        bot_state._trailing_stop_peaks.clear()

    @patch('src.orchestration.bot_state._db_save_peak')
    def test_persists_on_increase(self, mock_save):
        """When price rises above previous peak, save to DB."""
        from src.orchestration.bot_state import update_trailing_stop, _trailing_stop_peaks

        _trailing_stop_peaks['order_1'] = 50000.0
        result = update_trailing_stop('order_1', 51000.0)

        assert result == 51000.0
        assert _trailing_stop_peaks['order_1'] == 51000.0
        mock_save.assert_called_once_with('order_1', 51000.0)

    @patch('src.orchestration.bot_state._db_save_peak')
    def test_no_persist_on_decrease(self, mock_save):
        """When price drops, no DB write."""
        from src.orchestration.bot_state import update_trailing_stop, _trailing_stop_peaks

        _trailing_stop_peaks['order_1'] = 51000.0
        result = update_trailing_stop('order_1', 49000.0)

        assert result == 51000.0
        assert _trailing_stop_peaks['order_1'] == 51000.0
        mock_save.assert_not_called()

    @patch('src.orchestration.bot_state._db_save_peak')
    def test_no_persist_on_equal(self, mock_save):
        """When price equals peak, no DB write."""
        from src.orchestration.bot_state import update_trailing_stop, _trailing_stop_peaks

        _trailing_stop_peaks['order_1'] = 50000.0
        result = update_trailing_stop('order_1', 50000.0)

        assert result == 50000.0
        mock_save.assert_not_called()

    @patch('src.orchestration.bot_state._db_save_peak')
    def test_initializes_new_position(self, mock_save):
        """First call for unknown order_id sets peak without DB write."""
        from src.orchestration.bot_state import update_trailing_stop, _trailing_stop_peaks

        result = update_trailing_stop('new_order', 45000.0)

        assert result == 45000.0
        assert _trailing_stop_peaks['new_order'] == 45000.0
        # First call: prev_peak defaults to current_price, new_peak == prev_peak, no increase
        mock_save.assert_not_called()

    @patch('src.orchestration.bot_state._db_save_peak')
    def test_db_error_does_not_crash(self, mock_save):
        """If DB write fails, function still returns correct peak."""
        from src.orchestration.bot_state import update_trailing_stop, _trailing_stop_peaks

        mock_save.side_effect = Exception("DB connection lost")
        _trailing_stop_peaks['order_1'] = 50000.0

        result = update_trailing_stop('order_1', 52000.0)

        assert result == 52000.0
        assert _trailing_stop_peaks['order_1'] == 52000.0
        mock_save.assert_called_once()


class TestClearTrailingStop:
    """Tests for clear_trailing_stop()."""

    def setup_method(self):
        bot_state._trailing_stop_peaks.clear()

    def test_removes_from_dict(self):
        """Clearing removes the order_id from the dict."""
        from src.orchestration.bot_state import clear_trailing_stop, _trailing_stop_peaks

        _trailing_stop_peaks['order_1'] = 50000.0
        clear_trailing_stop('order_1')

        assert 'order_1' not in _trailing_stop_peaks

    def test_clear_nonexistent_no_error(self):
        """Clearing a non-existent order_id does not raise."""
        from src.orchestration.bot_state import clear_trailing_stop

        clear_trailing_stop('nonexistent')  # Should not raise


class TestStartupLoadsPeaks:
    """Tests for trailing stop peak loading during startup."""

    @patch('main.start_bot', new_callable=AsyncMock)
    @patch('main.load_session_peaks', new_callable=MagicMock)
    @patch('main.resolve_stale_circuit_breaker_events', new_callable=MagicMock)
    @patch('main.load_signal_cooldowns', new_callable=AsyncMock, return_value=({}, {}))
    @patch('main.load_stoploss_cooldowns', new_callable=AsyncMock, return_value={})
    @patch('main.load_trailing_stop_peaks', new_callable=AsyncMock)
    def test_startup_loads_peaks(self, mock_load, mock_cooldowns, mock_sig_cd,
                                  mock_resolve_cb, mock_session_peaks, mock_start_bot):
        """startup_event() populates _trailing_stop_peaks from DB."""
        import asyncio
        import main

        bot_state._trailing_stop_peaks.clear()
        mock_load.return_value = {'order_a': 60000.0, 'order_b': 3500.0}
        mock_start_bot.return_value = MagicMock()

        # Patch background task creation to prevent actual loops
        with patch('asyncio.create_task') as mock_task:
            with patch.object(main, 'os') as mock_os:
                mock_os.environ.get.return_value = None
                asyncio.run(main.startup_event())

        assert bot_state._trailing_stop_peaks == {'order_a': 60000.0, 'order_b': 3500.0}
        mock_load.assert_called_once()

    @patch('main.start_bot', new_callable=AsyncMock)
    @patch('main.load_session_peaks', new_callable=MagicMock)
    @patch('main.resolve_stale_circuit_breaker_events', new_callable=MagicMock)
    @patch('main.load_signal_cooldowns', new_callable=AsyncMock, return_value=({}, {}))
    @patch('main.load_stoploss_cooldowns', new_callable=AsyncMock, return_value={})
    @patch('main.load_trailing_stop_peaks', new_callable=AsyncMock)
    def test_startup_handles_load_failure(self, mock_load, mock_cooldowns, mock_sig_cd,
                                           mock_resolve_cb, mock_session_peaks, mock_start_bot):
        """If load_trailing_stop_peaks raises, startup continues."""
        import asyncio
        import main

        bot_state._trailing_stop_peaks.clear()
        mock_load.side_effect = Exception("DB unavailable")
        mock_start_bot.return_value = MagicMock()

        with patch('asyncio.create_task'):
            with patch.object(main, 'os') as mock_os:
                mock_os.environ.get.return_value = None
                # Should not raise
                asyncio.run(main.startup_event())

        assert bot_state._trailing_stop_peaks == {}
