"""4-hour periodic summary + enhanced trade alerts.

Replaces: morning_briefing, portfolio_digest, daily_recap, sector_review_digest,
signal confirmation messages, and per-trade silent execution.

Two message types:
1. send_periodic_summary() — consolidated 4h status update
2. send_trade_alert() — detailed alert when a trade executes or closes
"""

from datetime import datetime, timezone, timedelta

from telegram import Bot

from src.config import app_config
from src.logger import log
from src.notify.formatting import escape_md

_error_count: int = 0


def increment_error_count():
    """Called by error handler to track critical errors between summaries."""
    global _error_count
    _error_count += 1


async def send_periodic_summary():
    """Build and send the consolidated 4-hour summary."""
    global _error_count

    tg_cfg = app_config.get('notification_services', {}).get('telegram', {})
    token = tg_cfg.get('token')
    chat_id = tg_cfg.get('chat_id')
    if not token or not chat_id:
        return

    now = datetime.now(timezone.utc)
    lines = [f"*4H Summary* ({now.strftime('%H:%M UTC')})"]
    lines.append("")

    # --- Macro regime ---
    try:
        from src.analysis.macro_regime import get_macro_regime
        regime = get_macro_regime()
        r_name = regime.get('regime', '?')
        r_score = regime.get('score', 0)
        vix = regime.get('indicators', {}).get('vix', {}).get('current', '?')
        lines.append(f"*Macro:* {r_name} ({r_score:+.1f}) | VIX {vix}")
    except Exception:
        lines.append("*Macro:* unavailable")

    # --- Per-strategy table ---
    try:
        from src.execution.binance_trader import get_open_positions
        from src.orchestration import bot_state

        strategies = ['auto', 'momentum', 'conservative', 'longterm']
        lines.append("")
        lines.append("```")
        lines.append(f"{'Strat':<12} {'PnL':>8} {'Open':>4} {'Unreal':>8} {'Strk':>4}")

        for strat in strategies:
            # Realized PnL
            try:
                from src.database import get_db_connection, release_db_connection, _cursor
                import psycopg2
                conn = get_db_connection()
                is_pg = isinstance(conn, psycopg2.extensions.connection)
                ph = "%s" if is_pg else "?"
                with _cursor(conn) as cur:
                    cur.execute(
                        f"SELECT COALESCE(SUM(pnl), 0) FROM trades "
                        f"WHERE status='CLOSED' AND trading_strategy={ph}",
                        (strat,))
                    realized = cur.fetchone()[0] or 0
                release_db_connection(conn)
            except Exception:
                realized = 0

            # Open positions + unrealized
            try:
                positions = get_open_positions.sync(
                    asset_type='all', trading_strategy=strat)
                open_count = len(positions)
            except Exception:
                positions = []
                open_count = 0

            unrealized = 0.0
            try:
                from src.database import get_db_connection as _gc2, release_db_connection as _rc2
                conn2 = _gc2()
                with _cursor(conn2) as cur2:
                    for p in positions:
                        cur2.execute(
                            f"SELECT price FROM market_prices WHERE symbol={ph} "
                            f"ORDER BY id DESC LIMIT 1",
                            (p['symbol'],))
                        row = cur2.fetchone()
                        if row:
                            price = row[0] if isinstance(row, (list, tuple)) else row['price']
                            unrealized += (price - p['entry_price']) * p['quantity']
                _rc2(conn2)
            except Exception:
                pass

            # Streak
            streak = bot_state.strategy_get_streak_state(strat)
            cw = streak.get('consecutive_wins', 0)
            streak_str = f"{cw}W" if cw > 0 else "-"

            r_str = f"{'+' if realized >= 0 else ''}${realized:.0f}"
            u_str = f"{'+' if unrealized >= 0 else ''}${unrealized:.0f}"
            lines.append(
                f"{strat:<12} {r_str:>8} {open_count:>4} {u_str:>8} {streak_str:>4}")

        lines.append("```")
    except Exception as e:
        lines.append(f"_Strategy data unavailable: {e}_")

    # --- Trades since last summary (4h) ---
    try:
        import psycopg2
        conn = get_db_connection()
        is_pg = isinstance(conn, psycopg2.extensions.connection)
        ph = "%s" if is_pg else "?"
        cutoff = (now - timedelta(hours=4)).isoformat()

        with _cursor(conn) as cur:
            if is_pg:
                cur.execute(
                    "SELECT symbol, trading_strategy, entry_price, quantity "
                    "FROM trades WHERE status='OPEN' AND entry_timestamp >= %s "
                    "ORDER BY entry_timestamp", (cutoff,))
            else:
                cur.execute(
                    "SELECT symbol, trading_strategy, entry_price, quantity "
                    "FROM trades WHERE status='OPEN' AND entry_timestamp >= ? "
                    "ORDER BY entry_timestamp", (cutoff,))
            opened = [dict(r) if hasattr(r, 'keys') else
                      {'symbol': r[0], 'trading_strategy': r[1],
                       'entry_price': r[2], 'quantity': r[3]}
                      for r in cur.fetchall()]

            if is_pg:
                cur.execute(
                    "SELECT symbol, trading_strategy, pnl, exit_reason "
                    "FROM trades WHERE status='CLOSED' AND exit_timestamp >= %s "
                    "ORDER BY exit_timestamp", (cutoff,))
            else:
                cur.execute(
                    "SELECT symbol, trading_strategy, pnl, exit_reason "
                    "FROM trades WHERE status='CLOSED' AND exit_timestamp >= ? "
                    "ORDER BY exit_timestamp", (cutoff,))
            closed = [dict(r) if hasattr(r, 'keys') else
                      {'symbol': r[0], 'trading_strategy': r[1],
                       'pnl': r[2], 'exit_reason': r[3]}
                      for r in cur.fetchall()]

        release_db_connection(conn)

        lines.append("")
        if opened or closed:
            lines.append("*Since last (4h):*")
            for t in opened:
                cost = t['entry_price'] * t['quantity']
                lines.append(f"  BUY {t['symbol']} ({t['trading_strategy']}) ${cost:.0f}")
            for t in closed:
                p = t['pnl'] or 0
                tag = "+" if p >= 0 else ""
                lines.append(
                    f"  SELL {t['symbol']} {tag}${p:.2f} ({t['exit_reason']})")
        else:
            lines.append("_No trades in last 4h_")
    except Exception:
        pass

    # --- Top/worst open positions ---
    try:
        all_positions = get_open_positions.sync(asset_type='all', trading_strategy='all')
        if all_positions:
            scored = []
            conn3 = get_db_connection()
            with _cursor(conn3) as cur3:
                for p in all_positions:
                    cur3.execute(
                        f"SELECT price FROM market_prices WHERE symbol={ph} "
                        f"ORDER BY id DESC LIMIT 1", (p['symbol'],))
                    row = cur3.fetchone()
                    if row:
                        price = row[0] if isinstance(row, (list, tuple)) else row['price']
                        pnl_pct = (price - p['entry_price']) / p['entry_price'] * 100
                        pnl_d = (price - p['entry_price']) * p['quantity']
                        scored.append((p['symbol'], pnl_pct, pnl_d))
            release_db_connection(conn3)

            if scored:
                scored.sort(key=lambda x: x[1], reverse=True)
                top3 = scored[:3]
                worst3 = sorted(scored, key=lambda x: x[1])[:3]
                lines.append("")
                lines.append("*Best:* " + ", ".join(
                    f"{s} {p:+.1f}%" for s, p, _ in top3))
                lines.append("*Worst:* " + ", ".join(
                    f"{s} {p:+.1f}%" for s, p, _ in worst3))
    except Exception:
        pass

    # --- Errors ---
    if _error_count > 0:
        lines.append(f"\n_{_error_count} error(s) since last summary_")
        _error_count = 0

    text = "\n".join(lines)
    try:
        bot = Bot(token=token)
        await bot.send_message(chat_id=chat_id, text=text,
                               parse_mode='Markdown')
        log.info("4h periodic summary sent.")
    except Exception as e:
        log.warning(f"Failed to send periodic summary: {e}")


