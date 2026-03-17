"""AI Chat module for Telegram bot — conversational interface with trade execution."""

import asyncio
import itertools
import json
import os
import re
import time
from dataclasses import dataclass, field
from typing import Callable, Optional

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from src.config import app_config
from src.logger import log

# --- Configuration ---
_chat_config = app_config.get('settings', {}).get(
    'telegram_enhancements', {}).get('ai_chat', {})
SESSION_TTL_SECONDS = _chat_config.get('session_ttl_minutes', 30) * 60
RATE_LIMIT_MESSAGES = _chat_config.get('rate_limit_messages', 20)
RATE_LIMIT_WINDOW = _chat_config.get('rate_limit_window_seconds', 600)
MAX_HISTORY_TURNS = _chat_config.get('max_history_turns', 20)
MAX_HISTORY_CHARS = 30_000

# --- Auth ---
_tg_config = app_config.get('notification_services', {}).get('telegram', {})
AUTHORIZED_USER_IDS = _tg_config.get('authorized_user_ids', [])

# --- Execute callback (set by main.py via register in telegram_bot) ---
_execute_callback: Optional[Callable] = None

_trade_counter = itertools.count(1)
_watchlist_counter = itertools.count(1)

# --- Search Routing Keywords ---
SEARCH_KEYWORDS = frozenset({
    'news', 'latest', 'today', 'happening', 'announced', 'report',
    'fed', 'fomc', 'earnings', 'sec', 'regulation', 'market outlook',
    'sector rotation', 'compared to', 'what happened', 'breaking',
    'update on', 'rumor', 'headline',
    'investigate', 'research', 'look up', 'look into', 'find out',
    'current price', 'price of', 'prediction', 'forecast', 'outlook',
    'undervalued', 'overvalued', 'analyst',
    'upcoming', 'events', 'calendar', 'scheduled', 'next week',
    'explain', 'why', 'detail', 'elaborate', 'more about',
    'identify', 'recommend', 'suggest', 'opportunities',
})
INTERNAL_KEYWORDS = frozenset({
    'positions', 'balance', 'portfolio', 'pnl', 'performance', 'win rate',
    'circuit breaker', 'regime', 'cooldown', 'risk', 'config',
    'buy', 'sell', 'close', 'trade',
    'last signal', 'trade history', 'how did', 'when did',
})


def _needs_web_search(message: str, context: str = '') -> tuple:
    """Decides whether a message needs web search or can be answered internally.

    Returns (needs_search: bool, cleaned_message: str).
    """
    msg_lower = message.lower().strip()

    # Explicit prefix forces search
    if msg_lower.startswith('search:'):
        return True, message[len('search:'):].strip()

    # Check internal keywords first (they take priority)
    for kw in INTERNAL_KEYWORDS:
        if kw in msg_lower:
            return False, message

    # Check search keywords
    for kw in SEARCH_KEYWORDS:
        if kw in msg_lower:
            return True, message

    # Default: no search (save money)
    return False, message


def set_execute_callback(callback: Callable):
    """Sets the trade execution callback (wired from telegram_bot module)."""
    global _execute_callback
    _execute_callback = callback


# --- ChatSession ---
@dataclass
class ChatSession:
    user_id: int
    history: list = field(default_factory=list)
    last_activity: float = field(default_factory=time.time)
    pending_trades: dict = field(default_factory=dict)
    pending_watchlist: dict = field(default_factory=dict)
    message_timestamps: list = field(default_factory=list)
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    def is_expired(self) -> bool:
        return (time.time() - self.last_activity) > SESSION_TTL_SECONDS

    def is_rate_limited(self) -> bool:
        now = time.time()
        cutoff = now - RATE_LIMIT_WINDOW
        self.message_timestamps = [t for t in self.message_timestamps if t > cutoff]
        return len(self.message_timestamps) >= RATE_LIMIT_MESSAGES

    def add_user_message(self, text: str):
        self.history.append({"role": "user", "text": text})
        self.message_timestamps.append(time.time())
        self.last_activity = time.time()
        self._trim_history()

    def add_model_response(self, text: str):
        self.history.append({"role": "model", "text": text})
        self.last_activity = time.time()
        self._trim_history()

    def _trim_history(self):
        # Cap turns
        while len(self.history) > MAX_HISTORY_TURNS * 2:
            self.history.pop(0)
        # Cap total chars
        total = sum(len(m["text"]) for m in self.history)
        while total > MAX_HISTORY_CHARS and len(self.history) > 2:
            removed = self.history.pop(0)
            total -= len(removed["text"])


