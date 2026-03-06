"""Tests for AI chat module (src/notify/telegram_chat.py)."""

import asyncio
import os
import time
from unittest.mock import AsyncMock, MagicMock, patch

from src.notify.telegram_chat import (
    ChatSession,
    INTERNAL_KEYWORDS,
    SEARCH_KEYWORDS,
    _build_system_instruction,
    _build_trade_buttons,
    _gather_context,
    _needs_web_search,
    _parse_trade_suggestions,
    _sessions,
    cleanup_expired_sessions,
    clear_session,
    get_or_create_session,
    handle_chat_message,
    handle_chat_trade_callback,
)


def _run(coro):
    """Helper to run async tests without pytest-asyncio."""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


# --- ChatSession Tests ---
class TestChatSession:
    def test_new_session_not_expired(self):
        s = ChatSession(user_id=1)
        assert not s.is_expired()

    def test_expired_session(self):
        s = ChatSession(user_id=1)
        s.last_activity = time.time() - 3600
        assert s.is_expired()

    def test_not_rate_limited_initially(self):
        s = ChatSession(user_id=1)
        assert not s.is_rate_limited()

    def test_rate_limited_after_burst(self):
        s = ChatSession(user_id=1)
        now = time.time()
        s.message_timestamps = [now - i for i in range(25)]
        assert s.is_rate_limited()

    def test_rate_limit_window_expires(self):
        s = ChatSession(user_id=1)
        s.message_timestamps = [time.time() - 700 for _ in range(25)]
        assert not s.is_rate_limited()

    def test_add_user_message(self):
        s = ChatSession(user_id=1)
        s.add_user_message("hello")
        assert len(s.history) == 1
        assert s.history[0] == {"role": "user", "text": "hello"}
        assert len(s.message_timestamps) == 1

    def test_add_model_response(self):
        s = ChatSession(user_id=1)
        s.add_model_response("hi there")
        assert len(s.history) == 1
        assert s.history[0] == {"role": "model", "text": "hi there"}

    def test_trim_history_by_turns(self):
        s = ChatSession(user_id=1)
        for i in range(50):
            s.history.append({"role": "user", "text": f"msg {i}"})
        s._trim_history()
        assert len(s.history) <= 40  # MAX_HISTORY_TURNS * 2

    def test_trim_history_by_chars(self):
        s = ChatSession(user_id=1)
        for i in range(10):
            s.history.append({"role": "user", "text": "x" * 5000})
        s._trim_history()
        total = sum(len(m["text"]) for m in s.history)
        assert total <= 30_000 or len(s.history) <= 2

    def test_activity_updated_on_message(self):
        s = ChatSession(user_id=1)
        s.last_activity = time.time() - 100
        s.add_user_message("test")
        assert time.time() - s.last_activity < 2


# --- Session Store Tests ---
class TestSessionStore:
    def setup_method(self):
        _sessions.clear()

    def test_create_new_session(self):
        s = get_or_create_session(42)
        assert s.user_id == 42
        assert 42 in _sessions

    def test_retrieve_existing_session(self):
        s1 = get_or_create_session(42)
        s1.add_user_message("hello")
        s2 = get_or_create_session(42)
        assert s2 is s1
        assert len(s2.history) == 1

    def test_replace_expired_session(self):
        s1 = get_or_create_session(42)
        s1.add_user_message("old")
        s1.last_activity = time.time() - 3600
        s2 = get_or_create_session(42)
        assert s2 is not s1
        assert len(s2.history) == 0

    def test_clear_session(self):
        get_or_create_session(42)
        clear_session(42)
        assert 42 not in _sessions

    def test_clear_nonexistent_session(self):
        clear_session(999)

    def test_cleanup_expired(self):
        s1 = get_or_create_session(1)
        get_or_create_session(2)
        s1.last_activity = time.time() - 3600
        cleanup_expired_sessions()
        assert 1 not in _sessions
        assert 2 in _sessions

    def teardown_method(self):
        _sessions.clear()


