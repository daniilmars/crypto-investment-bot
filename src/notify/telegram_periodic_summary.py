"""4-hour periodic summary + enhanced trade alerts.

Replaces: morning_briefing, portfolio_digest, daily_recap, sector_review_digest.
Uses plain text (no Markdown) to avoid parse errors.
"""

from datetime import datetime, timezone, timedelta

from telegram import Bot

from src.config import app_config
from src.database import get_db_connection, release_db_connection, _cursor
from src.logger import log

_error_count: int = 0


def increment_error_count():
    """Called by error handler to track critical errors between summaries."""
    global _error_count
    _error_count += 1


def _ph():
    """Placeholder for SQL queries."""
    try:
        import psycopg2
        conn = get_db_connection()
        is_pg = isinstance(conn, psycopg2.extensions.connection)
        release_db_connection(conn)
        return "%s" if is_pg else "?"
    except Exception:
        return "?"


async def send_periodic_summary():
    """Build and send the consolidated 4-hour summary (plain text)."""
    global _error_count

    tg_cfg = app_config.get('notification_services', {}).get('telegram', {})
    token = tg_cfg.get('token')
    chat_id = tg_cfg.get('chat_id')
    if not token or not chat_id:
        return

    now = datetime.now(timezone.utc)
    ph = "?"  # SQLite default
    lines = [f"== 4H Summary ({now.strftime('%H:%M UTC')}) =="]
    lines.append("")

    # --- Macro regime ---
    try:
        from src.analysis.macro_regime import get_macro_regime
        regime = get_macro_regime()
        r_name = regime.get('regime', '?')
        r_score = regime.get('score', 0)
        vix = regime.get('indicators', {}).get('vix', {}).get('current', '?')
        lines.append(f"Regime: {r_name} ({r_score:+.1f}) | VIX {vix}")
    except Exception:
        lines.append("Regime: unavailable")
    lines.append("")

    # --- Per-strategy table ---
    try:
        from src.execution.binance_trader import get_open_positions
        from src.orchestration import bot_state
        import psycopg2

        strategies = ['auto', 'momentum', 'conservative', 'longterm']
        header = f"{'Strategy':<13}{'PnL':>7}{'Open':>5}{'Unreal':>8}{'Strk':>5}"
        lines.append(header)
        lines.append("-" * len(header))

        for strat in strategies:
            realized = 0
            try:
                conn = get_db_connection()
                is_pg = isinstance(conn, psycopg2.extensions.connection)
                ph = "%s" if is_pg else "?"
                with _cursor(conn) as cur:
                    cur.execute(
                        f"SELECT COALESCE(SUM(pnl), 0) FROM trades "
                        f"WHERE status='CLOSED' AND trading_strategy={ph}",
                        (strat,))
                    row = cur.fetchone()
                    realized = (row[0] if isinstance(row, (list, tuple))
                                else row['coalesce']) or 0
                release_db_connection(conn)
            except Exception:
                pass

            positions = []
            try:
                positions = get_open_positions.sync(
                    asset_type='all', trading_strategy=strat)
            except Exception:
                pass
            open_count = len(positions)

            unrealized = 0.0
            try:
                conn2 = get_db_connection()
                with _cursor(conn2) as cur2:
                    for p in positions:
                        cur2.execute(
                            f"SELECT price FROM market_prices WHERE symbol={ph} "
                            f"ORDER BY id DESC LIMIT 1",
                            (p['symbol'],))
                        row = cur2.fetchone()
                        if row:
                            price = (row[0] if isinstance(row, (list, tuple))
                                     else row['price'])
                            unrealized += (price - p['entry_price']) * p['quantity']
                release_db_connection(conn2)
            except Exception:
                pass

            streak = bot_state.strategy_get_streak_state(strat)
            cw = streak.get('consecutive_wins', 0)
            streak_str = f"{cw}W" if cw > 0 else "-"

            r_str = f"${realized:+.0f}" if realized != 0 else "$0"
            u_str = f"${unrealized:+.0f}" if unrealized != 0 else "$0"
            lines.append(
                f"{strat:<13}{r_str:>7}{open_count:>5}{u_str:>8}{streak_str:>5}")

    except Exception as e:
        lines.append(f"(strategy data unavailable: {e})")

    lines.append("")

    # --- Trades since last summary (4h) ---
    try:
        cutoff = (now - timedelta(hours=4)).isoformat()
        conn3 = get_db_connection()
        with _cursor(conn3) as cur3:
            cur3.execute(
                f"SELECT symbol, trading_strategy, entry_price, quantity "
                f"FROM trades WHERE status='OPEN' AND entry_timestamp >= {ph} "
                f"ORDER BY entry_timestamp", (cutoff,))
            opened = cur3.fetchall()

            cur3.execute(
                f"SELECT symbol, trading_strategy, pnl, exit_reason "
                f"FROM trades WHERE status='CLOSED' AND exit_timestamp >= {ph} "
                f"ORDER BY exit_timestamp", (cutoff,))
            closed = cur3.fetchall()
        release_db_connection(conn3)

        if opened or closed:
            lines.append("Since last 4h:")
            for r in opened:
                sym = r[0] if isinstance(r, (list, tuple)) else r['symbol']
                strat = r[1] if isinstance(r, (list, tuple)) else r['trading_strategy']
                ep = r[2] if isinstance(r, (list, tuple)) else r['entry_price']
                qty = r[3] if isinstance(r, (list, tuple)) else r['quantity']
                lines.append(f"  BUY {sym} ({strat}) ${ep * qty:.0f}")
            for r in closed:
                sym = r[0] if isinstance(r, (list, tuple)) else r['symbol']
                strat = r[1] if isinstance(r, (list, tuple)) else r['trading_strategy']
                pnl = (r[2] if isinstance(r, (list, tuple)) else r['pnl']) or 0
                reason = r[3] if isinstance(r, (list, tuple)) else r['exit_reason']
                lines.append(f"  SELL {sym} ${pnl:+.2f} ({reason})")
        else:
            lines.append("No trades in last 4h")
    except Exception:
        pass

    # --- Top/worst open positions ---
    try:
        from src.execution.binance_trader import get_open_positions
        all_pos = get_open_positions.sync(asset_type='all', trading_strategy='all')
        if all_pos:
            scored = []
            conn4 = get_db_connection()
            with _cursor(conn4) as cur4:
                for p in all_pos:
                    cur4.execute(
                        f"SELECT price FROM market_prices WHERE symbol={ph} "
                        f"ORDER BY id DESC LIMIT 1", (p['symbol'],))
                    row = cur4.fetchone()
                    if row:
                        price = (row[0] if isinstance(row, (list, tuple))
                                 else row['price'])
                        pnl_pct = (price - p['entry_price']) / p['entry_price'] * 100
                        scored.append((p['symbol'], pnl_pct))
            release_db_connection(conn4)

            if scored:
                scored.sort(key=lambda x: x[1], reverse=True)
                top = scored[:3]
                worst = sorted(scored, key=lambda x: x[1])[:3]
                lines.append("")
                lines.append("Best: " + ", ".join(
                    f"{s} {p:+.1f}%" for s, p in top))
                lines.append("Worst: " + ", ".join(
                    f"{s} {p:+.1f}%" for s, p in worst))
    except Exception:
        pass

    # --- Errors ---
    if _error_count > 0:
        lines.append(f"\n{_error_count} error(s) since last summary")
        _error_count = 0

    text = "\n".join(lines)
    try:
        bot = Bot(token=token)
        await bot.send_message(chat_id=chat_id, text=text)
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
    """Send a concise trade execution alert (plain text, no Markdown)."""
    tg_cfg = app_config.get('notification_services', {}).get('telegram', {})
    token = tg_cfg.get('token')
    chat_id = tg_cfg.get('chat_id')
    if not token or not chat_id:
        return

    if action == "BUY":
        cost = entry_price * quantity
        lines = [f">> BUY {symbol} ({trading_strategy})"]
        lines.append(f"   ${entry_price:,.2f} x {quantity:.4f} (${cost:.0f})")

        if gemini_direction and gemini_confidence:
            lines.append(
                f"   Gemini: {gemini_direction} {gemini_confidence:.2f} "
                f"({catalyst_freshness or 'N/A'})")
        if key_headline:
            lines.append(f'   "{key_headline[:100]}"')
        if signal_strength:
            mods = []
            if macro_multiplier != 1.0:
                mods.append(f"macro {macro_multiplier:.1f}x")
            if streak_multiplier != 1.0:
                mods.append(f"streak {streak_multiplier:.1f}x")
            if sma_override:
                mods.append("SMA override")
            mod_str = f" | {', '.join(mods)}" if mods else ""
            lines.append(f"   Strength: {signal_strength:.2f}{mod_str}")

    elif action == "SELL":
        lines = [f"<< SELL {symbol} ({trading_strategy})"]
        if entry_price and exit_price:
            lines.append(
                f"   ${entry_price:,.2f} -> ${exit_price:,.2f} | "
                f"{pnl_pct:+.1f}% | ${pnl:+.2f}")
        if hold_duration:
            lines.append(f"   Hold: {hold_duration} | {exit_reason}")
        elif exit_reason:
            lines.append(f"   Exit: {exit_reason}")
    else:
        lines = [f"{action} {symbol} ({trading_strategy})"]
        if reason:
            lines.append(f"   {reason[:150]}")

    text = "\n".join(lines)
    try:
        bot = Bot(token=token)
        await bot.send_message(chat_id=chat_id, text=text)
    except Exception as e:
        log.warning(f"Failed to send trade alert: {e}")
