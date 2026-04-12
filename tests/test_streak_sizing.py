"""Tests for streak-based position sizing."""
import pytest
from unittest.mock import patch

from src.orchestration import bot_state


@pytest.fixture(autouse=True)
def _reset_bot_state():
    bot_state.clear_all()
    bot_state._strategy_streak_state.clear()
    yield
    bot_state.clear_all()
    bot_state._strategy_streak_state.clear()


ENABLED_CONFIG = {
    'enabled': True,
    'min_consecutive_wins': 3,
    'boost_multiplier': 1.3,
    'defense_multiplier': 0.7,
    'max_boost_multiplier': 1.5,
}


class TestStreakStateTracking:
    def test_increments_on_win(self):
        bot_state.strategy_record_trade_outcome('auto', is_win=True)
        bot_state.strategy_record_trade_outcome('auto', is_win=True)
        bot_state.strategy_record_trade_outcome('auto', is_win=True)
        state = bot_state.strategy_get_streak_state('auto')
        assert state['consecutive_wins'] == 3
        assert state['in_defensive_mode'] is False

    def test_resets_on_loss_below_threshold(self):
        bot_state.strategy_record_trade_outcome('auto', is_win=True)
        bot_state.strategy_record_trade_outcome('auto', is_win=True)
        bot_state.strategy_record_trade_outcome('auto', is_win=False)
        state = bot_state.strategy_get_streak_state('auto')
        assert state['consecutive_wins'] == 0
        assert state['in_defensive_mode'] is False

    def test_defensive_mode_activates_after_streak_breaks(self):
        for _ in range(4):
            bot_state.strategy_record_trade_outcome('auto', is_win=True)
        bot_state.strategy_record_trade_outcome('auto', is_win=False)
        state = bot_state.strategy_get_streak_state('auto')
        assert state['consecutive_wins'] == 0
        assert state['in_defensive_mode'] is True

    def test_defensive_mode_consumed_after_one_trade(self):
        for _ in range(3):
            bot_state.strategy_record_trade_outcome('auto', is_win=True)
        bot_state.strategy_record_trade_outcome('auto', is_win=False)
        assert bot_state.strategy_get_streak_state('auto')['in_defensive_mode'] is True

        bot_state.strategy_consume_defensive_mode('auto')
        assert bot_state.strategy_get_streak_state('auto')['in_defensive_mode'] is False

    def test_per_strategy_isolation(self):
        for _ in range(5):
            bot_state.strategy_record_trade_outcome('auto', is_win=True)
        assert bot_state.strategy_get_streak_state('auto')['consecutive_wins'] == 5
        assert bot_state.strategy_get_streak_state('momentum')['consecutive_wins'] == 0


class TestStreakMultiplier:
    def test_returns_boost_at_threshold(self):
        for _ in range(3):
            bot_state.strategy_record_trade_outcome('auto', is_win=True)
        mult = bot_state.strategy_get_streak_multiplier('auto', ENABLED_CONFIG)
        assert mult == 1.3

    def test_returns_defense_after_streak_break(self):
        for _ in range(3):
            bot_state.strategy_record_trade_outcome('auto', is_win=True)
        bot_state.strategy_record_trade_outcome('auto', is_win=False)
        mult = bot_state.strategy_get_streak_multiplier('auto', ENABLED_CONFIG)
        assert mult == 0.7

    def test_returns_normal_below_threshold(self):
        bot_state.strategy_record_trade_outcome('auto', is_win=True)
        mult = bot_state.strategy_get_streak_multiplier('auto', ENABLED_CONFIG)
        assert mult == 1.0

    def test_returns_normal_when_disabled(self):
        for _ in range(5):
            bot_state.strategy_record_trade_outcome('auto', is_win=True)
        mult = bot_state.strategy_get_streak_multiplier('auto', {'enabled': False})
        assert mult == 1.0

    def test_capped_at_max(self):
        for _ in range(20):
            bot_state.strategy_record_trade_outcome('auto', is_win=True)
        config = {**ENABLED_CONFIG, 'boost_multiplier': 2.0, 'max_boost_multiplier': 1.5}
        mult = bot_state.strategy_get_streak_multiplier('auto', config)
        assert mult == 1.5

    def test_empty_config_returns_normal(self):
        for _ in range(5):
            bot_state.strategy_record_trade_outcome('auto', is_win=True)
        mult = bot_state.strategy_get_streak_multiplier('auto', {})
        assert mult == 1.0


class TestStreakPersistence:
    @patch('src.database.save_bot_state')
    def test_persists_on_outcome(self, mock_save):
        bot_state.strategy_record_trade_outcome('auto', is_win=True)
        mock_save.assert_called_once()
        key, value = mock_save.call_args[0]
        assert key == 'streak_state:auto'
        assert '"consecutive_wins": 1' in value

    def test_load_restores_state(self):
        bot_state.strategy_load_streak_state('auto', {
            'consecutive_wins': 5, 'in_defensive_mode': False
        })
        state = bot_state.strategy_get_streak_state('auto')
        assert state['consecutive_wins'] == 5

    def test_clear_all_resets(self):
        for _ in range(5):
            bot_state.strategy_record_trade_outcome('auto', is_win=True)
        bot_state.clear_all()
        state = bot_state.strategy_get_streak_state('auto')
        assert state['consecutive_wins'] == 0
