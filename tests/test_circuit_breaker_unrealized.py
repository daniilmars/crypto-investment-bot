"""Tests for circuit breaker unrealized P&L calculation (W1)."""

from unittest.mock import patch, MagicMock

import pytest

from src.execution.circuit_breaker import get_unrealized_pnl


class TestUnrealizedPnl:

    @patch('src.execution.binance_trader.get_open_positions')
    def test_long_profit(self, mock_positions):
        """Long position with price above entry has positive unrealized PnL."""
        mock_positions.return_value = [
            {'symbol': 'BTC', 'entry_price': 50000, 'quantity': 0.1, 'status': 'OPEN'},
        ]
        pnl = get_unrealized_pnl({'BTC': 55000})
        assert pnl == pytest.approx(500.0)  # (55000 - 50000) * 0.1

    @patch('src.execution.binance_trader.get_open_positions')
    def test_long_loss(self, mock_positions):
        """Long position with price below entry has negative unrealized PnL."""
        mock_positions.return_value = [
            {'symbol': 'ETH', 'entry_price': 3000, 'quantity': 1.0, 'status': 'OPEN'},
        ]
        pnl = get_unrealized_pnl({'ETH': 2800})
        assert pnl == pytest.approx(-200.0)

    @patch('src.execution.binance_trader.get_open_positions')
    def test_missing_price_skipped(self, mock_positions):
        """Position with no current price is skipped (not included in total)."""
        mock_positions.return_value = [
            {'symbol': 'BTC', 'entry_price': 50000, 'quantity': 0.1, 'status': 'OPEN'},
            {'symbol': 'SOL', 'entry_price': 100, 'quantity': 10, 'status': 'OPEN'},
        ]
        # Only provide price for BTC, SOL is missing
        pnl = get_unrealized_pnl({'BTC': 51000})
        assert pnl == pytest.approx(100.0)  # Only BTC counted

    @patch('src.execution.binance_trader.get_open_positions')
    def test_no_positions(self, mock_positions):
        """No open positions returns 0.0."""
        mock_positions.return_value = []
        pnl = get_unrealized_pnl({'BTC': 50000})
        assert pnl == 0.0

    @patch('src.execution.binance_trader.get_open_positions')
    def test_exception_returns_zero(self, mock_positions):
        """Any exception returns 0.0 (fail-safe)."""
        mock_positions.side_effect = Exception("DB error")
        pnl = get_unrealized_pnl({'BTC': 50000})
        assert pnl == 0.0