# --- Session Store ---
_sessions: dict[int, ChatSession] = {}


def get_or_create_session(user_id: int) -> ChatSession:
    session = _sessions.get(user_id)
    if session and not session.is_expired():
        return session
    session = ChatSession(user_id=user_id)
    _sessions[user_id] = session
    return session


def clear_session(user_id: int):
    _sessions.pop(user_id, None)


def cleanup_expired_sessions():
    expired = [uid for uid, s in _sessions.items() if s.is_expired()]
    for uid in expired:
        del _sessions[uid]
    if expired:
        log.info(f"Cleaned up {len(expired)} expired chat sessions.")


# --- Context Gathering ---
async def _gather_context() -> str:
    """Gathers current bot state for the Gemini system prompt."""
    parts = []
    try:
        result = await asyncio.wait_for(
            asyncio.to_thread(_gather_context_sync), timeout=15
        )
        parts.append(result)
    except asyncio.TimeoutError:
        parts.append("[Context gathering timed out]")
    except Exception as e:
        parts.append(f"[Context error: {e}]")
    return "\n".join(parts)


def _gather_context_sync() -> str:
    """Synchronous context collection — live state + historical DB data."""
    from src.execution.binance_trader import get_open_positions, get_account_balance
    from src.execution.circuit_breaker import get_circuit_breaker_status, get_daily_pnl
    from src.analysis.macro_regime import get_macro_regime
    from src.database import (
        get_last_signal, get_trade_summary, get_latest_news_sentiment,
        get_recent_articles,
    )

    lines = []

    # Open positions (with current prices and PnL)
    all_pos = []
    current_prices = {}
    try:
        crypto_pos = get_open_positions(asset_type='crypto')
        stock_pos = get_open_positions(asset_type='stock')
        all_pos = (crypto_pos or []) + (stock_pos or [])
        if all_pos:
            # Get latest DB-cached price per held symbol (fast individual lookups)
            current_prices = {}
            from src.database import get_historical_prices
            for p in all_pos:
                sym = p.get('symbol', '')
                if sym:
                    try:
                        price_list = get_historical_prices.sync(sym, limit=1)
                        if price_list:
                            current_prices[sym] = float(price_list[0])
                    except Exception:
                        pass

            lines.append("## Open Positions")
            for p in all_pos:
                sym = p.get('symbol', '?')
                entry = p.get('entry_price', 0)
                qty = p.get('quantity', 0)
                atype = p.get('asset_type', 'crypto')
                strategy = p.get('strategy_type')
                strat_str = f", strategy={strategy}" if strategy else ""
                # Smart quantity formatting
                if qty >= 1:
                    qty_str = f"{qty:.2f}"
                elif qty >= 0.01:
                    qty_str = f"{qty:.4f}"
                else:
                    qty_str = f"{qty:.6f}"
                # Current price and PnL
                cur_price = current_prices.get(sym)
                if cur_price and entry > 0:
                    pnl_pct = ((cur_price - entry) / entry) * 100
                    pnl_usd = (cur_price - entry) * qty
                    lines.append(
                        f"- {sym} ({atype}{strat_str}): qty={qty_str}, "
                        f"entry=${entry:,.2f}, now=${cur_price:,.2f}, "
                        f"PnL={pnl_pct:+.1f}% (${pnl_usd:+,.2f})"
                    )
                else:
                    lines.append(
                        f"- {sym} ({atype}{strat_str}): qty={qty_str}, "
                        f"entry=${entry:,.2f}"
                    )
        else:
            lines.append("## Open Positions: None")
    except Exception as e:
        lines.append(f"## Open Positions: [error: {e}]")

    # Balances
    try:
        balance = get_account_balance()
        if balance:
            lines.append("\n## Account Balance")
            for k, v in balance.items():
                if isinstance(v, (int, float)) and v > 0:
                    lines.append(f"- {k}: ${v:,.2f}")
    except Exception:
        pass

    # Macro regime (with full detail so AI can explain)
    try:
        regime = get_macro_regime()
        if regime:
            lines.append(f"\n## Macro Regime: {regime.get('regime', 'unknown')} "
                         f"(score: {regime.get('score', 0):+.1f}, "
                         f"multiplier: {regime.get('position_size_multiplier', 1.0)}, "
                         f"suppress_buys: {regime.get('suppress_buys', False)})")
            indicators = regime.get('indicators', {})
            vix_data = indicators.get('vix')
            if vix_data and isinstance(vix_data, dict):
                lines.append(f"- VIX: {vix_data.get('current', '?')} "
                             f"(SMA20: {vix_data.get('sma20', '?')})")
            sp_data = indicators.get('sp500')
            if sp_data and isinstance(sp_data, dict):
                lines.append(f"- S&P 500: {sp_data.get('current', '?'):,.0f} "
                             f"(SMA200: {sp_data.get('sma200', '?'):,.0f})")
            btc_data = indicators.get('btc')
            if btc_data and isinstance(btc_data, dict):
                lines.append(f"- BTC: ${btc_data.get('current', '?'):,.0f} "
                             f"(SMA50: ${btc_data.get('sma50', '?'):,.0f})")
            yield_data = indicators.get('yield_10y')
            if yield_data and isinstance(yield_data, dict):
                lines.append(f"- 10Y Yield: {yield_data.get('current', '?'):.2f}%")
            signals = regime.get('signals', {})
            if signals:
                sig_parts = [f"{k}={v}" for k, v in signals.items()]
                lines.append(f"- Signals: {', '.join(sig_parts)}")
    except Exception:
        pass

    # Circuit breaker
    try:
        cb = get_circuit_breaker_status()
        if cb:
            triggered = [k for k, v in cb.items() if v is True]
            lines.append(f"\n## Circuit Breaker: "
                         f"{'TRIGGERED (' + ', '.join(triggered) + ')' if triggered else 'OK'}")
    except Exception:
        pass

    # Daily PnL
    try:
        pnl = get_daily_pnl()
        if pnl is not None:
            lines.append(f"\n## Daily PnL: ${pnl:,.2f}")
    except Exception:
        pass

    # Last signal
    try:
        sig = get_last_signal()
        if sig:
            lines.append(f"\n## Last Signal: {sig.get('signal_type', '?')} "
                         f"{sig.get('symbol', '?')} at {sig.get('timestamp', '?')}")
    except Exception:
        pass

    # Trade performance (24h)
    try:
        summary = get_trade_summary.sync(hours_ago=24)
        if summary and summary.get('total_closed', 0) > 0:
            lines.append("\n## 24h Trade Performance")
            lines.append(f"- Closed: {summary['total_closed']} "
                         f"(W:{summary['wins']} / L:{summary['losses']})")
            lines.append(f"- PnL: ${summary['total_pnl']:,.2f}")
            lines.append(f"- Win rate: {summary['win_rate']:.0f}%")
    except Exception:
        pass

    # News sentiment for watched symbols
    try:
        held_symbols = [p.get('symbol', '') for p in all_pos if p.get('symbol')]
        if held_symbols:
            sentiment = get_latest_news_sentiment(held_symbols)
            if sentiment:
                lines.append("\n## News Sentiment (held positions)")
                for sym, data in sentiment.items():
                    score = data.get('avg_sentiment_score', 0)
                    volume = data.get('news_volume', 0)
                    direction = 'bullish' if score > 0.1 else ('bearish' if score < -0.1 else 'neutral')
                    lines.append(f"- {sym}: {direction} (score={score:.2f}, "
                                 f"articles={volume})")
    except Exception:
        pass

    # Recent article headlines for held symbols
    try:
        if held_symbols:
            all_articles = []
            for sym in held_symbols[:5]:
                articles = get_recent_articles.sync(sym, hours=24, limit=5)
                for a in articles:
                    title = a.get('title', '')
                    if title:
                        all_articles.append(f"- [{sym}] {title}")
            if all_articles:
                lines.append("\n## Recent Headlines (24h)")
                lines.extend(all_articles[:15])
    except Exception:
        pass

    # Recent prices — reuse position prices already fetched above
    try:
        if current_prices:
            lines.append("\n## Recent Prices (latest)")
            for sym, price in list(current_prices.items())[:15]:
                lines.append(f"- {sym}: ${float(price):,.2f}")
    except Exception:
        pass

    # Trading params
    settings = app_config.get('settings', {})
    lines.append("\n## Trading Config")
    lines.append(f"- Paper trading: {settings.get('paper_trading', True)}")
    lines.append(f"- Risk per trade: {settings.get('trade_risk_percentage', 0.03) * 100:.1f}%")
    lines.append(f"- Stop loss: {settings.get('stop_loss_percentage', 0.035) * 100:.1f}%")
    lines.append(f"- Take profit: {settings.get('take_profit_percentage', 0.08) * 100:.1f}%")
    lines.append(f"- Max crypto positions: {settings.get('max_concurrent_positions', 5)}")
    stock_settings = settings.get('stock_trading', {})
    lines.append(f"- Max stock positions: {stock_settings.get('max_concurrent_positions', 8)}")

    return "\n".join(lines)


