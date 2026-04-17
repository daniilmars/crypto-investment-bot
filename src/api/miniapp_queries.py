"""
Data layer for the Mini App dashboard.

Queries SQLite/Postgres directly, USD-normalizes foreign-currency figures
via ``src.analysis.fx``, and returns plain dicts ready for JSON serialization.

MVP approximation: trades.pnl is stored in trade-local currency, but we
convert with the *current* FX rate (not historical) — documented as
``fx_method: "current_rate_snapshot"`` in API responses.
"""

from datetime import datetime, timezone

from src.analysis.fx import currency_for_symbol, to_usd
from src.logger import log


STALE_PRICE_SECONDS = 15 * 60  # flag prices older than 15 min

FX_METHOD_NOTE = "current_rate_snapshot"


def _rows_to_dicts(cur, rows):
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, r)) for r in rows]


def _sum(rows, key):
    return sum(float(r.get(key) or 0) for r in rows)


# ---------------------------------------------------------------- positions

def positions_data() -> dict:
    """Live unrealized PnL for every open trade, USD-normalized.

    Reuses the existing ``_get_position_price`` helper so pricing behavior
    matches the rest of the Telegram bot exactly.
    """
    from src.notify.telegram_dashboard import _get_position_price
    from src.database import get_db_connection, release_db_connection, _cursor

    conn = get_db_connection()
    if not conn:
        return {"positions": [], "as_of_ts": _now_iso(), "stale_prices": [],
                "fx_method": FX_METHOD_NOTE, "error": "db_unavailable"}

    try:
        with _cursor(conn) as cur:
            cur.execute(
                "SELECT id, symbol, entry_price, quantity, trading_strategy, "
                "asset_type, entry_timestamp "
                "FROM trades WHERE status = 'OPEN' "
                "ORDER BY trading_strategy, entry_timestamp"
            )
            rows = _rows_to_dicts(cur, cur.fetchall())
    finally:
        release_db_connection(conn)

    out = []
    stale: list[str] = []
    for r in rows:
        sym = r["symbol"]
        entry = float(r["entry_price"] or 0)
        qty = float(r["quantity"] or 0)
        ccy = currency_for_symbol(sym)

        try:
            price = _get_position_price(sym) or entry
        except Exception as e:
            log.debug("price lookup failed for %s: %s", sym, e)
            price = entry
            stale.append(sym)

        raw_pnl = (price - entry) * qty
        raw_pnl_pct = ((price - entry) / entry * 100.0) if entry else 0.0
        pnl_usd = to_usd(raw_pnl, ccy)

        out.append({
            "id": r["id"],
            "symbol": sym,
            "strategy": r.get("trading_strategy") or "manual",
            "asset_type": r.get("asset_type") or "crypto",
            "currency": ccy,
            "entry_price": entry,
            "current_price": price,
            "quantity": qty,
            "pnl_local": raw_pnl,
            "pnl_usd": pnl_usd,
            "pnl_pct": raw_pnl_pct,
            "entry_timestamp": r.get("entry_timestamp"),
            "age_days": _age_days(r.get("entry_timestamp")),
        })

    return {
        "positions": out,
        "as_of_ts": _now_iso(),
        "stale_prices": stale,
        "fx_method": FX_METHOD_NOTE,
    }


# ---------------------------------------------------------------- summary

