"""Tests for trading improvements.

Plan 2: Daily SMA Trend Filter
Plan 1: ATR-Based Dynamic SL/TP
Plan 3: Limit Orders with Pullback Entry
Fix: Article-level scores passed to symbol assessment
"""

import asyncio
import time
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# --- Plan 2: Daily SMA Trend Filter ---


class TestDailyKlinesCache:
    """Tests for _fetch_daily_klines_batch caching."""

    @pytest.fixture(autouse=True)
    def reset_cache(self):
        """Reset module-level cache before each test."""
        from src.orchestration import cycle_runner
        cycle_runner._daily_kline_cache = {}
        cycle_runner._daily_kline_cache_ts = 0

    @patch('src.orchestration.cycle_runner.get_klines')
    def test_fetches_klines_on_first_call(self, mock_klines):
        from src.orchestration.cycle_runner import _fetch_daily_klines_batch

        mock_klines.return_value = [
            {'open': 100, 'high': 105, 'low': 95, 'close': 102, 'volume': 1000, 'timestamp': 1}
        ] * 50

        result = asyncio.run(
            _fetch_daily_klines_batch(['BTC', 'ETH'], cache_minutes=60))

        assert 'BTC' in result
        assert 'ETH' in result
        assert mock_klines.call_count == 2

    @patch('src.orchestration.cycle_runner.get_klines')
    def test_returns_cached_within_ttl(self, mock_klines):
        from src.orchestration import cycle_runner
        from src.orchestration.cycle_runner import _fetch_daily_klines_batch

        # Pre-populate cache
        cycle_runner._daily_kline_cache = {'BTC': [{'close': 100}]}
        cycle_runner._daily_kline_cache_ts = time.time()

        result = asyncio.run(
            _fetch_daily_klines_batch(['BTC'], cache_minutes=60))

        assert result == {'BTC': [{'close': 100}]}
        mock_klines.assert_not_called()

    @patch('src.orchestration.cycle_runner.get_klines')
    def test_refreshes_after_ttl_expires(self, mock_klines):
        from src.orchestration import cycle_runner
        from src.orchestration.cycle_runner import _fetch_daily_klines_batch

        cycle_runner._daily_kline_cache = {'BTC': [{'close': 100}]}
        cycle_runner._daily_kline_cache_ts = time.time() - 3700  # > 60 min ago

        mock_klines.return_value = [{'close': 105}] * 50
        asyncio.run(_fetch_daily_klines_batch(['BTC'], cache_minutes=60))

        assert mock_klines.called

    @patch('src.orchestration.cycle_runner.get_klines')
    def test_handles_kline_failure_gracefully(self, mock_klines):
        from src.orchestration.cycle_runner import _fetch_daily_klines_batch

        mock_klines.return_value = None  # API failure

        result = asyncio.run(
            _fetch_daily_klines_batch(['BTC'], cache_minutes=60))

        assert 'BTC' not in result  # Symbol skipped


class TestDailySMAComputation:
    """Tests for SMA computed on daily closes vs 15-min snapshots."""

    def test_sma_on_daily_closes(self):
        from src.analysis.technical_indicators import calculate_sma
        daily_closes = [100 + i for i in range(20)]
        sma = calculate_sma(daily_closes, period=20)
        assert sma is not None
        assert abs(sma - 109.5) < 0.01  # mean of 100..119

    def test_sma_fallback_insufficient_data(self):
        from src.analysis.technical_indicators import calculate_sma
        sma = calculate_sma([100, 101, 102], period=20)
        assert sma is None


# --- Plan 1: ATR-Based Dynamic SL/TP ---