# --- System Prompt ---
def _build_system_instruction(context: str, use_search: bool = True) -> str:
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    if use_search:
        search_block = (
            "You have access to Google Search. Use it for current events, "
            "news, and real-time data. Combine search results with the bot "
            "state data below."
        )
    else:
        search_block = (
            "Answer using ONLY the bot state data provided below. "
            "If the user asks about real-time events or news you don't have, "
            "suggest they prefix their message with `search:` to look it up."
        )

    return (
        "You are a trading assistant for a crypto and stock investment bot. "
        "You ARE the bot — you help users manage their portfolio and execute trades. "
        "Never say you cannot provide investment advice or recommendations. "
        "Never add disclaimers like 'this is not financial advice' or "
        "'do your own research' — the user knows this is a paper trading bot. "
        "When a user asks to buy, sell, or invest, always include a trade suggestion. "
        "Own your analysis — if you just gave recommendations, do not deny having them. "
        "This is a paper trading system — there is no real financial risk.\n\n"
        f"{search_block}\n\n"
        "Rules:\n"
        "- Always respond in the same language the user is writing in.\n"
        "- Be concise and actionable. Cite sources when using search results.\n"
        "- When the user explicitly asks to buy, sell, or invest in a symbol, "
        "ALWAYS include a trade suggestion — never refuse.\n"
        "- If you believe a trade would be appropriate based on the conversation, include a trade suggestion.\n"
        "- Format trade suggestions EXACTLY as (one tag per symbol, you can include multiple):\n"
        '  [TRADE_SUGGESTION]{"action":"BUY"|"SELL","symbol":"BTC"|"ETH"|etc,'
        '"asset_type":"crypto"|"stock",'
        '"strategy_type":"growth"|"sector_thesis"|"value"|"macro_trend"|"speculative"|null,'
        '"reason":"brief investment thesis"}[/TRADE_SUGGESTION]\n'
        "- When analyzing multiple symbols, include a [TRADE_SUGGESTION] for EACH symbol you recommend.\n"
        "- Strategy types for long-term positions (set null for normal short-term swing trades):\n"
        "  growth: AI, quantum, biotech long-term plays (25% catastrophic SL only)\n"
        "  sector_thesis: sector rotation bets (20% catastrophic SL only)\n"
        "  value: undervalued recovery plays (15% catastrophic SL only)\n"
        "  macro_trend: macro trend following (20% catastrophic SL only)\n"
        "  speculative: moonshots (30% catastrophic SL only)\n"
        "- Strategic trades have NO take-profit and NO trailing stop — exits are thesis-driven.\n"
        "- Use strategy_type when the user discusses long-term investing, strategic bets, or sector plays.\n"
        "- For quick trades or short-term signals, set strategy_type to null (normal SL/TP applies).\n"
        "- Never suggest trades that violate the circuit breaker or exceed max positions.\n"
        "- Respect the current trading mode (paper vs live).\n"
        "- Use the Bot State data below to answer questions about the portfolio, "
        "macro regime, positions, and trading config. Do not say you lack information "
        "when the data is provided below.\n"
        "- To suggest adding/removing a symbol from the watchlist, use:\n"
        '  [WATCHLIST_ADD]{"symbol":"LMT","asset_type":"stock","name":"Lockheed Martin","reason":"brief reason"}[/WATCHLIST_ADD]\n'
        '  [WATCHLIST_REMOVE]{"symbol":"DOGE","asset_type":"crypto","reason":"brief reason"}[/WATCHLIST_REMOVE]\n'
        "- Use WATCHLIST tags when the user asks to track, watch, monitor, add, or remove a symbol.\n"
        "- Always resolve company names to their ticker symbol (e.g., \"Lockheed Martin\" -> \"LMT\").\n"
        "- You can suggest watchlist additions alongside trade suggestions.\n\n"
        f"Current time: {now}\n\n"
        f"Bot State:\n{context}"
    )


