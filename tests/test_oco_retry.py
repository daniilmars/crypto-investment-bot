# tests/test_oco_retry.py
"""Tests for OCO bracket retry with exponential backoff and fallback stop-loss."""

from unittest.mock import patch, MagicMock


class TestIsRetryableBinanceError:
    """Tests for _is_retryable_binance_error helper."""

    def test_retryable_api_codes(self):
        from src.execution.binance_trader import _is_retryable_binance_error
        for code in (-1015, -1001, -1003):
            exc = type('BinanceAPIException', (Exception,), {'code': code})()
            assert _is_retryable_binance_error(exc) is True

    def test_non_retryable_api_code(self):
        from src.execution.binance_trader import _is_retryable_binance_error
        exc = type('BinanceAPIException', (Exception,), {'code': -1100})()
        assert _is_retryable_binance_error(exc) is False

    def test_retryable_message_patterns(self):
        from src.execution.binance_trader import _is_retryable_binance_error
        for msg in ('500 Internal Server Error', 'Connection timeout',
                    '503 Service Unavailable', 'rate limit exceeded'):
            assert _is_retryable_binance_error(Exception(msg)) is True

    def test_non_retryable_message(self):
        from src.execution.binance_trader import _is_retryable_binance_error
        assert _is_retryable_binance_error(Exception("Invalid quantity")) is False


class TestOCOWithRetry:
    """Tests for _place_oco_with_retry wrapper."""

    @patch('src.execution.binance_trader._place_oco_bracket')
    def test_oco_succeeds_first_attempt(self, mock_oco):
        """OCO succeeds on first try — no retries needed."""
        from src.execution.binance_trader import _place_oco_with_retry
        expected = {"order_list_id": 123, "take_profit": 110.0, "stop_loss": 95.0}
        mock_oco.return_value = expected

        result = _place_oco_with_retry("BTCUSDT", 100.0, 0.01)
        assert result == expected
        assert mock_oco.call_count == 1

    @patch('src.execution.binance_trader.time.sleep')
    @patch('src.execution.binance_trader._place_oco_bracket')
    def test_oco_retries_on_transient_error(self, mock_oco, mock_sleep):
        """OCO fails with retryable error, then succeeds on retry."""
        from src.execution.binance_trader import _place_oco_with_retry

        transient_exc = type('BinanceAPIException', (Exception,),
                             {'code': -1001})("Disconnected")
        expected = {"order_list_id": 456, "take_profit": 110.0, "stop_loss": 95.0}
        mock_oco.side_effect = [transient_exc, expected]

        result = _place_oco_with_retry("BTCUSDT", 100.0, 0.01)
        assert result == expected
        assert mock_oco.call_count == 2
        mock_sleep.assert_called_once()

    @patch('src.execution.binance_trader._place_fallback_stop_loss')
    @patch('src.execution.binance_trader.time.sleep')
    @patch('src.execution.binance_trader._place_oco_bracket')
    def test_oco_fallback_stop_loss_on_exhaustion(self, mock_oco, mock_sleep,
                                                   mock_fallback):
        """All OCO retries fail → falls back to plain stop-loss."""
        from src.execution.binance_trader import _place_oco_with_retry

        transient_exc = type('BinanceAPIException', (Exception,),
                             {'code': -1001})("Disconnected")
        mock_oco.side_effect = transient_exc
        fallback_result = {"order_id": 789, "stop_loss": 95.0, "fallback": True}
        mock_fallback.return_value = fallback_result

        result = _place_oco_with_retry("BTCUSDT", 100.0, 0.01)
        assert result == fallback_result
        assert result.get("fallback") is True
        assert mock_oco.call_count == 3
        mock_fallback.assert_called_once_with("BTCUSDT", 100.0, 0.01)

    @patch('src.execution.binance_trader._place_fallback_stop_loss')
    @patch('src.execution.binance_trader._place_oco_bracket')
    def test_non_retryable_error_skips_retries(self, mock_oco, mock_fallback):
        """Non-retryable error goes straight to fallback without retrying."""
        from src.execution.binance_trader import _place_oco_with_retry

        mock_oco.side_effect = Exception("Invalid quantity")
        mock_fallback.return_value = None

        result = _place_oco_with_retry("BTCUSDT", 100.0, 0.01)
        assert result is None
        assert mock_oco.call_count == 1
        mock_fallback.assert_called_once()