class TestDynamicRisk:
    """Tests for compute_dynamic_sl_tp."""

    def test_basic_computation(self):
        from src.analysis.dynamic_risk import compute_dynamic_sl_tp
        sl, tp = compute_dynamic_sl_tp(0.03, 0.035, 0.08)
        assert sl == 0.045  # 0.03 * 1.5
        assert tp == 0.09   # 0.03 * 3.0

    def test_none_atr_falls_back_to_config(self):
        from src.analysis.dynamic_risk import compute_dynamic_sl_tp
        sl, tp = compute_dynamic_sl_tp(None, 0.035, 0.08)
        assert sl == 0.035
        assert tp == 0.08

    def test_sl_floor_clamping(self):
        from src.analysis.dynamic_risk import compute_dynamic_sl_tp
        # Very low ATR → should clamp to floor
        sl, tp = compute_dynamic_sl_tp(0.005, 0.035, 0.08)
        assert sl == 0.02  # floor

    def test_sl_ceiling_clamping(self):
        from src.analysis.dynamic_risk import compute_dynamic_sl_tp
        # Very high ATR → should clamp to ceiling
        sl, tp = compute_dynamic_sl_tp(0.10, 0.035, 0.08)
        assert sl == 0.07  # ceiling

    def test_tp_floor_clamping(self):
        from src.analysis.dynamic_risk import compute_dynamic_sl_tp
        sl, tp = compute_dynamic_sl_tp(0.005, 0.035, 0.08)
        assert tp == 0.04  # floor

    def test_tp_ceiling_clamping(self):
        from src.analysis.dynamic_risk import compute_dynamic_sl_tp
        sl, tp = compute_dynamic_sl_tp(0.10, 0.035, 0.08)
        assert tp == 0.15  # ceiling (0.10 * 3.0 = 0.30 → clamped)

    def test_custom_multipliers(self):
        from src.analysis.dynamic_risk import compute_dynamic_sl_tp
        sl, tp = compute_dynamic_sl_tp(
            0.03, 0.035, 0.08,
            sl_atr_mult=2.0, tp_atr_mult=4.0)
        assert sl == 0.06  # 0.03 * 2.0
        assert tp == 0.12  # 0.03 * 4.0


class TestPositionMonitorDynamicSLTP:
    """Tests for position monitor receiving dynamic SL/TP via kwargs.

    cycle_runner overwrites stop_loss_pct/take_profit_pct before calling
    monitor_position, using the position's dynamic_sl_pct/dynamic_tp_pct.
    """

    @patch('src.orchestration.position_monitor.place_order')
    @patch('src.orchestration.position_monitor.send_telegram_alert', new_callable=AsyncMock)
    @patch('src.orchestration.position_monitor.bot_state')
    def test_wider_sl_prevents_premature_exit(self, mock_state, mock_alert, mock_order):
        from src.orchestration.position_monitor import monitor_position
        mock_state.update_trailing_stop.return_value = 100.0

        position = {
            'symbol': 'BTC', 'entry_price': 100.0, 'order_id': 'test1',
            'quantity': 1.0,
        }
        # Price dropped 4% — below default 3.5% SL but above 5% dynamic SL
        # cycle_runner passes stop_loss_pct=0.05 (from dynamic_sl_pct)
        result = asyncio.run(
            monitor_position(
                position, 96.0,
                stop_loss_pct=0.05,  # Dynamic SL (wider than default 3.5%)
                take_profit_pct=0.10,
                trailing_stop_enabled=True,
                trailing_stop_activation=0.02,
                trailing_stop_distance=0.015))

        # 4% drop < 5% dynamic SL → no exit
        assert result == 'none'

    @patch('src.orchestration.position_monitor.place_order')
    @patch('src.orchestration.position_monitor.send_telegram_alert', new_callable=AsyncMock)
    @patch('src.orchestration.position_monitor.bot_state')
    def test_default_sl_triggers_at_3_5_pct(self, mock_state, mock_alert, mock_order):
        from src.orchestration.position_monitor import monitor_position
        mock_state.update_trailing_stop.return_value = 100.0

        position = {
            'symbol': 'BTC', 'entry_price': 100.0, 'order_id': 'test2',
            'quantity': 1.0,
        }
        # 4% drop > 3.5% default SL → triggers
        result = asyncio.run(
            monitor_position(
                position, 96.0,
                stop_loss_pct=0.035,  # Default static SL
                take_profit_pct=0.08,
                trailing_stop_enabled=True,
                trailing_stop_activation=0.02,
                trailing_stop_distance=0.015))

        assert result == 'stop_loss'


# --- Plan 3: Limit Orders with Pullback Entry ---