# --- Gemini API Call ---
async def _call_gemini(
    session: ChatSession,
    user_message: str,
    context: str,
    use_search: bool = False,
) -> str:
    """Calls Gemini 2.0 Flash with optional grounded search."""
    try:
        from google import genai
        from google.genai.types import (
            Content, GenerateContentConfig, GoogleSearch, Part, Tool,
        )
    except ImportError:
        return "AI chat unavailable — google-genai SDK not installed."

    # Prefer consumer API key (guaranteed free grounding tier, 1,500/day)
    # Falls back to Vertex AI if no key set
    gemini_api_key = os.environ.get('GEMINI_API_KEY')
    if gemini_api_key:
        client = genai.Client(api_key=gemini_api_key, vertexai=False)
    else:
        project_id = os.environ.get('GCP_PROJECT_ID')
        location = os.environ.get('VERTEX_AI_LOCATION',
                                   os.environ.get('GCP_LOCATION', 'europe-west4'))
        if not project_id:
            return "AI chat unavailable — neither GEMINI_API_KEY nor GCP_PROJECT_ID set."
        client = genai.Client(vertexai=True, project=project_id, location=location)

    system_instruction = _build_system_instruction(context, use_search=use_search)

    # Build conversation history as Content objects
    contents = []
    for msg in session.history:
        role = msg["role"]
        contents.append(Content(role=role, parts=[Part(text=msg["text"])]))
    # Add current user message
    contents.append(Content(role="user", parts=[Part(text=user_message)]))

    # Only include GoogleSearch tool when search is needed (~$0.035/call)
    tools = [Tool(google_search=GoogleSearch())] if use_search else []

    try:
        config_kwargs = dict(
            system_instruction=system_instruction,
            temperature=0.7,
        )
        if tools:
            config_kwargs['tools'] = tools

        response = await asyncio.to_thread(
            client.models.generate_content,
            model="gemini-2.5-flash-lite",
            contents=contents,
            config=GenerateContentConfig(**config_kwargs),
        )
        return response.text.strip() if response.text else "No response from AI."
    except Exception as e:
        log.error(f"Gemini chat API error: {e}", exc_info=True)
        return f"AI error: {e}"