async def send_trade_alert(
    *,
    action: str,
    symbol: str,
    trading_strategy: str,
    entry_price: float = 0,
    exit_price: float = 0,
    quantity: float = 0,
    pnl: float = 0,
    pnl_pct: float = 0,
    hold_duration: str = "",
    exit_reason: str = "",
    signal_strength: float = 0,
    gemini_direction: str = "",
    gemini_confidence: float = 0,
    catalyst_freshness: str = "",
    catalyst_type: str = "",
    key_headline: str = "",
    reason: str = "",
    macro_multiplier: float = 1.0,
    streak_multiplier: float = 1.0,
    sma_override: bool = False,
):
    """Send a concise trade execution alert with reasoning."""
    tg_cfg = app_config.get('notification_services', {}).get('telegram', {})
    token = tg_cfg.get('token')
    chat_id = tg_cfg.get('chat_id')
    if not token or not chat_id:
        return

    if action == "BUY":
        cost = entry_price * quantity
        lines = [f"*BUY {symbol}* ({trading_strategy})"]
        lines.append(f"${entry_price:,.2f} x {quantity:.4f} (${cost:.0f})")

        if gemini_direction and gemini_confidence:
            lines.append(
                f"Gemini: {gemini_direction} {gemini_confidence:.2f} "
                f"({catalyst_freshness or 'N/A'})")
        if key_headline:
            lines.append(f"_{escape_md(key_headline[:100])}_")
        if signal_strength:
            mods = []
            if macro_multiplier != 1.0:
                mods.append(f"macro {macro_multiplier:.1f}x")
            if streak_multiplier != 1.0:
                mods.append(f"streak {streak_multiplier:.1f}x")
            if sma_override:
                mods.append("SMA override")
            mod_str = f" | {', '.join(mods)}" if mods else ""
            lines.append(f"Strength: {signal_strength:.2f}{mod_str}")

    elif action == "SELL":
        lines = [f"*SELL {symbol}* ({trading_strategy})"]
        if entry_price and exit_price:
            lines.append(
                f"${entry_price:,.2f} -> ${exit_price:,.2f} | "
                f"{pnl_pct:+.1f}% | {'+' if pnl >= 0 else ''}${pnl:.2f}")
        if hold_duration:
            lines.append(f"Hold: {hold_duration} | {exit_reason}")
        elif exit_reason:
            lines.append(f"Exit: {exit_reason}")

    else:
        lines = [f"*{action} {symbol}* ({trading_strategy})"]
        if reason:
            lines.append(escape_md(reason[:150]))

    text = "\n".join(lines)
    try:
        bot = Bot(token=token)
        await bot.send_message(chat_id=chat_id, text=text,
                               parse_mode='Markdown')
    except Exception as e:
        log.warning(f"Failed to send trade alert: {e}")