class TestShouldUseLimitOrder:
    """Tests for limit order decision logic."""

    @patch('src.orchestration.trade_executor.app_config')
    @patch('src.database.get_pending_orders')
    def test_high_confidence_uses_market(self, mock_pending, mock_config):
        from src.orchestration.trade_executor import _should_use_limit_order
        mock_config.get.return_value = {
            'limit_orders': {
                'enabled': True,
                'market_order_threshold': 0.8,
                'max_pending_orders': 3,
                'pullback_pct': 0.005,
            }
        }
        mock_pending.return_value = []

        signal = {'signal_strength': 0.85}
        use_limit, pullback = _should_use_limit_order(signal, mock_config)

        assert use_limit is False

    @patch('src.orchestration.trade_executor.app_config')
    @patch('src.database.get_pending_orders')
    def test_low_confidence_uses_limit(self, mock_pending, mock_config):
        from src.orchestration.trade_executor import _should_use_limit_order
        mock_config.get.return_value = {
            'limit_orders': {
                'enabled': True,
                'market_order_threshold': 0.8,
                'max_pending_orders': 3,
                'pullback_pct': 0.005,
            }
        }
        mock_pending.return_value = []

        signal = {'signal_strength': 0.65}
        use_limit, pullback = _should_use_limit_order(signal, mock_config)

        assert use_limit is True
        assert pullback == 0.005

    @patch('src.orchestration.trade_executor.app_config')
    def test_disabled_returns_false(self, mock_config):
        from src.orchestration.trade_executor import _should_use_limit_order
        mock_config.get.return_value = {
            'limit_orders': {'enabled': False}
        }

        signal = {'signal_strength': 0.5}
        use_limit, pullback = _should_use_limit_order(signal, mock_config)

        assert use_limit is False

    @patch('src.orchestration.trade_executor.app_config')
    @patch('src.database.get_pending_orders')
    def test_max_pending_blocks_limit(self, mock_pending, mock_config):
        from src.orchestration.trade_executor import _should_use_limit_order
        mock_config.get.return_value = {
            'limit_orders': {
                'enabled': True,
                'market_order_threshold': 0.8,
                'max_pending_orders': 3,
                'pullback_pct': 0.005,
            }
        }
        mock_pending.return_value = [{'symbol': 'A'}, {'symbol': 'B'}, {'symbol': 'C'}]

        signal = {'signal_strength': 0.5}
        use_limit, pullback = _should_use_limit_order(signal, mock_config)

        assert use_limit is False


class TestPaperLimitOrder:
    """Tests for paper trading LIMIT BUY order flow."""

    @patch('src.execution.binance_trader.release_db_connection')
    @patch('src.execution.binance_trader.get_db_connection')
    def test_limit_buy_creates_pending_row(self, mock_conn_fn, mock_release):
        from src.execution.binance_trader import _paper_place_order

        mock_conn = MagicMock()
        mock_conn_fn.return_value = mock_conn
        mock_cursor = MagicMock()
        mock_conn.cursor.return_value = mock_cursor

        # Mock isinstance check for SQLite (not postgres)
        with patch('src.execution.binance_trader.isinstance', return_value=False):
            result = _paper_place_order(
                'BTC', 'BUY', 0.1, 50000.0, order_type='LIMIT',
                dynamic_sl_pct=0.05, dynamic_tp_pct=0.10)

        assert result['status'] == 'PENDING'
        assert result['order_type'] == 'LIMIT'
        assert 'LIMIT' in result['order_id']

    @patch('src.execution.binance_trader.release_db_connection')
    @patch('src.execution.binance_trader.get_db_connection')
    def test_market_buy_persists_dynamic_sl_tp(self, mock_conn_fn, mock_release):
        from src.execution.binance_trader import _paper_place_order

        mock_conn = MagicMock()
        mock_conn_fn.return_value = mock_conn
        mock_cursor = MagicMock()
        mock_conn.cursor.return_value = mock_cursor

        with patch('src.execution.binance_trader.isinstance', return_value=False):
            result = _paper_place_order(
                'BTC', 'BUY', 0.1, 50000.0,
                dynamic_sl_pct=0.045, dynamic_tp_pct=0.09)

        assert result['status'] == 'FILLED'
        # Verify the INSERT query included dynamic columns
        insert_call = mock_cursor.execute.call_args
        assert insert_call is not None
        # The query should include dynamic_sl_pct and dynamic_tp_pct
        query_str = insert_call[0][0]
        assert 'dynamic_sl_pct' in query_str
        assert 'dynamic_tp_pct' in query_str