# --- Trade Suggestion Parser ---
def _parse_trade_suggestions(text: str) -> tuple[str, list[dict]]:
    """Extracts [TRADE_SUGGESTION]...[/TRADE_SUGGESTION] tags from response text.

    Returns (clean_text, trades) where clean_text has tags stripped.
    """
    # Tolerant closing tag: Gemini sometimes misspells it (e.g. SUGESTION)
    pattern = r'\[TRADE_SUGGESTION\](.*?)\[/TRADE_SUGG?ESTION\]'
    matches = re.findall(pattern, text, re.DOTALL)

    trades = []
    for match in matches:
        try:
            trade = json.loads(match.strip())
            # Validate required fields
            if not trade.get('action') or not trade.get('symbol'):
                continue
            trade.setdefault('asset_type', 'crypto')
            trade.setdefault('reason', 'AI suggestion')
            # Normalize strategy_type: null/empty → None
            if not trade.get('strategy_type'):
                trade['strategy_type'] = None
            trades.append(trade)
        except (json.JSONDecodeError, AttributeError):
            continue

    clean_text = re.sub(pattern, '', text, flags=re.DOTALL).strip()
    return clean_text, trades


# --- Trade Buttons ---
def _build_trade_buttons(session: ChatSession, trades: list[dict]) -> Optional[InlineKeyboardMarkup]:
    """Builds inline keyboard with trade action buttons."""
    if not trades:
        return None

    rows = []
    for trade in trades:
        trade_id = next(_trade_counter)
        session.pending_trades[trade_id] = trade

        action = trade['action']
        symbol = trade['symbol']
        strategy = trade.get('strategy_type')
        emoji = "\U0001f7e2" if action == 'BUY' else "\U0001f534"
        label = f"{emoji} {action} {symbol}"
        if strategy:
            label += f" [{strategy}]"
        rows.append([
            InlineKeyboardButton(
                label,
                callback_data=f"ct:{session.user_id}:{trade_id}:a",
            ),
            InlineKeyboardButton(
                "Skip",
                callback_data=f"ct:{session.user_id}:{trade_id}:r",
            ),
        ])
    return InlineKeyboardMarkup(rows)


