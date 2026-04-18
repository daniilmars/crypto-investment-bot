"""Telegram /dashboard command with drill-down callbacks."""

from datetime import datetime, timezone

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from src.logger import log
from src.config import app_config
from src.notify.formatting import (
    text_sparkline, pnl_emoji, format_position_line,
    truncate_for_telegram, escape_md, format_region_label,
)
from src.execution.binance_trader import (
    get_open_positions, get_account_balance,
)
from src.execution.circuit_breaker import (
    get_circuit_breaker_status, get_daily_pnl,
)
from src.analysis.macro_regime import get_macro_regime
from src.database import get_last_signal, get_price_history_since


def _latest_price_from_db(symbol: str) -> float | None:
    """Read the most recent price for a symbol from market_prices (no HTTP)."""
    import psycopg2
    from src.database import get_db_connection, release_db_connection, _cursor
    conn = None
    try:
        conn = get_db_connection()
        if not conn:
            return None
        is_pg = isinstance(conn, psycopg2.extensions.connection)
        placeholder = "%s" if is_pg else "?"
        with _cursor(conn) as cur:
            cur.execute(
                f"SELECT price FROM market_prices WHERE symbol = {placeholder} "
                f"ORDER BY timestamp DESC LIMIT 1",
                (symbol,),
            )
            row = cur.fetchone()
        if not row:
            return None
        return float(row[0]) if not isinstance(row, dict) else float(row.get("price") or 0)
    except Exception as e:
        log.debug("DB price lookup failed for %s: %s", symbol, e)
        return None
    finally:
        if conn is not None:
            try:
                release_db_connection(conn)
            except Exception:
                pass


def _get_position_price(symbol):
    """Get a current-ish price for dashboard display.

    Reads from the market_prices table populated each cycle. Avoids per-call
    HTTPS fetches (Alpha Vantage fallback to yfinance was 10-20s each, which
    hung the /dashboard command for minutes on a 24-position portfolio).
    """
    # Try exact symbol first (stocks: "NVDA", crypto: "BTC" after strip)
    price = _latest_price_from_db(symbol)
    if price is not None:
        return price
    # Crypto stored in market_prices with USDT suffix
    price = _latest_price_from_db(f"{symbol}USDT")
    if price is not None:
        return price
    return 0


def _enrich_positions(positions: list) -> list:
    """Add current_price, pnl, pnl_pct to each position dict."""
    enriched = []
    for pos in positions:
        symbol = pos.get('symbol', '')
        entry_price = pos.get('entry_price', 0)
        current_price = _get_position_price(symbol) or entry_price
        quantity = pos.get('quantity', 0)
        pnl = (current_price - entry_price) * quantity
        pnl_pct = ((current_price - entry_price) / entry_price * 100) if entry_price > 0 else 0
        enriched.append({
            **pos,
            'current_price': current_price,
            'pnl': pnl,
            'pnl_pct': pnl_pct,
        })
    return enriched


def _get_price_sparkline(symbol: str, hours: int = 24, points: int = 10) -> str:
    """Fetch recent prices from DB and render sparkline for a symbol."""
    try:
        history = get_price_history_since(hours_ago=hours)
        # Filter for this symbol
        prices = [h.get('price', 0) for h in history
                  if h.get('symbol', '').replace('USDT', '') == symbol
                  or h.get('symbol', '') == symbol]
        if len(prices) < 2:
            return ''
        return text_sparkline(prices, width=points)
    except Exception as e:
        log.debug(f"Sparkline generation failed for {symbol}: {e}")
        return ''


def _dashboard_keyboard() -> InlineKeyboardMarkup:
    import os
    rows = [
        [
            InlineKeyboardButton("Crypto", callback_data="dash:crypto"),
            InlineKeyboardButton("Stocks", callback_data="dash:stocks"),
        ],
        [
            InlineKeyboardButton("Auto", callback_data="dash:auto"),
            InlineKeyboardButton("Regime", callback_data="dash:regime"),
        ],
    ]
    base = os.environ.get("MINIAPP_BASE_URL")
    if base:
        try:
            from telegram import WebAppInfo
            rows.append([InlineKeyboardButton(
                "📊 Open Mini App",
                web_app=WebAppInfo(url=f"{base.rstrip('/')}/miniapp/"),
            )])
        except Exception as e:  # pragma: no cover — WebAppInfo is in PTB 20+
            log.debug("WebApp button not added: %s", e)
    return InlineKeyboardMarkup(rows)


def _back_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("Back", callback_data="dash:back")],
    ])