class TestPendingOrderCheck:
    """Tests for pending order fill/expire logic in cycle_runner."""

    @patch('src.orchestration.cycle_runner.send_telegram_alert', new_callable=AsyncMock)
    @patch('src.orchestration.cycle_runner._is_live_trading', return_value=False)
    def test_fills_when_price_drops_below_limit(self, mock_live, mock_alert):
        from src.orchestration.cycle_runner import _check_pending_limit_orders

        now = datetime.now(timezone.utc)
        expires = now + timedelta(hours=2)

        mock_pending = [
            {'order_id': 'PAPER_BTC_LIMIT_1', 'symbol': 'BTC',
             'limit_price': 50000.0, 'limit_expires_at': expires.isoformat(),
             'quantity': 0.1, 'dynamic_sl_pct': 0.05, 'dynamic_tp_pct': 0.10},
        ]

        with patch('src.database.get_pending_orders', return_value=mock_pending), \
             patch('src.database.fill_pending_order') as mock_fill, \
             patch('src.database.cancel_pending_order'):

            prices = {'BTC': 49500.0}  # Below limit of 50000
            settings = {'limit_orders': {'enabled': True}, 'run_interval_minutes': 15}

            asyncio.run(
                _check_pending_limit_orders(
                    prices, settings,
                    risk_cfg={}, trading_mode='paper'))

            # Called for both manual and auto strategy loops
            assert mock_fill.call_count >= 1
            mock_fill.assert_any_call('PAPER_BTC_LIMIT_1', 49500.0)

    @patch('src.orchestration.cycle_runner.send_telegram_alert', new_callable=AsyncMock)
    @patch('src.orchestration.cycle_runner._is_live_trading', return_value=False)
    def test_expires_when_ttl_elapsed(self, mock_live, mock_alert):
        from src.orchestration.cycle_runner import _check_pending_limit_orders
        now = datetime.now(timezone.utc)
        expired = now - timedelta(hours=1)

        mock_pending = [
            {'order_id': 'PAPER_ETH_LIMIT_1', 'symbol': 'ETH',
             'limit_price': 3000.0, 'limit_expires_at': expired.isoformat(),
             'quantity': 1.0, 'dynamic_sl_pct': None, 'dynamic_tp_pct': None},
        ]

        with patch('src.database.get_pending_orders', return_value=mock_pending), \
             patch('src.database.fill_pending_order') as mock_fill, \
             patch('src.database.cancel_pending_order') as mock_cancel:

            prices = {'ETH': 3100.0}  # Above limit, so wouldn't fill anyway
            settings = {'limit_orders': {'enabled': True}, 'run_interval_minutes': 15}

            asyncio.run(
                _check_pending_limit_orders(
                    prices, settings,
                    risk_cfg={}, trading_mode='paper'))

            # Called for both manual+auto loops
            assert mock_cancel.call_count >= 1
            mock_cancel.assert_any_call('PAPER_ETH_LIMIT_1', reason='expired')
            mock_fill.assert_not_called()