def summary_data() -> dict:
    """Realized PnL buckets + live unrealized + regime snapshot."""
    from src.database import get_db_connection, release_db_connection, _cursor

    conn = get_db_connection()
    if not conn:
        return {"error": "db_unavailable"}

    try:
        with _cursor(conn) as cur:
            # All closed trades (small table; do USD conversion in Python)
            cur.execute(
                "SELECT symbol, pnl, exit_timestamp, trading_strategy "
                "FROM trades WHERE status = 'CLOSED'"
            )
            closed = _rows_to_dicts(cur, cur.fetchall())

            cur.execute(
                "SELECT trading_strategy, COUNT(*) AS n "
                "FROM trades WHERE status = 'OPEN' "
                "GROUP BY trading_strategy"
            )
            open_by_strat = {r["trading_strategy"] or "manual": int(r["n"])
                             for r in _rows_to_dicts(cur, cur.fetchall())}

            cur.execute(
                "SELECT regime, score, recorded_at FROM macro_regime_history "
                "ORDER BY recorded_at DESC LIMIT 1"
            )
            regime_row = cur.fetchone()
    finally:
        release_db_connection(conn)

    now = datetime.now(timezone.utc)
    d1_ago = now.timestamp() - 86400
    d7_ago = now.timestamp() - 86400 * 7
    d30_ago = now.timestamp() - 86400 * 30

    bucket = {"d1": 0.0, "d7": 0.0, "d30": 0.0, "all": 0.0}
    wins_7d = losses_7d = 0
    for t in closed:
        ccy = currency_for_symbol(t["symbol"])
        pnl_usd = to_usd(float(t["pnl"] or 0), ccy)
        bucket["all"] += pnl_usd
        exit_ts = _parse_ts(t.get("exit_timestamp"))
        if exit_ts is None:
            continue
        if exit_ts >= d30_ago:
            bucket["d30"] += pnl_usd
        if exit_ts >= d7_ago:
            bucket["d7"] += pnl_usd
            if pnl_usd > 0:
                wins_7d += 1
            else:
                losses_7d += 1
        if exit_ts >= d1_ago:
            bucket["d1"] += pnl_usd

    # Unrealized now — reuse positions_data
    pos_blob = positions_data()
    unrealized_now = sum(float(p["pnl_usd"] or 0) for p in pos_blob.get("positions", []))
    capital_deployed = sum(
        to_usd(float(p["entry_price"]) * float(p["quantity"]), p["currency"])
        for p in pos_blob.get("positions", [])
    )

    regime = None
    if regime_row is not None:
        # regime_row may be a tuple or a dict depending on cursor factory
        if isinstance(regime_row, dict):
            regime = {"regime": regime_row.get("regime"),
                      "score": float(regime_row.get("score") or 0),
                      "as_of": regime_row.get("recorded_at")}
        else:
            regime = {"regime": regime_row[0],
                      "score": float(regime_row[1] or 0),
                      "as_of": regime_row[2]}

    total_7d = wins_7d + losses_7d
    win_rate_7d = (wins_7d / total_7d) if total_7d else None

    return {
        "realized": bucket,
        "unrealized_now_usd": unrealized_now,
        "capital_deployed_usd": capital_deployed,
        "open_by_strategy": open_by_strat,
        "win_rate_7d": win_rate_7d,
        "wins_7d": wins_7d,
        "losses_7d": losses_7d,
        "regime": regime,
        "stale_prices": pos_blob.get("stale_prices", []),
        "as_of_ts": _now_iso(),
        "fx_method": FX_METHOD_NOTE,
    }


# ---------------------------------------------------------------- equity

def equity_data(days: int = 30) -> dict:
    """Cumulative realized PnL timeline + a single 'now' unrealized point.

    Returns ``points`` as an ordered list of ``{t, realized_usd}`` from the
    first close within the window to the most recent, with a bucket per day.
    Adds a final ``now`` point with unrealized on top.
    """
    from src.database import get_db_connection, release_db_connection, _cursor

    conn = get_db_connection()
    if not conn:
        return {"points": [], "now": None, "error": "db_unavailable"}

    try:
        with _cursor(conn) as cur:
            cur.execute(
                "SELECT symbol, pnl, exit_timestamp, trading_strategy "
                "FROM trades WHERE status='CLOSED' AND exit_timestamp IS NOT NULL "
                "ORDER BY exit_timestamp ASC"
            )
            rows = _rows_to_dicts(cur, cur.fetchall())
    finally:
        release_db_connection(conn)

    now_ts = datetime.now(timezone.utc).timestamp()
    cutoff = now_ts - days * 86400

    cumulative = 0.0
    # First compute cumulative since the beginning of time, then slice in the window
    series: list[tuple[float, float, str]] = []  # (ts, cumulative_usd, strategy)
    for r in rows:
        pnl_usd = to_usd(float(r["pnl"] or 0), currency_for_symbol(r["symbol"]))
        cumulative += pnl_usd
        ts = _parse_ts(r.get("exit_timestamp"))
        if ts is None:
            continue
        series.append((ts, cumulative, r.get("trading_strategy") or "manual"))

    points = [
        {"t": datetime.fromtimestamp(ts, tz=timezone.utc).isoformat(),
         "realized_usd": round(cum, 2)}
        for (ts, cum, _) in series
        if ts >= cutoff
    ]

    # Live unrealized marker
    pos_blob = positions_data()
    unrealized_now = sum(float(p["pnl_usd"] or 0) for p in pos_blob.get("positions", []))

    return {
        "points": points,
        "now": {
            "t": _now_iso(),
            "realized_usd": round(cumulative, 2),
            "unrealized_usd": round(unrealized_now, 2),
            "portfolio_delta_usd": round(cumulative + unrealized_now, 2),
        },
        "window_days": days,
        "fx_method": FX_METHOD_NOTE,
    }


# ---------------------------------------------------------------- helpers

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_ts(s) -> float | None:
    if s is None:
        return None
    if isinstance(s, datetime):
        return (s if s.tzinfo else s.replace(tzinfo=timezone.utc)).timestamp()
    try:
        dt = datetime.fromisoformat(str(s).replace(" ", "T"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except Exception:
        return None


def _age_days(ts) -> int | None:
    t = _parse_ts(ts)
    if t is None:
        return None
    return max(0, int((datetime.now(timezone.utc).timestamp() - t) // 86400))