# --- Trade Suggestion Parser Tests ---
class TestParseTradeSuggestions:
    def test_no_suggestions(self):
        text = "The market looks bullish today."
        clean, trades = _parse_trade_suggestions(text)
        assert clean == text
        assert trades == []

    def test_single_suggestion(self):
        tag = '[TRADE_SUGGESTION]{"action":"BUY","symbol":"BTC","reason":"breakout"}[/TRADE_SUGGESTION]'
        text = f"I recommend buying BTC. {tag}"
        clean, trades = _parse_trade_suggestions(text)
        assert "[TRADE_SUGGESTION]" not in clean
        assert len(trades) == 1
        assert trades[0]["action"] == "BUY"
        assert trades[0]["symbol"] == "BTC"
        assert trades[0]["asset_type"] == "crypto"

    def test_multiple_suggestions(self):
        t1 = '[TRADE_SUGGESTION]{"action":"BUY","symbol":"ETH","reason":"dip"}[/TRADE_SUGGESTION]'
        t2 = '[TRADE_SUGGESTION]{"action":"SELL","symbol":"BTC","reason":"overbought"}[/TRADE_SUGGESTION]'
        text = f"Analysis: {t1} and {t2}"
        clean, trades = _parse_trade_suggestions(text)
        assert len(trades) == 2
        assert trades[0]["symbol"] == "ETH"
        assert trades[1]["symbol"] == "BTC"

    def test_malformed_json(self):
        tag = '[TRADE_SUGGESTION]{bad json}[/TRADE_SUGGESTION]'
        text = f"Some text {tag}"
        clean, trades = _parse_trade_suggestions(text)
        assert trades == []
        assert "[TRADE_SUGGESTION]" not in clean

    def test_missing_required_fields(self):
        tag = '[TRADE_SUGGESTION]{"reason":"no action or symbol"}[/TRADE_SUGGESTION]'
        text = f"Text {tag}"
        _, trades = _parse_trade_suggestions(text)
        assert trades == []

    def test_stock_asset_type(self):
        tag = '[TRADE_SUGGESTION]{"action":"BUY","symbol":"AAPL","asset_type":"stock","reason":"earnings"}[/TRADE_SUGGESTION]'
        _, trades = _parse_trade_suggestions(tag)
        assert trades[0]["asset_type"] == "stock"

    def test_tags_stripped_from_text(self):
        tag = '[TRADE_SUGGESTION]{"action":"BUY","symbol":"BTC","reason":"x"}[/TRADE_SUGGESTION]'
        text = f"Before. {tag} After."
        clean, _ = _parse_trade_suggestions(text)
        assert "Before." in clean
        assert "After." in clean
        assert "[TRADE_SUGGESTION]" not in clean


# --- Trade Buttons Tests ---
class TestBuildTradeButtons:
    def setup_method(self):
        _sessions.clear()

    def test_no_trades_returns_none(self):
        s = ChatSession(user_id=1)
        result = _build_trade_buttons(s, [])
        assert result is None

    def test_buttons_created(self):
        s = ChatSession(user_id=1)
        trades = [{"action": "BUY", "symbol": "BTC", "reason": "test"}]
        markup = _build_trade_buttons(s, trades)
        assert markup is not None
        assert len(markup.inline_keyboard) == 1
        row = markup.inline_keyboard[0]
        assert len(row) == 2
        assert "BUY BTC" in row[0].text
        assert row[1].text == "Skip"

    def test_callback_data_format(self):
        s = ChatSession(user_id=42)
        trades = [{"action": "SELL", "symbol": "ETH", "reason": "test"}]
        markup = _build_trade_buttons(s, trades)
        row = markup.inline_keyboard[0]
        assert row[0].callback_data.startswith("ct:42:")
        assert row[0].callback_data.endswith(":a")
        assert row[1].callback_data.endswith(":r")

    def test_pending_trades_stored(self):
        s = ChatSession(user_id=1)
        trades = [{"action": "BUY", "symbol": "BTC", "reason": "x"}]
        _build_trade_buttons(s, trades)
        assert len(s.pending_trades) == 1

    def teardown_method(self):
        _sessions.clear()


# --- Context Gathering Tests ---
class TestGatherContext:
    @patch('src.notify.telegram_chat._gather_context_sync')
    def test_gather_context_success(self, mock_sync):
        mock_sync.return_value = "## Open Positions: None"
        result = _run(_gather_context())
        assert "Open Positions" in result

    @patch('src.notify.telegram_chat._gather_context_sync')
    def test_gather_context_error(self, mock_sync):
        mock_sync.side_effect = RuntimeError("db down")
        result = _run(_gather_context())
        assert "error" in result.lower()