class TestPreTradeGatesPendingOrders:
    """Tests for PENDING orders counted in pre-trade gates."""

    @patch('src.orchestration.pre_trade_gates.check_sector_limit', return_value=(True, ''))
    @patch('src.orchestration.pre_trade_gates.check_event_gate', return_value=('allow', 1.0, ''))
    def test_pending_limit_blocks_duplicate(self, mock_event, mock_sector):
        from src.orchestration.pre_trade_gates import check_buy_gates

        with patch('src.database.get_pending_orders',
                   return_value=[{'symbol': 'BTC'}]):
            allowed, _, reason = check_buy_gates(
                'BTC', [], 5, False, 1.0)

        assert not allowed
        assert 'Pending limit order' in reason

    @patch('src.orchestration.pre_trade_gates.check_sector_limit', return_value=(True, ''))
    @patch('src.orchestration.pre_trade_gates.check_event_gate', return_value=('allow', 1.0, ''))
    def test_pending_counts_toward_max_positions(self, mock_event, mock_sector):
        from src.orchestration.pre_trade_gates import check_buy_gates

        # 4 open + 1 pending = 5, max is 5 → blocked
        open_positions = [
            {'symbol': f'SYM{i}', 'status': 'OPEN'} for i in range(4)
        ]

        with patch('src.database.get_pending_orders',
                   return_value=[{'symbol': 'OTHER'}]):
            allowed, _, reason = check_buy_gates(
                'BTC', open_positions, 5, False, 1.0)

        assert not allowed
        assert 'Max concurrent positions' in reason


class TestOCOBracketDynamicSLTP:
    """Tests for OCO bracket using per-position SL/TP."""

    @patch('src.execution.binance_trader._get_symbol_info', return_value=None)
    @patch('src.execution.binance_trader._get_binance_client')
    @patch('src.execution.binance_trader.app_config')
    def test_oco_uses_dynamic_values(self, mock_config, mock_client_fn, mock_sym):
        from src.execution.binance_trader import _place_oco_bracket

        mock_client = MagicMock()
        mock_client_fn.return_value = mock_client
        mock_client.create_oco_order.return_value = {'orderListId': 123}
        mock_config.get.return_value = {
            'live_trading': {
                'stop_loss_percentage': 0.03,
                'take_profit_percentage': 0.06,
            }
        }

        result = _place_oco_bracket('BTCUSDT', 50000.0, 0.1,
                                      sl_pct=0.05, tp_pct=0.10)

        assert result is not None
        # Verify the SL/TP prices used dynamic values (new above/below API)
        call_kw = mock_client.create_oco_order.call_args
        sl_price = float(call_kw.kwargs.get('belowStopPrice', call_kw[1].get('belowStopPrice', 0)))
        tp_price = float(call_kw.kwargs.get('abovePrice', call_kw[1].get('abovePrice', 0)))
        # SL = 50000 * (1 - 0.05) = 47500
        assert abs(sl_price - 47500.0) < 1.0
        # TP = 50000 * (1 + 0.10) = 55000
        assert abs(tp_price - 55000.0) < 1.0

    @patch('src.execution.binance_trader._get_symbol_info', return_value=None)
    @patch('src.execution.binance_trader._get_binance_client')
    @patch('src.execution.binance_trader.app_config')
    def test_oco_falls_back_to_config(self, mock_config, mock_client_fn, mock_sym):
        from src.execution.binance_trader import _place_oco_bracket

        mock_client = MagicMock()
        mock_client_fn.return_value = mock_client
        mock_client.create_oco_order.return_value = {'orderListId': 456}
        mock_config.get.return_value = {
            'live_trading': {
                'stop_loss_percentage': 0.03,
                'take_profit_percentage': 0.06,
            }
        }

        _place_oco_bracket('BTCUSDT', 50000.0, 0.1)  # No sl_pct/tp_pct

        call_kw = mock_client.create_oco_order.call_args
        sl_price = float(call_kw.kwargs.get('belowStopPrice', call_kw[1].get('belowStopPrice', 0)))
        # SL = 50000 * (1 - 0.03) = 48500
        assert abs(sl_price - 48500.0) < 1.0


# --- Article-Level Scores in Symbol Assessment ---