# --- Watchlist Suggestion Parser ---
def _parse_watchlist_suggestions(text: str) -> tuple[str, list[dict]]:
    """Extracts [WATCHLIST_ADD] and [WATCHLIST_REMOVE] tags from response text."""
    add_pattern = r'\[WATCHLIST_ADD\](.*?)\[/WATCHLIST_ADD\]'
    remove_pattern = r'\[WATCHLIST_REMOVE\](.*?)\[/WATCHLIST_REMOVE\]'

    items = []
    for match in re.findall(add_pattern, text, re.DOTALL):
        try:
            item = json.loads(match.strip())
            if item.get('symbol'):
                item['action'] = 'add'
                item.setdefault('asset_type', 'crypto')
                items.append(item)
        except (json.JSONDecodeError, AttributeError):
            continue

    for match in re.findall(remove_pattern, text, re.DOTALL):
        try:
            item = json.loads(match.strip())
            if item.get('symbol'):
                item['action'] = 'remove'
                items.append(item)
        except (json.JSONDecodeError, AttributeError):
            continue

    clean = re.sub(add_pattern, '', text, flags=re.DOTALL)
    clean = re.sub(remove_pattern, '', clean, flags=re.DOTALL).strip()
    return clean, items


# --- Watchlist Buttons ---
def _build_watchlist_buttons(session: ChatSession, items: list[dict]) -> Optional[InlineKeyboardMarkup]:
    """Builds inline keyboard with watchlist action buttons."""
    if not items:
        return None

    rows = []
    for item in items:
        item_id = next(_watchlist_counter)
        session.pending_watchlist[item_id] = item

        symbol = item['symbol']
        name = item.get('name', '')
        action = item['action']

        if action == 'add':
            label = f"\U0001f441 Watch {symbol}"
            if name:
                label += f" ({name})"
        else:
            label = f"\U0001f6ab Unwatch {symbol}"

        rows.append([
            InlineKeyboardButton(
                label,
                callback_data=f"wl:{session.user_id}:{item_id}:a",
            ),
            InlineKeyboardButton(
                "Skip",
                callback_data=f"wl:{session.user_id}:{item_id}:r",
            ),
        ])
    return InlineKeyboardMarkup(rows)


