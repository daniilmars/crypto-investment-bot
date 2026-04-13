"""4-hour periodic summary + enhanced trade alerts.

Uses HTML parse_mode for reliable formatting (monospace tables via <pre>,
bold headers via <b>). HTML only needs &lt; &gt; &amp; escaping.
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


def _esc(text) -> str:
    """Escape HTML special characters."""
    return str(text).replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')


async def send_periodic_summary():
    """Build and send the consolidated 4-hour summary."""
    global _error_count

    tg_cfg = app_config.get('notification_services', {}).get('telegram', {})
    token = tg_cfg.get('token')
    chat_id = tg_cfg.get('chat_id')
    if not token or not chat_id:
        return

    now = datetime.now(timezone.utc)
    ph = "?"
    parts = []

    parts.append(f"<b>4H Summary</b> ({now.strftime('%H:%M UTC')})\n")

    # --- Macro regime ---
    try:
        from src.analysis.macro_regime import get_macro_regime
        regime = get_macro_regime()
        r_name = regime.get('regime', '?')
        r_score = regime.get('score', 0)
        vix_raw = regime.get('indicators', {}).get('vix', {}).get('current')
        vix = f"{vix_raw:.1f}" if isinstance(vix_raw, (int, float)) else '?'
        parts.append(f"Regime: <b>{r_name}</b> ({r_score:+.1f}) | VIX {vix}\n")
    except Exception:
        parts.append("Regime: unavailable\n")

    # --- Per-strategy table (monospace) ---
    try:
        from src.execution.binance_trader import get_open_positions
        from src.orchestration import bot_state
        import psycopg2

        strategies = ['auto', 'momentum', 'conservative', 'longterm']
        rows = []

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
                    realized = float(row[0] if isinstance(row, (list, tuple))
                                     else (row.get('coalesce') or row.get('sum')
                                           or 0)) or 0
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
                            price = float(row[0] if isinstance(row, (list, tuple))
                                          else row['price'])
                            unrealized += (price - p['entry_price']) * p['quantity']
                release_db_connection(conn2)
            except Exception:
                pass

            streak = bot_state.strategy_get_streak_state(strat)
            cw = streak.get('consecutive_wins', 0)
            sk = f"{cw}W" if cw > 0 else " -"

            short = strat[:4].upper()
            r_s = f"{realized:+.0f}" if realized else "0"
            u_s = f"{unrealized:+.0f}" if unrealized else "0"
            rows.append(f" {short:<5} ${r_s:>5} {open_count:>3}  ${u_s:>5} {sk:>3}")

        table = "<pre>"
        table += f" {'':5} {'PnL':>6} {'Opn':>3}  {'Unrl':>6} {'Sk':>3}\n"
        table += "\n".join(rows)
        table += "</pre>"
        parts.append(table)

    except Exception:
        parts.append("<i>Strategy data unavailable</i>\n")

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
            trade_lines = []
            for r in opened:
                sym = r[0] if isinstance(r, (list, tuple)) else r['symbol']
                st = r[1] if isinstance(r, (list, tuple)) else r['trading_strategy']
                ep = float(r[2] if isinstance(r, (list, tuple)) else r['entry_price'])
                qty = float(r[3] if isinstance(r, (list, tuple)) else r['quantity'])
                trade_lines.append(f"  + {sym} ({st}) ${ep * qty:.0f}")
            for r in closed:
                sym = r[0] if isinstance(r, (list, tuple)) else r['symbol']
                st = r[1] if isinstance(r, (list, tuple)) else r['trading_strategy']
                pnl = float((r[2] if isinstance(r, (list, tuple)) else r['pnl']) or 0)
                reason = r[3] if isinstance(r, (list, tuple)) else r['exit_reason']
                tag = "+" if pnl >= 0 else ""
                trade_lines.append(f"  - {sym} {tag}${pnl:.2f} ({reason})")
            parts.append("\n<b>Last 4h:</b>\n" + "\n".join(trade_lines))
        else:
            parts.append("\n<i>No trades in last 4h</i>")
    except Exception:
        pass

    # --- Top/worst open positions ---
    try:
        from src.execution.binance_trader import get_open_positions
        all_pos = get_open_positions.sync(asset_type='all', trading_strategy='all')
        if all_pos and len(all_pos) > 0:
            scored = []
            conn4 = get_db_connection()
            with _cursor(conn4) as cur4:
                for p in all_pos:
                    cur4.execute(
                        f"SELECT price FROM market_prices WHERE symbol={ph} "
                        f"ORDER BY id DESC LIMIT 1", (p['symbol'],))
                    row = cur4.fetchone()
                    if row:
                        price = float(row[0] if isinstance(row, (list, tuple))
                                      else row['price'])
                        pct = (price - p['entry_price']) / p['entry_price'] * 100
                        scored.append((p['symbol'], pct))
            release_db_connection(conn4)

            if len(scored) >= 2:
                scored.sort(key=lambda x: x[1], reverse=True)
                top = ", ".join(f"{s} {p:+.1f}%" for s, p in scored[:3])
                worst = ", ".join(f"{s} {p:+.1f}%" for s, p in
                                 sorted(scored, key=lambda x: x[1])[:3])
                parts.append(f"\nBest: {_esc(top)}")
                parts.append(f"Worst: {_esc(worst)}")
    except Exception:
        pass

    # --- Errors ---
    if _error_count > 0:
        parts.append(f"\n<i>{_error_count} error(s) since last summary</i>")
        _error_count = 0

    text = "\n".join(parts)
    try:
        bot = Bot(token=token)
        await bot.send_message(chat_id=chat_id, text=text, parse_mode='HTML')
        log.info("4h periodic summary sent.")
    except Exception as e:
        log.warning(f"Failed to send periodic summary: {e}")
        # Fallback: try plain text
        try:
            import re
            plain = re.sub(r'<[^>]+>', '', text)
            await bot.send_message(chat_id=chat_id, text=plain)
            log.info("4h summary sent (plain text fallback).")
        except Exception:
            pass


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
    """Send a concise trade execution alert."""
    tg_cfg = app_config.get('notification_services', {}).get('telegram', {})
    token = tg_cfg.get('token')
    chat_id = tg_cfg.get('chat_id')
    if not token or not chat_id:
        return

    if action == "BUY":
        cost = entry_price * quantity
        lines = [f"<b>BUY {_esc(symbol)}</b> ({_esc(trading_strategy)})"]
        lines.append(f"${entry_price:,.2f} x {quantity:.4f} (${cost:.0f})")

        if gemini_direction and gemini_confidence:
            lines.append(
                f"Gemini: {gemini_direction} {gemini_confidence:.2f} "
                f"({catalyst_freshness or '?'})")
        if key_headline:
            lines.append(f"<i>{_esc(key_headline[:100])}</i>")

        mods = []
        if macro_multiplier != 1.0:
            mods.append(f"macro {macro_multiplier:.1f}x")
        if streak_multiplier != 1.0:
            mods.append(f"streak {streak_multiplier:.1f}x")
        if sma_override:
            mods.append("SMA override")
        if signal_strength:
            mod_str = f" | {', '.join(mods)}" if mods else ""
            lines.append(f"Strength: {signal_strength:.2f}{mod_str}")

    elif action == "SELL":
        lines = [f"<b>SELL {_esc(symbol)}</b> ({_esc(trading_strategy)})"]
        if entry_price and exit_price:
            lines.append(
                f"${entry_price:,.2f} → ${exit_price:,.2f} | "
                f"{pnl_pct:+.1f}% | ${pnl:+.2f}")
        if hold_duration and exit_reason:
            lines.append(f"{hold_duration} | {exit_reason}")
        elif exit_reason:
            lines.append(exit_reason)
    else:
        lines = [f"<b>{_esc(action)} {_esc(symbol)}</b> ({_esc(trading_strategy)})"]
        if reason:
            lines.append(_esc(reason[:150]))

    text = "\n".join(lines)
    try:
        bot = Bot(token=token)
        await bot.send_message(chat_id=chat_id, text=text, parse_mode='HTML')
    except Exception as e:
        log.warning(f"Failed to send trade alert: {e}")