def build_dashboard_message() -> str:
    """Assemble the main dashboard message."""
    try:
        # Portfolio totals
        crypto_balance = get_account_balance(asset_type='crypto')
        stock_balance = get_account_balance(asset_type='stock')
        crypto_total = crypto_balance.get('total_usd', 0)
        stock_total = stock_balance.get('total_usd', 0)
        portfolio_total = crypto_total + stock_total

        # Daily PnL
        daily_crypto = get_daily_pnl('crypto')
        daily_stock = get_daily_pnl('stock')
        daily_total = daily_crypto + daily_stock

        # Positions
        crypto_positions = get_open_positions(asset_type='crypto')
        stock_positions = get_open_positions(asset_type='stock')
        crypto_positions = _enrich_positions(crypto_positions)
        stock_positions = _enrich_positions(stock_positions)

        # Regime — escape _ for Markdown-legacy (RISK_ON otherwise starts italic)
        regime = get_macro_regime()
        regime_name = regime.get('regime', '?').replace('_', r'\_')
        regime_mult = regime.get('position_size_multiplier', 1.0)

        # Circuit breaker
        cb = get_circuit_breaker_status()
        cb_label = "HALT" if cb.get('in_cooldown') else "OK"

        # Top movers (combine, sort by abs pnl_pct)
        all_positions = crypto_positions + stock_positions
        top_movers = sorted(all_positions, key=lambda p: abs(p.get('pnl_pct', 0)), reverse=True)[:5]

        # Last signal
        last_signal = get_last_signal()

        # Build message
        daily_sign = '+' if daily_total >= 0 else ''
        lines = [
            "*DASHBOARD*\n",
            f"*Portfolio:* ${portfolio_total:,.0f} ({daily_sign}${daily_total:,.0f} today)",
            f"  Crypto ${crypto_total:,.0f} | Stocks ${stock_total:,.0f}\n",
            f"*Regime:* {regime_name} ({regime_mult:.1f}x) | *CB:* {cb_label}",
            f"*Positions:* {len(crypto_positions)} crypto, {len(stock_positions)} stocks\n",
        ]

        if top_movers:
            lines.append("*Top Movers:*")
            for pos in top_movers:
                symbol = pos.get('symbol', '?')
                sparkline = _get_price_sparkline(symbol)
                lines.append(f"  {format_position_line(symbol, pos['pnl_pct'], pos['current_price'], sparkline)}")
            lines.append("")

        if last_signal:
            sig_type = last_signal.get('signal_type', last_signal.get('signal', '?'))
            sig_symbol = last_signal.get('symbol', '?')
            sig_time = last_signal.get('timestamp', '')
            age_str = ''
            if sig_time:
                try:
                    if isinstance(sig_time, str):
                        sig_dt = datetime.fromisoformat(sig_time.replace('Z', '+00:00'))
                    else:
                        sig_dt = sig_time
                    if sig_dt.tzinfo is None:
                        sig_dt = sig_dt.replace(tzinfo=timezone.utc)
                    age_min = (datetime.now(timezone.utc) - sig_dt).total_seconds() / 60
                    if age_min < 60:
                        age_str = f" ({age_min:.0f}m ago)"
                    else:
                        age_str = f" ({age_min / 60:.0f}h ago)"
                except Exception as e:
                    log.debug(f"Signal age calculation failed: {e}")
            lines.append(f"*Last Signal:* {sig_type} {sig_symbol}{age_str}")

        return truncate_for_telegram('\n'.join(lines))
    except Exception as e:
        log.error(f"Error building dashboard: {e}", exc_info=True)
        return f"Error building dashboard: {e}"


def build_crypto_detail() -> str:
    """Detailed crypto positions view."""
    positions = _enrich_positions(get_open_positions(asset_type='crypto'))
    if not positions:
        return "*Crypto Positions*\n\nNo open crypto positions."

    lines = ["*Crypto Positions*\n"]
    total_pnl = 0
    for pos in sorted(positions, key=lambda p: p.get('pnl_pct', 0), reverse=True):
        symbol = pos.get('symbol', '?')
        sparkline = _get_price_sparkline(symbol)
        entry = pos.get('entry_price', 0)
        current = pos.get('current_price', 0)
        pnl_pct = pos.get('pnl_pct', 0)
        pnl = pos.get('pnl', 0)
        total_pnl += pnl
        emoji = pnl_emoji(pnl_pct)
        lines.append(
            f"{emoji} *{symbol}*  {pnl_pct:+.1f}%  ${pnl:+,.2f}\n"
            f"  Entry ${entry:,.2f} -> ${current:,.2f}  {sparkline}"
        )
    lines.append(f"\n*Total:* ${total_pnl:+,.2f}")
    return truncate_for_telegram('\n'.join(lines))


def build_stocks_detail() -> str:
    """Detailed stock positions view, grouped by region."""
    positions = _enrich_positions(get_open_positions(asset_type='stock'))
    if not positions:
        return "*Stock Positions*\n\nNo open stock positions."

    # Group by region
    regions = {'US': [], 'EU': [], 'Asia': []}
    for pos in positions:
        region = format_region_label(pos.get('symbol', ''))
        regions.setdefault(region, []).append(pos)

    lines = ["*Stock Positions*\n"]
    total_pnl = 0
    for region in ('US', 'EU', 'Asia'):
        region_positions = regions.get(region, [])
        if not region_positions:
            continue
        lines.append(f"_{region}:_")
        for pos in sorted(region_positions, key=lambda p: p.get('pnl_pct', 0), reverse=True):
            symbol = pos.get('symbol', '?')
            pnl_pct = pos.get('pnl_pct', 0)
            pnl = pos.get('pnl', 0)
            current = pos.get('current_price', 0)
            total_pnl += pnl
            emoji = pnl_emoji(pnl_pct)
            lines.append(f"  {emoji} *{escape_md(symbol)}*  {pnl_pct:+.1f}% ${current:,.2f}")
        lines.append("")
    lines.append(f"*Total:* ${total_pnl:+,.2f}")
    return truncate_for_telegram('\n'.join(lines))