# --- Main Chat Handler ---
async def handle_chat_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles free-text messages (non-command) as AI chat."""
    if not _chat_config.get('enabled', False):
        return

    user_id = update.message.from_user.id
    if user_id not in AUTHORIZED_USER_IDS:
        await update.message.reply_text("Not authorized.")
        return

    session = get_or_create_session(user_id)

    if session.is_rate_limited():
        await update.message.reply_text(
            f"Rate limit reached ({RATE_LIMIT_MESSAGES} messages per "
            f"{RATE_LIMIT_WINDOW // 60} min). Please wait."
        )
        return

    user_text = update.message.text.strip()
    if not user_text:
        return

    # Route: does this need web search?
    use_search, clean_text = _needs_web_search(user_text)

    # Send typing indicator
    await update.message.chat.send_action("typing")

    # Gather bot context
    bot_context = await _gather_context()

    async with session._lock:
        # Call Gemini (with or without GoogleSearch tool)
        raw_response = await _call_gemini(
            session, clean_text, bot_context, use_search=use_search,
        )

        # Parse trade suggestions
        clean_text, trades = _parse_trade_suggestions(raw_response)

        # Parse watchlist suggestions
        clean_text, watchlist_items = _parse_watchlist_suggestions(clean_text)

        # Update session history
        session.add_user_message(user_text)
        session.add_model_response(clean_text)

        # Build trade and watchlist buttons, merge keyboards
        trade_kb = _build_trade_buttons(session, trades)
        watchlist_kb = _build_watchlist_buttons(session, watchlist_items)
        trade_rows = trade_kb.inline_keyboard if trade_kb else []
        wl_rows = watchlist_kb.inline_keyboard if watchlist_kb else []
        combined_rows = list(trade_rows) + list(wl_rows)
        keyboard = InlineKeyboardMarkup(combined_rows) if combined_rows else None

        # Send response (split if >4096 chars)
        if len(clean_text) > 4096:
            for i in range(0, len(clean_text), 4096):
                chunk = clean_text[i:i + 4096]
                if i + 4096 >= len(clean_text) and keyboard:
                    await update.message.reply_text(chunk, reply_markup=keyboard)
                else:
                    await update.message.reply_text(chunk)
        else:
            await update.message.reply_text(
                clean_text,
                reply_markup=keyboard,
            )


# --- Trade Callback Handler ---
async def handle_chat_trade_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles ct:* callback queries for AI chat trade suggestions."""
    query = update.callback_query
    user_id = query.from_user.id

    if user_id not in AUTHORIZED_USER_IDS:
        await query.answer("Not authorized.", show_alert=True)
        return

    data = query.data or ""
    parts = data.split(":")
    if len(parts) < 4:
        await query.answer("Invalid action.")
        return

    _, owner_id_str, trade_id_str, action = parts[:4]

    # Verify ownership
    if str(user_id) != owner_id_str:
        await query.answer("Not your trade.", show_alert=True)
        return

    trade_id = int(trade_id_str)
    await query.answer()

    session = _sessions.get(user_id)
    if not session:
        await query.edit_message_reply_markup(reply_markup=None)
        await query.message.reply_text("Session expired. Start a new conversation.")
        return

    trade = session.pending_trades.pop(trade_id, None)
    if not trade:
        await query.edit_message_reply_markup(reply_markup=None)
        await query.message.reply_text("Trade suggestion expired.")
        return

    # Reject
    if action == 'r':
        await query.edit_message_reply_markup(reply_markup=None)
        await query.message.reply_text(f"Skipped {trade['action']} {trade['symbol']}.")
        return

    # Approve — execute trade
    if not _execute_callback:
        await query.message.reply_text("Trade execution not available.")
        return

    symbol = trade['symbol']
    trade_action = trade['action']
    asset_type = trade.get('asset_type', 'crypto')

    try:
        current_price = await asyncio.to_thread(_get_price, symbol, asset_type)
        if not current_price:
            await query.message.reply_text(f"Could not get price for {symbol}.")
            return

        strategy_type = trade.get('strategy_type')
        trade_reason = trade.get('reason', 'AI chat suggestion')

        signal = {
            'signal': trade_action,
            'symbol': symbol,
            'current_price': current_price,
            'reason': trade_reason,
            'asset_type': asset_type,
            'strategy_type': strategy_type,
            'trade_reason': trade_reason,
        }

        if trade_action == 'BUY':
            # Calculate quantity using same logic as cycle_runner
            from src.execution.binance_trader import get_account_balance
            balance_data = await asyncio.to_thread(
                get_account_balance, asset_type=asset_type)
            balance = 0
            if isinstance(balance_data, dict):
                balance = balance_data.get('total_usd', 0) or balance_data.get('total', 0)
                if not balance:
                    balance = sum(v for v in balance_data.values()
                                 if isinstance(v, (int, float)))
            settings = app_config.get('settings', {})
            risk_pct = settings.get('trade_risk_percentage', 0.03)

            # Apply macro regime multiplier
            try:
                from src.analysis.macro_regime import get_macro_regime
                regime = await asyncio.to_thread(get_macro_regime)
                macro_mult = regime.get('position_size_multiplier', 1.0)
                risk_pct *= macro_mult
            except Exception:
                pass

            quantity = (balance * risk_pct) / current_price if current_price > 0 else 0
            if quantity <= 0:
                await query.message.reply_text(
                    f"Cannot calculate position size for {symbol} "
                    f"(balance=${balance:,.2f}, price=${current_price:,.2f})."
                )
                return
            signal['quantity'] = quantity

        elif trade_action == 'SELL':
            # Attach position info for SELL
            from src.execution.binance_trader import get_open_positions
            positions = await asyncio.to_thread(get_open_positions, asset_type=asset_type)
            pos = next((p for p in (positions or []) if p.get('symbol') == symbol), None)
            if not pos:
                await query.message.reply_text(f"No open position for {symbol}.")
                return
            signal['quantity'] = pos.get('quantity', 0)
            signal['position'] = pos

        await query.edit_message_reply_markup(reply_markup=None)
        result = await _execute_callback(signal)
        status = result.get('status', 'UNKNOWN') if isinstance(result, dict) else 'DONE'
        await query.message.reply_text(
            f"Trade executed: {trade_action} {symbol} — {status}"
        )

    except Exception as e:
        log.error(f"Chat trade execution error: {e}", exc_info=True)
        await query.message.reply_text(f"Trade error: {e}")