# --- System Instruction Tests ---
class TestBuildSystemInstruction:
    def test_includes_context(self):
        result = _build_system_instruction("## Positions: BTC")
        assert "Positions: BTC" in result
        assert "trading assistant" in result.lower()

    def test_includes_timestamp(self):
        result = _build_system_instruction("")
        assert "UTC" in result

    def test_search_mode_instruction(self):
        result = _build_system_instruction("ctx", use_search=True)
        assert "Google Search" in result
        assert "search:" not in result.lower() or "search results" in result.lower()

    def test_no_search_mode_instruction(self):
        result = _build_system_instruction("ctx", use_search=False)
        assert "ONLY" in result
        assert "search:" in result


# --- Handler Tests ---
class TestHandleChatMessage:
    def setup_method(self):
        _sessions.clear()

    def _make_update(self, text="hello", user_id=1):
        update = MagicMock(spec=['message'])
        update.message = MagicMock()
        update.message.from_user = MagicMock()
        update.message.from_user.id = user_id
        update.message.text = text
        update.message.reply_text = AsyncMock()
        update.message.chat = MagicMock()
        update.message.chat.send_action = AsyncMock()
        return update

    @patch('src.notify.telegram_chat._call_gemini', new_callable=AsyncMock)
    @patch('src.notify.telegram_chat._gather_context', new_callable=AsyncMock)
    @patch('src.notify.telegram_chat.AUTHORIZED_USER_IDS', [1])
    @patch('src.notify.telegram_chat._chat_config', {'enabled': True})
    def test_basic_response(self, mock_ctx, mock_gemini):
        mock_ctx.return_value = "context"
        mock_gemini.return_value = "The market is bullish."
        update = self._make_update("What's happening?")
        _run(handle_chat_message(update, MagicMock()))
        update.message.reply_text.assert_called_once()
        args = update.message.reply_text.call_args
        assert "bullish" in args[0][0]

    @patch('src.notify.telegram_chat.AUTHORIZED_USER_IDS', [1])
    @patch('src.notify.telegram_chat._chat_config', {'enabled': True})
    def test_unauthorized_user(self):
        update = self._make_update(user_id=999)
        _run(handle_chat_message(update, MagicMock()))
        update.message.reply_text.assert_called_once_with("Not authorized.")

    @patch('src.notify.telegram_chat._call_gemini', new_callable=AsyncMock)
    @patch('src.notify.telegram_chat._gather_context', new_callable=AsyncMock)
    @patch('src.notify.telegram_chat.AUTHORIZED_USER_IDS', [1])
    @patch('src.notify.telegram_chat._chat_config', {'enabled': True})
    def test_rate_limited(self, mock_ctx, mock_gemini):
        session = get_or_create_session(1)
        session.message_timestamps = [time.time() for _ in range(25)]
        update = self._make_update()
        _run(handle_chat_message(update, MagicMock()))
        update.message.reply_text.assert_called_once()
        assert "rate limit" in update.message.reply_text.call_args[0][0].lower()

    @patch('src.notify.telegram_chat._chat_config', {'enabled': False})
    def test_disabled_chat(self):
        update = self._make_update()
        _run(handle_chat_message(update, MagicMock()))
        update.message.reply_text.assert_not_called()

    @patch('src.notify.telegram_chat._call_gemini', new_callable=AsyncMock)
    @patch('src.notify.telegram_chat._gather_context', new_callable=AsyncMock)
    @patch('src.notify.telegram_chat.AUTHORIZED_USER_IDS', [1])
    @patch('src.notify.telegram_chat._chat_config', {'enabled': True})
    def test_trade_suggestion_buttons(self, mock_ctx, mock_gemini):
        mock_ctx.return_value = "ctx"
        tag = '[TRADE_SUGGESTION]{"action":"BUY","symbol":"BTC","reason":"breakout"}[/TRADE_SUGGESTION]'
        mock_gemini.return_value = f"Buy BTC now. {tag}"
        update = self._make_update("Should I buy BTC?")
        _run(handle_chat_message(update, MagicMock()))
        call_kwargs = update.message.reply_text.call_args
        assert call_kwargs.kwargs.get('reply_markup') is not None

    def teardown_method(self):
        _sessions.clear()