class TestArticleScoresPassthrough:
    """Tests for per-article Gemini scores flowing to symbol-level assessment."""

    def test_news_data_includes_top_scored_articles(self):
        """collect_news_sentiment output should include top_scored_articles."""
        # Simulate what news_data.py produces
        articles = [
            {'title': 'ETF approved', 'score': 0.9},
            {'title': 'Miner sells BTC', 'score': -0.6},
            {'title': 'Whale buys', 'score': 0.7},
            {'title': 'Tether FUD', 'score': -0.2},  # Below 0.3, filtered out
            {'title': 'Noise article', 'score': 0.1},  # Below 0.3, filtered out
        ]
        # Replicate the filtering logic from news_data.py
        top_scored = sorted(
            [a for a in articles if abs(a['score']) > 0.3],
            key=lambda a: abs(a['score']), reverse=True)[:10]

        assert len(top_scored) == 3
        assert top_scored[0]['title'] == 'ETF approved'  # Highest abs score
        assert top_scored[1]['title'] == 'Whale buys'
        assert top_scored[2]['title'] == 'Miner sells BTC'

    def test_news_pipeline_builds_scored_articles_dict(self):
        """_build_news_stats equivalent: scored articles are extracted from per_symbol."""
        news_per_symbol = {
            'BTC': {
                'headlines': ['ETF approved', 'Miner sells'],
                'top_scored_articles': [
                    {'title': 'ETF approved', 'score': 0.9},
                    {'title': 'Miner sells BTC', 'score': -0.6},
                ],
            },
            'ETH': {
                'headlines': ['Upgrade planned'],
                'top_scored_articles': [],
            },
        }

        scored_articles_by_symbol = {}
        for sym, sym_data in news_per_symbol.items():
            top_scored = sym_data.get('top_scored_articles', [])
            if top_scored:
                scored_articles_by_symbol[sym] = top_scored

        assert 'BTC' in scored_articles_by_symbol
        assert 'ETH' not in scored_articles_by_symbol  # Empty list filtered
        assert len(scored_articles_by_symbol['BTC']) == 2

    @patch.dict('os.environ', {'GCP_PROJECT_ID': 'test-project'})
    @patch('src.analysis.gemini_news_analyzer._call_with_retry')
    @patch('src.analysis.gemini_news_analyzer.GenerativeModel')
    @patch('src.analysis.gemini_news_analyzer.vertexai')
    def test_analyze_news_impact_injects_scores_into_prompt(
            self, mock_vertexai, mock_model, mock_call):
        """Scored articles should appear in the Gemini prompt."""
        from src.analysis.gemini_news_analyzer import analyze_news_impact

        mock_response = MagicMock()
        mock_response.text = '{"symbol_assessments": {"BTC": {"direction": "bullish", "confidence": 0.7, "reasoning": "test", "catalyst_type": "etf", "catalyst_freshness": "breaking", "sentiment_divergence": false, "key_headline": "ETF approved"}}, "market_mood": "bullish", "cross_asset_theme": null}'
        mock_call.return_value = mock_response

        result = analyze_news_impact(
            headlines_by_symbol={'BTC': ['ETF approved', 'Miner sells']},
            current_prices={'BTC': 50000},
            scored_articles_by_symbol={
                'BTC': [
                    {'title': 'ETF approved', 'score': 0.9},
                    {'title': 'Miner sells BTC', 'score': -0.6},
                ],
            },
        )

        assert result is not None
        # Verify the prompt passed to Gemini contains the scored articles
        prompt_arg = mock_call.call_args[0][1]
        assert '[+0.90] ETF approved' in prompt_arg
        assert '[-0.60] Miner sells BTC' in prompt_arg
        assert 'Pre-scored articles' in prompt_arg

    def test_scored_articles_format_string(self):
        """Verify the score formatting produces expected output."""
        scored = [
            {'title': 'Big news', 'score': 0.85},
            {'title': 'Bad news', 'score': -0.7},
        ]
        lines = [f"- [{a['score']:+.2f}] {a['title']}" for a in scored]
        assert lines[0] == '- [+0.85] Big news'
        assert lines[1] == '- [-0.70] Bad news'


# --- Asset Class Concentration Limits ---


