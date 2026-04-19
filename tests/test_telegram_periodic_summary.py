"""Regression test for the 4h periodic summary.

Specifically guards against the sqlite3.Row-factory bug where
`isinstance(row, (list, tuple))` returned False for Row objects and the
else-branch (`row.get(...)`) raised AttributeError — silently caught by
`except: pass`, producing a summary that showed $0 realized / 0 open
positions for every strategy.
"""

import asyncio
import sqlite3
from unittest.mock import patch, MagicMock


def _populate_db(conn):
    """Seed an in-memory SQLite DB with the minimum shape the summary reads."""
    cur = conn.cursor()
    # Minimal trades schema — only the columns the summary touches.
    cur.execute("""
        CREATE TABLE trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol TEXT NOT NULL,
            side TEXT,
            entry_price REAL NOT NULL,
            quantity REAL NOT NULL,
            status TEXT NOT NULL,
            pnl REAL,
            exit_price REAL,
            entry_timestamp TIMESTAMP,
            exit_timestamp TIMESTAMP,
            asset_type TEXT DEFAULT 'crypto',
            trading_strategy TEXT DEFAULT 'manual',
            strategy_type TEXT,
            exit_reason TEXT
        )
    """)
    cur.execute("""
        CREATE TABLE market_prices (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol TEXT NOT NULL,
            price REAL NOT NULL,
            timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    # Closed trades → realized PnL
    cur.execute("""INSERT INTO trades
        (symbol, side, entry_price, quantity, status, pnl, exit_price,
         entry_timestamp, exit_timestamp, asset_type, trading_strategy, exit_reason)
        VALUES ('BTC', 'BUY', 50000, 0.01, 'CLOSED', 150.35, 65035,
                '2026-03-01 10:00:00', '2026-03-05 12:00:00', 'crypto', 'auto', 'trailing_stop')""")
    cur.execute("""INSERT INTO trades
        (symbol, side, entry_price, quantity, status, pnl, exit_price,
         entry_timestamp, exit_timestamp, asset_type, trading_strategy, exit_reason)
        VALUES ('ETH', 'BUY', 2000, 1.0, 'CLOSED', 80.69, 2080.69,
                '2026-03-02 10:00:00', '2026-03-06 12:00:00', 'crypto', 'conservative', 'take_profit')""")
    # Open trade for auto → should appear in open_count and pos_details
    cur.execute("""INSERT INTO trades
        (symbol, side, entry_price, quantity, status, entry_timestamp,
         asset_type, trading_strategy)
        VALUES ('SOL', 'BUY', 100.0, 1.0, 'OPEN', '2026-04-18 10:00:00', 'crypto', 'auto')""")
    # Latest price so unrealized computes
    cur.execute("INSERT INTO market_prices (symbol, price) VALUES ('SOL', 110.0)")
    conn.commit()


def test_summary_renders_realized_pnl_not_zero(monkeypatch):
    """Regression: the Row-factory bug made realized always $0.
    With closed trades summing to +$150.35 (auto) and +$80.69 (conservative),
    the rendered summary must include those figures.
    """
    conn = sqlite3.connect(':memory:')
    conn.row_factory = sqlite3.Row
    _populate_db(conn)

    # Patch DB layer so the summary code operates against our in-memory conn
    monkeypatch.setattr(
        'src.notify.telegram_periodic_summary.get_db_connection',
        lambda: conn)
    monkeypatch.setattr(
        'src.notify.telegram_periodic_summary.release_db_connection',
        lambda c: None)

    # Return our seeded OPEN position for 'auto' only
    def fake_get_open_positions(asset_type=None, trading_strategy=None):
        if trading_strategy == 'auto':
            return [{'symbol': 'SOL', 'entry_price': 100.0, 'quantity': 1.0}]
        return []
    monkeypatch.setattr(
        'src.execution.binance_trader.get_open_positions',
        fake_get_open_positions)

    # Patch macro regime fetcher (avoids live VIX HTTP call)
    monkeypatch.setattr(
        'src.analysis.macro_regime.get_macro_regime',
        lambda: {'regime': 'RISK_ON', 'score': 3.5,
                 'indicators': {'vix': {'current': 16.0}}})

    # Capture sent text instead of hitting Telegram
    captured = []

    class FakeBot:
        def __init__(self, *a, **k): pass

        async def send_message(self, chat_id=None, text=None, parse_mode=None, **kw):
            captured.append(text)
            return MagicMock(message_id=999)

    # Ensure config has token + chat_id so the send path runs
    monkeypatch.setattr(
        'src.notify.telegram_periodic_summary.app_config',
        {'notification_services': {'telegram': {
            'token': 'test-token', 'chat_id': '7910661624'}}})

    with patch('src.notify.telegram_periodic_summary.Bot', FakeBot):
        from src.notify.telegram_periodic_summary import send_periodic_summary
        asyncio.run(send_periodic_summary())

    assert len(captured) == 1, \
        f"expected exactly 1 send, got {len(captured)}"
    text = captured[0]

    # Regression assertions: realized PnL from closed trades must surface
    assert "+150" in text, \
        f"auto realized +$150.35 missing from summary:\n{text}"
    assert "+80" in text or "+81" in text, \
        f"conservative realized +$80.69 missing from summary:\n{text}"
    # SOL open position with +10% unrealized must surface — "Solana" is the
    # display-name mapping via _get_name, and "1 open" confirms open_count.
    assert "Solana" in text or "SOL" in text, \
        f"open position SOL missing from summary:\n{text}"
    assert "1 open" in text, \
        f"open_count=1 missing (get_open_positions bug regressed?):\n{text}"
    # Must NOT show the old bug symptom: all zeros
    # (individual $0 lines OK if a strategy genuinely has zero; but AUTO should not)
    assert "AUTO" in text, f"AUTO row missing:\n{text}"
    auto_line = [ln for ln in text.split("\n") if "AUTO" in ln]
    assert auto_line, "AUTO line not found"
    assert "realized $0" not in auto_line[0], \
        f"AUTO shows $0 realized — Row-factory bug regressed:\n{auto_line[0]}"


def test_summary_handles_empty_db_gracefully(monkeypatch):
    """No trades at all — summary should still render without errors."""
    conn = sqlite3.connect(':memory:')
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE trades (
            id INTEGER PRIMARY KEY, symbol TEXT, side TEXT, entry_price REAL,
            quantity REAL, status TEXT, pnl REAL, exit_price REAL,
            entry_timestamp TIMESTAMP, exit_timestamp TIMESTAMP,
            trading_strategy TEXT, exit_reason TEXT)
    """)
    cur.execute("""
        CREATE TABLE market_prices (
            id INTEGER PRIMARY KEY, symbol TEXT, price REAL,
            timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP)
    """)
    conn.commit()

    monkeypatch.setattr(
        'src.notify.telegram_periodic_summary.get_db_connection',
        lambda: conn)
    monkeypatch.setattr(
        'src.notify.telegram_periodic_summary.release_db_connection',
        lambda c: None)
    monkeypatch.setattr(
        'src.execution.binance_trader.get_open_positions',
        lambda asset_type=None, trading_strategy=None: [])
    monkeypatch.setattr(
        'src.analysis.macro_regime.get_macro_regime',
        lambda: {'regime': 'RISK_ON', 'score': 0,
                 'indicators': {'vix': {'current': 15.0}}})
    monkeypatch.setattr(
        'src.notify.telegram_periodic_summary.app_config',
        {'notification_services': {'telegram': {
            'token': 'test-token', 'chat_id': '7910661624'}}})

    captured = []

    class FakeBot:
        def __init__(self, *a, **k): pass

        async def send_message(self, chat_id=None, text=None, parse_mode=None, **kw):
            captured.append(text)
            return MagicMock(message_id=999)

    with patch('src.notify.telegram_periodic_summary.Bot', FakeBot):
        from src.notify.telegram_periodic_summary import send_periodic_summary
        asyncio.run(send_periodic_summary())

    assert len(captured) == 1
    text = captured[0]
    # All three strategies should appear with $0 — but this is CORRECT behavior
    # when DB is empty, not the Row-factory bug.
    assert "AUTO" in text and "CONS" in text and "LONG" in text
    assert "No trades in last 4h" in text