# --- Trade Callback Tests ---
class TestHandleChatTradeCallback:
    def setup_method(self):
        _sessions.clear()

    def _make_query_update(self, data, user_id=1):
        update = MagicMock(spec=['callback_query'])
        update.callback_query = MagicMock()
        update.callback_query.from_user = MagicMock()
        update.callback_query.from_user.id = user_id
        update.callback_query.data = data
        update.callback_query.answer = AsyncMock()
        update.callback_query.edit_message_reply_markup = AsyncMock()
        update.callback_query.message = MagicMock()
        update.callback_query.message.reply_text = AsyncMock()
        return update

    @patch('src.notify.telegram_chat.AUTHORIZED_USER_IDS', [1])
    def test_reject_trade(self):
        session = get_or_create_session(1)
        session.pending_trades[100] = {"action": "BUY", "symbol": "BTC", "reason": "test"}
        update = self._make_query_update("ct:1:100:r")
        _run(handle_chat_trade_callback(update, MagicMock()))
        assert 100 not in session.pending_trades
        update.callback_query.message.reply_text.assert_called_once()
        assert "Skipped" in update.callback_query.message.reply_text.call_args[0][0]

    @patch('src.notify.telegram_chat.AUTHORIZED_USER_IDS', [1])
    def test_approve_buy(self):
        mock_exec_fn = AsyncMock(return_value={"status": "FILLED"})
        # to_thread calls: 1) _get_price → 50000.0, 2) get_account_balance → dict
        to_thread_results = [50000.0, {'total': 10000.0}]
        to_thread_mock = AsyncMock(side_effect=to_thread_results)
        with patch('src.notify.telegram_chat._execute_callback', mock_exec_fn), \
             patch('src.notify.telegram_chat.asyncio.to_thread', to_thread_mock):
            session = get_or_create_session(1)
            session.pending_trades[101] = {
                "action": "BUY", "symbol": "BTC",
                "asset_type": "crypto", "reason": "test"
            }
            update = self._make_query_update("ct:1:101:a")
            _run(handle_chat_trade_callback(update, MagicMock()))
            mock_exec_fn.assert_called_once()
            sig = mock_exec_fn.call_args[0][0]
            assert sig['signal'] == 'BUY'
            assert sig['symbol'] == 'BTC'
            assert sig['current_price'] == 50000.0
            # qty = (10000 * 0.02) / 50000 = 0.004
            assert sig['quantity'] > 0

    @patch('src.notify.telegram_chat.AUTHORIZED_USER_IDS', [1])
    def test_expired_session(self):
        update = self._make_query_update("ct:1:100:a")
        _run(handle_chat_trade_callback(update, MagicMock()))
        update.callback_query.message.reply_text.assert_called()
        assert "expired" in update.callback_query.message.reply_text.call_args[0][0].lower()

    @patch('src.notify.telegram_chat.AUTHORIZED_USER_IDS', [1, 2])
    def test_wrong_user(self):
        session = get_or_create_session(1)
        session.pending_trades[100] = {"action": "BUY", "symbol": "BTC", "reason": "x"}
        update = self._make_query_update("ct:1:100:a", user_id=2)
        _run(handle_chat_trade_callback(update, MagicMock()))
        update.callback_query.answer.assert_called_with("Not your trade.", show_alert=True)

    @patch('src.notify.telegram_chat.AUTHORIZED_USER_IDS', [999])
    def test_unauthorized_callback(self):
        update = self._make_query_update("ct:1:100:a", user_id=1)
        _run(handle_chat_trade_callback(update, MagicMock()))
        update.callback_query.answer.assert_called_with("Not authorized.", show_alert=True)

    @patch('src.notify.telegram_chat.AUTHORIZED_USER_IDS', [1])
    def test_approve_sell(self):
        mock_exec_fn = AsyncMock(return_value={"status": "FILLED"})
        pos = {'symbol': 'AAPL', 'quantity': 10, 'entry_price': 140.0, 'asset_type': 'stock'}
        to_thread_results = [150.0, [pos]]
        to_thread_mock = AsyncMock(side_effect=to_thread_results)
        with patch('src.notify.telegram_chat._execute_callback', mock_exec_fn), \
             patch('src.notify.telegram_chat.asyncio.to_thread', to_thread_mock):
            session = get_or_create_session(1)
            session.pending_trades[102] = {
                "action": "SELL", "symbol": "AAPL",
                "asset_type": "stock", "reason": "take profit"
            }
            update = self._make_query_update("ct:1:102:a")
            _run(handle_chat_trade_callback(update, MagicMock()))
            mock_exec_fn.assert_called_once()
            sig = mock_exec_fn.call_args[0][0]
            assert sig['signal'] == 'SELL'
            assert sig['quantity'] == 10

    def teardown_method(self):
        _sessions.clear()