class TestFallbackStopLoss:
    """Tests for _place_fallback_stop_loss."""

    @patch('src.execution.binance_trader._get_symbol_info', return_value=None)
    @patch('src.execution.binance_trader._get_binance_client')
    @patch('src.execution.binance_trader.app_config', new_callable=dict)
    def test_fallback_stop_loss_success(self, mock_config, mock_client_fn,
                                        mock_sym_info):
        """Fallback stop-loss order placed successfully."""
        from src.execution.binance_trader import _place_fallback_stop_loss
        mock_config['settings'] = {'live_trading': {'stop_loss_percentage': 0.03}}
        mock_client = MagicMock()
        mock_client.create_order.return_value = {'orderId': 999}
        mock_client_fn.return_value = mock_client

        result = _place_fallback_stop_loss("BTCUSDT", 100.0, 0.01)
        assert result is not None
        assert result['fallback'] is True
        assert result['order_id'] == 999
        mock_client.create_order.assert_called_once()

    @patch('src.execution.binance_trader._get_symbol_info', return_value=None)
    @patch('src.execution.binance_trader._get_binance_client')
    @patch('src.execution.binance_trader.app_config', new_callable=dict)
    def test_fallback_stop_loss_also_fails(self, mock_config, mock_client_fn,
                                            mock_sym_info):
        """Fallback stop-loss also fails → returns None."""
        from src.execution.binance_trader import _place_fallback_stop_loss
        mock_config['settings'] = {'live_trading': {'stop_loss_percentage': 0.03}}
        mock_client = MagicMock()
        mock_client.create_order.side_effect = Exception("Exchange down")
        mock_client_fn.return_value = mock_client

        result = _place_fallback_stop_loss("BTCUSDT", 100.0, 0.01)
        assert result is None

    @patch('src.execution.binance_trader._get_binance_client', return_value=None)
    def test_fallback_no_client(self, mock_client_fn):
        """Fallback returns None when Binance client is unavailable."""
        from src.execution.binance_trader import _place_fallback_stop_loss
        result = _place_fallback_stop_loss("BTCUSDT", 100.0, 0.01)
        assert result is None


class TestStockBracketFailureAlert:
    """Tests for stock bracket failure Telegram alert."""

    @patch('src.execution.stock_trader._record_stock_trade')
    @patch('src.execution.stock_trader._place_bracket_order', return_value=None)
    @patch('src.execution.stock_trader._is_market_open', return_value=True)
    @patch('src.execution.stock_trader._get_alpaca_client')
    @patch('src.execution.stock_trader.log')
    def test_stock_bracket_failure_logs_warning(self, mock_log, mock_client_fn,
                                                 mock_market, mock_bracket,
                                                 mock_record):
        """When bracket order fails after stock BUY, a warning is logged."""
        import sys
        # Create mock alpaca modules so imports inside place_stock_order succeed
        mock_alpaca = MagicMock()
        mock_alpaca.trading.requests.MarketOrderRequest = MagicMock(
            return_value=MagicMock())
        mock_alpaca.trading.enums.OrderSide.BUY = 'BUY'
        mock_alpaca.trading.enums.OrderSide.SELL = 'SELL'
        mock_alpaca.trading.enums.TimeInForce.DAY = 'DAY'
        sys.modules['alpaca'] = mock_alpaca
        sys.modules['alpaca.trading'] = mock_alpaca.trading
        sys.modules['alpaca.trading.requests'] = mock_alpaca.trading.requests
        sys.modules['alpaca.trading.enums'] = mock_alpaca.trading.enums

        try:
            from src.execution.stock_trader import place_stock_order

            mock_client = MagicMock()
            mock_order = MagicMock()
            mock_order.id = 'test-id'
            mock_order.filled_avg_price = 150.0
            mock_order.filled_qty = 10.0
            mock_client.submit_order.return_value = mock_order
            mock_client_fn.return_value = mock_client

            result = place_stock_order("AAPL", "BUY", 10, 150.0)
            assert result['status'] == 'FILLED'
            assert 'bracket' not in result
            mock_bracket.assert_called_once()
            # Verify warning was logged about bracket failure
            mock_log.warning.assert_any_call(
                "Bracket order failed for AAPL after BUY — "
                "position has NO server-side SL/TP protection!"
            )
        finally:
            for mod in ['alpaca', 'alpaca.trading',
                        'alpaca.trading.requests', 'alpaca.trading.enums']:
                sys.modules.pop(mod, None)