def build_auto_detail() -> str:
    """Auto-bot summary view."""
    try:
        auto_cfg = app_config.get('settings', {}).get('auto_trading', {})
        if not auto_cfg.get('enabled', False):
            return "*Auto-Bot*\n\nAuto-trading is disabled."

        auto_positions = get_open_positions(trading_strategy='auto')
        auto_balance = get_account_balance(trading_strategy='auto')
        initial = auto_cfg.get('paper_trading_initial_capital', 10000.0)
        auto_total = auto_balance.get('total_usd', initial)
        auto_return = ((auto_total - initial) / initial * 100) if initial > 0 else 0

        manual_balance = get_account_balance()
        manual_initial = app_config.get('settings', {}).get('paper_trading_initial_capital', 10000.0)
        manual_total = manual_balance.get('total_usd', 0)
        manual_return = ((manual_total - manual_initial) / manual_initial * 100) if manual_initial > 0 else 0

        lines = [
            "*Auto-Bot Summary*\n",
            f"*Balance:* ${auto_total:,.2f}",
            f"*Return:* {auto_return:+.1f}%",
            f"*Positions:* {len(auto_positions)}\n",
            f"_Manual: {manual_return:+.1f}% vs Auto: {auto_return:+.1f}%_",
        ]
        return '\n'.join(lines)
    except Exception as e:
        return f"*Auto-Bot*\n\nError: {e}"


def build_regime_detail() -> str:
    """Full macro regime details view."""
    try:
        regime = get_macro_regime()
        regime_emoji = {'RISK_ON': '🟢', 'CAUTION': '🟡', 'RISK_OFF': '🔴'}
        emoji = regime_emoji.get(regime['regime'], '⚪')
        regime_label = regime['regime'].replace('_', r'\_')
        signals = regime.get('signals', {})
        indicators = regime.get('indicators', {})

        lines = [
            f"{emoji} *Macro Regime: {regime_label}*\n",
            f"*Score:* {regime.get('score', 0)}",
            f"*Multiplier:* {regime['position_size_multiplier']:.1f}x",
            f"*Suppress BUYs:* {'Yes' if regime['suppress_buys'] else 'No'}\n",
            "*Signals:*",
            f"  VIX level: {signals.get('vix_signal', '?')}",
            f"  VIX trend: {signals.get('vix_trend', '?')}",
            f"  S&P 500: {signals.get('sp500_trend', '?')}",
            f"  10Y yield: {signals.get('yield_direction', '?')}",
            f"  BTC trend: {signals.get('btc_trend', '?')}\n",
            "*Indicators:*",
        ]
        if indicators:
            for key, val in indicators.items():
                if isinstance(val, float):
                    lines.append(f"  {key}: {val:.2f}")
                else:
                    lines.append(f"  {key}: {val}")

        return truncate_for_telegram('\n'.join(lines))
    except Exception as e:
        return f"*Macro Regime*\n\nError: {e}"


# --- Telegram Handlers ---

async def dashboard_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles the /dashboard command."""
    import asyncio
    try:
        # build_dashboard_message is synchronous and does many DB queries + HTTP
        # price fetches. Run it on a thread so the asyncio event loop stays
        # responsive for other handlers and the /webhook request path.
        msg = await asyncio.to_thread(build_dashboard_message)
        await update.message.reply_text(
            msg, parse_mode='Markdown', reply_markup=_dashboard_keyboard()
        )
    except Exception as e:
        log.error(f"Error in /dashboard: {e}", exc_info=True)
        try:
            await update.message.reply_text("Error loading dashboard.")
        except Exception:
            pass


async def handle_dashboard_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles dash:* callback queries for drill-down views."""
    query = update.callback_query
    await query.answer()

    data = query.data or ""
    _, action = data.split(":", 1)

    try:
        if action == 'crypto':
            text = build_crypto_detail()
        elif action == 'stocks':
            text = build_stocks_detail()
        elif action == 'auto':
            text = build_auto_detail()
        elif action == 'regime':
            text = build_regime_detail()
        elif action == 'back':
            text = build_dashboard_message()
            await query.edit_message_text(
                text, parse_mode='Markdown', reply_markup=_dashboard_keyboard()
            )
            return
        else:
            await query.edit_message_text("Unknown dashboard view.")
            return

        await query.edit_message_text(
            text, parse_mode='Markdown', reply_markup=_back_keyboard()
        )
    except Exception as e:
        log.error(f"Error in dashboard callback '{action}': {e}", exc_info=True)
        try:
            await query.edit_message_text(f"Error loading {action} view.")
        except Exception as e:
            log.debug(f"Dashboard callback error edit failed: {e}")