# --- Search Routing Tests ---
class TestNeedsWebSearch:
    def test_explicit_search_prefix(self):
        needs, cleaned = _needs_web_search("search: what is BTC price")
        assert needs is True
        assert cleaned == "what is BTC price"

    def test_explicit_search_prefix_case_insensitive(self):
        needs, cleaned = _needs_web_search("Search: latest news")
        assert needs is True
        assert cleaned == "latest news"

    def test_search_keywords_trigger(self):
        needs, _ = _needs_web_search("What's the latest news on crypto?")
        assert needs is True

    def test_news_keyword_triggers_search(self):
        needs, _ = _needs_web_search("Any news about ETH?")
        assert needs is True

    def test_earnings_triggers_search(self):
        needs, _ = _needs_web_search("When are AAPL earnings?")
        assert needs is True

    def test_internal_keywords_suppress_search(self):
        needs, _ = _needs_web_search("What are my positions?")
        assert needs is False

    def test_portfolio_is_internal(self):
        needs, _ = _needs_web_search("Show me my portfolio")
        assert needs is False

    def test_pnl_is_internal(self):
        needs, _ = _needs_web_search("What's my PnL today?")
        assert needs is False

    def test_circuit_breaker_is_internal(self):
        needs, _ = _needs_web_search("Is the circuit breaker triggered?")
        assert needs is False

    def test_buy_is_internal(self):
        needs, _ = _needs_web_search("Should I buy more BTC?")
        assert needs is False

    def test_internal_takes_priority_over_search(self):
        # "trade" is internal, "news" is search — internal wins
        needs, _ = _needs_web_search("What's the latest trade history?")
        assert needs is False

    def test_default_no_search(self):
        needs, _ = _needs_web_search("Hello, how are you?")
        assert needs is False

    def test_empty_message(self):
        needs, _ = _needs_web_search("")
        assert needs is False

    def test_keyword_sets_are_non_empty(self):
        assert len(SEARCH_KEYWORDS) > 5
        assert len(INTERNAL_KEYWORDS) > 5


# --- Gemini Routing Tests ---
class TestCallGeminiRouting:
    @patch('src.notify.telegram_chat.os.environ.get')
    def test_no_project_id(self, mock_env):
        mock_env.return_value = None
        session = ChatSession(user_id=1)
        from src.notify.telegram_chat import _call_gemini
        result = _run(_call_gemini(session, "hello", "ctx", use_search=False))
        assert "unavailable" in result.lower()

    @patch.dict(os.environ, {'GCP_PROJECT_ID': 'my-project'})
    def test_search_calls_gemini(self):
        """Verify _call_gemini completes when use_search=True."""
        mock_response = MagicMock()
        mock_response.text = "Search result"

        with patch('google.genai.Client'), \
             patch('src.notify.telegram_chat.asyncio.to_thread',
                   new_callable=AsyncMock) as mock_thread:
            mock_thread.return_value = mock_response
            session = ChatSession(user_id=1)
            from src.notify.telegram_chat import _call_gemini
            result = _run(_call_gemini(session, "news", "ctx", use_search=True))
            assert mock_thread.called
            assert result == "Search result"

    @patch.dict(os.environ, {'GCP_PROJECT_ID': 'my-project'})
    def test_no_search_calls_gemini(self):
        """Verify _call_gemini completes when use_search=False."""
        mock_response = MagicMock()
        mock_response.text = "Internal answer"

        with patch('google.genai.Client'), \
             patch('src.notify.telegram_chat.asyncio.to_thread',
                   new_callable=AsyncMock) as mock_thread:
            mock_thread.return_value = mock_response
            session = ChatSession(user_id=1)
            from src.notify.telegram_chat import _call_gemini
            result = _run(_call_gemini(session, "positions", "ctx", use_search=False))
            assert mock_thread.called
            assert result == "Internal answer"