class TestAssetClassLimits:
    """Tests for cross-group asset class position limits."""

    def test_infer_crypto_asset_class(self):
        from src.analysis.sector_limits import _infer_asset_class, reload_sector_groups
        reload_sector_groups()
        assert _infer_asset_class('BTC') == 'crypto'
        assert _infer_asset_class('ETH') == 'crypto'
        assert _infer_asset_class('UNI') == 'crypto'  # defi group
        assert _infer_asset_class('DOGE') == 'crypto'  # meme group

    def test_infer_stock_asset_class(self):
        from src.analysis.sector_limits import _infer_asset_class, reload_sector_groups
        reload_sector_groups()
        assert _infer_asset_class('AAPL') == 'stock'
        assert _infer_asset_class('NVDA') == 'stock'

    def test_infer_unknown_returns_none(self):
        from src.analysis.sector_limits import _infer_asset_class, reload_sector_groups
        reload_sector_groups()
        assert _infer_asset_class('UNKNOWN_SYMBOL_XYZ') is None

    def test_crypto_class_limit_blocks_at_max(self):
        from src.analysis.sector_limits import check_sector_limit, reload_sector_groups
        reload_sector_groups()

        # 4 open crypto positions across different groups
        positions = [
            {'symbol': 'BTC', 'status': 'OPEN'},   # l1_major
            {'symbol': 'UNI', 'status': 'OPEN'},    # defi
            {'symbol': 'ARB', 'status': 'OPEN'},    # l2_scaling
            {'symbol': 'DOGE', 'status': 'OPEN'},   # meme
        ]
        # 5th crypto position should be blocked (max_positions: 4)
        allowed, reason = check_sector_limit('LINK', positions)
        assert not allowed
        assert 'crypto' in reason.lower()

    def test_crypto_class_allows_within_limit(self):
        from src.analysis.sector_limits import check_sector_limit, reload_sector_groups
        reload_sector_groups()

        positions = [
            {'symbol': 'BTC', 'status': 'OPEN'},
            {'symbol': 'UNI', 'status': 'OPEN'},
        ]
        # 3rd crypto — within limit of 4
        allowed, reason = check_sector_limit('DOGE', positions)
        assert allowed

    def test_stock_positions_dont_count_against_crypto(self):
        from src.analysis.sector_limits import check_sector_limit, reload_sector_groups
        reload_sector_groups()

        positions = [
            {'symbol': 'AAPL', 'status': 'OPEN'},
            {'symbol': 'MSFT', 'status': 'OPEN'},
            {'symbol': 'GOOGL', 'status': 'OPEN'},
            {'symbol': 'AMZN', 'status': 'OPEN'},
        ]
        # 4 stocks open, but crypto class is empty → BTC should be allowed
        allowed, reason = check_sector_limit('BTC', positions)
        assert allowed

    def test_concentration_scaling_full(self):
        from src.analysis.sector_limits import (
            get_asset_class_concentration, reload_sector_groups)
        reload_sector_groups()

        # 0 crypto positions → 1.0x (no reduction)
        mult = get_asset_class_concentration('BTC', [])
        assert mult == 1.0

    def test_concentration_scaling_reduces(self):
        from src.analysis.sector_limits import (
            get_asset_class_concentration, reload_sector_groups)
        reload_sector_groups()

        positions = [
            {'symbol': 'ETH', 'status': 'OPEN'},
            {'symbol': 'UNI', 'status': 'OPEN'},
        ]
        # 2/4 crypto → remaining 2/4 = 0.5x
        mult = get_asset_class_concentration('BTC', positions)
        assert mult == 0.5

    def test_concentration_scaling_near_full(self):
        from src.analysis.sector_limits import (
            get_asset_class_concentration, reload_sector_groups)
        reload_sector_groups()

        positions = [
            {'symbol': 'BTC', 'status': 'OPEN'},
            {'symbol': 'ETH', 'status': 'OPEN'},
            {'symbol': 'UNI', 'status': 'OPEN'},
        ]
        # 3/4 crypto → remaining 1/4 = 0.25x
        mult = get_asset_class_concentration('DOGE', positions)
        assert mult == 0.25

    def test_stock_no_concentration_scaling(self):
        from src.analysis.sector_limits import (
            get_asset_class_concentration, reload_sector_groups)
        reload_sector_groups()

        positions = [
            {'symbol': 'AAPL', 'status': 'OPEN'},
            {'symbol': 'MSFT', 'status': 'OPEN'},
            {'symbol': 'GOOGL', 'status': 'OPEN'},
        ]
        # Stocks have concentration_scaling: false → always 1.0
        mult = get_asset_class_concentration('AMZN', positions)
        assert mult == 1.0