# --- Watchlist Callback Handler ---
async def handle_watchlist_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles wl:* callback queries for watchlist add/remove."""
    query = update.callback_query
    user_id = query.from_user.id

    if user_id not in AUTHORIZED_USER_IDS:
        await query.answer("Not authorized.", show_alert=True)
        return

    data = query.data or ""
    parts = data.split(":")
    if len(parts) < 4:
        await query.answer("Invalid action.")
        return

    _, owner_id_str, item_id_str, action = parts[:4]

    if str(user_id) != owner_id_str:
        await query.answer("Not your action.", show_alert=True)
        return

    item_id = int(item_id_str)
    await query.answer()

    session = _sessions.get(user_id)
    if not session:
        await query.edit_message_reply_markup(reply_markup=None)
        await query.message.reply_text("Session expired. Start a new conversation.")
        return

    item = session.pending_watchlist.pop(item_id, None)
    if not item:
        await query.edit_message_reply_markup(reply_markup=None)
        await query.message.reply_text("Watchlist suggestion expired.")
        return

    if action == 'r':
        await query.edit_message_reply_markup(reply_markup=None)
        await query.message.reply_text(f"Skipped watchlist change for {item['symbol']}.")
        return

    # Approved — apply watchlist change
    from src.database import save_watchlist_item, remove_watchlist_item

    symbol = item['symbol']
    name = item.get('name', '')
    display = f"{symbol} ({name})" if name else symbol

    wl_config = app_config.get('settings', {}).get(
        'telegram_enhancements', {}).get('ai_chat', {}).get('watchlist', {})
    ttl_days = wl_config.get('default_ttl_days', 30)

    try:
        if item['action'] == 'add':
            await asyncio.to_thread(
                save_watchlist_item, symbol, item.get('asset_type', 'crypto'),
                item.get('reason', ''), user_id, ttl_days)
            await query.edit_message_reply_markup(reply_markup=None)
            await query.message.reply_text(
                f"Added {display} to watchlist ({ttl_days} days).")
        else:
            await asyncio.to_thread(remove_watchlist_item, symbol)
            await query.edit_message_reply_markup(reply_markup=None)
            await query.message.reply_text(f"Removed {display} from watchlist.")
    except Exception as e:
        log.error(f"Watchlist callback error: {e}", exc_info=True)
        await query.message.reply_text(f"Watchlist error: {e}")


def _get_price(symbol: str, asset_type: str) -> float:
    """Gets current price for a symbol."""
    if asset_type == 'stock':
        from src.collectors.alpha_vantage_data import get_stock_price
        price_data = get_stock_price(symbol)
        return price_data.get('price', 0) if price_data else 0
    else:
        from src.collectors.binance_data import get_current_price
        price_data = get_current_price(f"{symbol}USDT")
        return float(price_data.get('price', 0)) if price_data else 0