# --- Rich Context Tests ---
class TestGatherContextRich:
    @patch('src.notify.telegram_chat._gather_context_sync')
    def test_context_includes_trade_summary(self, mock_sync):
        mock_sync.return_value = (
            "## Open Positions: None\n"
            "## 24h Trade Performance\n"
            "- Closed: 5 (W:3 / L:2)\n"
            "- PnL: $150.00\n"
            "- Win rate: 60%"
        )
        result = _run(_gather_context())
        assert "Trade Performance" in result
        assert "Win rate" in result

    @patch('src.notify.telegram_chat._gather_context_sync')
    def test_context_includes_sentiment(self, mock_sync):
        mock_sync.return_value = (
            "## News Sentiment (held positions)\n"
            "- BTC: bullish (score=0.35, articles=12)"
        )
        result = _run(_gather_context())
        assert "Sentiment" in result
        assert "BTC" in result

    @patch('src.notify.telegram_chat._gather_context_sync')
    def test_context_includes_headlines(self, mock_sync):
        mock_sync.return_value = (
            "## Recent Headlines (24h)\n"
            "- [BTC] Bitcoin hits new high"
        )
        result = _run(_gather_context())
        assert "Headlines" in result
        assert "Bitcoin" in result


# --- Handler Routing Tests ---
class TestHandleChatMessageRouting:
    def setup_method(self):
        _sessions.clear()

    def _make_update(self, text="hello", user_id=1):
        update = MagicMock(spec=['message'])
        update.message = MagicMock()
        update.message.from_user = MagicMock()
        update.message.from_user.id = user_id
        update.message.text = text
        update.message.reply_text = AsyncMock()
        update.message.chat = MagicMock()
        update.message.chat.send_action = AsyncMock()
        return update

    @patch('src.notify.telegram_chat._call_gemini', new_callable=AsyncMock)
    @patch('src.notify.telegram_chat._gather_context', new_callable=AsyncMock)
    @patch('src.notify.telegram_chat.AUTHORIZED_USER_IDS', [1])
    @patch('src.notify.telegram_chat._chat_config', {'enabled': True})
    def test_portfolio_question_no_search(self, mock_ctx, mock_gemini):
        mock_ctx.return_value = "context"
        mock_gemini.return_value = "You have 2 positions."
        update = self._make_update("What are my positions?")
        _run(handle_chat_message(update, MagicMock()))
        call_kwargs = mock_gemini.call_args
        assert call_kwargs.kwargs.get('use_search') is False

    @patch('src.notify.telegram_chat._call_gemini', new_callable=AsyncMock)
    @patch('src.notify.telegram_chat._gather_context', new_callable=AsyncMock)
    @patch('src.notify.telegram_chat.AUTHORIZED_USER_IDS', [1])
    @patch('src.notify.telegram_chat._chat_config', {'enabled': True})
    def test_news_question_triggers_search(self, mock_ctx, mock_gemini):
        mock_ctx.return_value = "context"
        mock_gemini.return_value = "Fed announced rate cut."
        update = self._make_update("What's the latest news on Fed?")
        _run(handle_chat_message(update, MagicMock()))
        call_kwargs = mock_gemini.call_args
        assert call_kwargs.kwargs.get('use_search') is True

    @patch('src.notify.telegram_chat._call_gemini', new_callable=AsyncMock)
    @patch('src.notify.telegram_chat._gather_context', new_callable=AsyncMock)
    @patch('src.notify.telegram_chat.AUTHORIZED_USER_IDS', [1])
    @patch('src.notify.telegram_chat._chat_config', {'enabled': True})
    def test_search_prefix_forces_search(self, mock_ctx, mock_gemini):
        mock_ctx.return_value = "context"
        mock_gemini.return_value = "NVDA results."
        update = self._make_update("search: is there any NVDA news today?")
        _run(handle_chat_message(update, MagicMock()))
        call_kwargs = mock_gemini.call_args
        assert call_kwargs.kwargs.get('use_search') is True
        # Verify "search:" prefix was stripped from message
        sent_message = call_kwargs[0][1]  # second positional arg
        assert not sent_message.startswith("search:")

    @patch('src.notify.telegram_chat._call_gemini', new_callable=AsyncMock)
    @patch('src.notify.telegram_chat._gather_context', new_callable=AsyncMock)
    @patch('src.notify.telegram_chat.AUTHORIZED_USER_IDS', [1])
    @patch('src.notify.telegram_chat._chat_config', {'enabled': True})
    def test_default_message_no_search(self, mock_ctx, mock_gemini):
        mock_ctx.return_value = "context"
        mock_gemini.return_value = "Hello!"
        update = self._make_update("Hello there")
        _run(handle_chat_message(update, MagicMock()))
        call_kwargs = mock_gemini.call_args
        assert call_kwargs.kwargs.get('use_search') is False

    def teardown_method(self):
        _sessions.clear()
