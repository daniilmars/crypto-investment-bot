"""
Data layer for the Mini App dashboard.

Queries SQLite/Postgres directly, USD-normalizes foreign-currency figures
via ``src.analysis.fx``, and returns plain dicts ready for JSON serialization.

MVP approximation: trades.pnl is stored in trade-local currency, but we
convert with the *current* FX rate (not historical) — documented as
``fx_method: "current_rate_snapshot"`` in API responses.
"""

from datetime import datetime, timezone

from src.analysis.display_names import display_name
from src.analysis.fx import currency_for_symbol, to_usd
from src.config import app_config
from src.logger import log


STALE_PRICE_SECONDS = 15 * 60  # flag prices older than 15 min

FX_METHOD_NOTE = "current_rate_snapshot"


def _rows_to_dicts(cur, rows):
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, r)) for r in rows]


def _sum(rows, key):
    return sum(float(r.get(key) or 0) for r in rows)


# ---------------------------------------------------------------- positions

_POSITIONS_WITH_RATIONALE_SQL = """
    SELECT t.id, t.order_id, t.symbol, t.entry_price, t.quantity,
           t.trading_strategy, t.asset_type, t.entry_timestamp,
           t.dynamic_sl_pct, t.dynamic_tp_pct, t.trade_reason,
           sa.gemini_direction, sa.gemini_confidence, sa.catalyst_type,
           sa.source_names, sa.signal_timestamp,
           ga.reasoning, ga.key_headline, ga.risk_factors,
           ga.catalyst_freshness, ga.hype_vs_fundamental, ga.market_mood
    FROM trades t
    LEFT JOIN signal_attribution sa ON sa.trade_order_id = t.order_id
    LEFT JOIN gemini_assessments ga
      ON ga.symbol = t.symbol
     AND ga.created_at <= t.entry_timestamp
     AND ga.created_at >= datetime(t.entry_timestamp, '-30 minutes')
    WHERE t.status = 'OPEN'
      AND (ga.id IS NULL
           OR ga.id = (
             SELECT g2.id FROM gemini_assessments g2
             WHERE g2.symbol = t.symbol
               AND g2.created_at <= t.entry_timestamp
               AND g2.created_at >= datetime(t.entry_timestamp, '-30 minutes')
             ORDER BY g2.created_at DESC LIMIT 1))
    ORDER BY t.trading_strategy, t.entry_timestamp
"""


def _parse_risk_factors(raw) -> list[str]:
    """risk_factors is JSON in new rows, plain text in old ones. Best effort."""
    if not raw:
        return []
    if isinstance(raw, list):
        return [str(x) for x in raw if x]
    s = str(raw).strip()
    if not s or s == "null":
        return []
    try:
        import json as _json
        v = _json.loads(s)
        if isinstance(v, list):
            return [str(x) for x in v if x]
        if isinstance(v, str):
            return [v]
    except Exception:
        pass
    # Plain text fallback — split on common separators
    for sep in ("|", ";", ","):
        if sep in s:
            return [p.strip() for p in s.split(sep) if p.strip()]
    return [s]


def _build_rationale(row: dict) -> dict | None:
    """Collect rationale fields from a joined row. None if no data at all."""
    catalyst = row.get("catalyst_type")
    direction = row.get("gemini_direction")
    confidence = row.get("gemini_confidence")
    key_headline = row.get("key_headline")
    reasoning = row.get("reasoning")
    sources_raw = row.get("source_names") or ""
    risks_raw = row.get("risk_factors")
    trade_reason = row.get("trade_reason")

    has_any = any([catalyst, direction, confidence, key_headline, reasoning,
                   sources_raw, risks_raw, trade_reason])
    if not has_any:
        return None

    sources = [s.strip() for s in str(sources_raw).split(",") if s.strip()]
    return {
        "gemini_direction": direction,
        "gemini_confidence": float(confidence) if confidence is not None else None,
        "catalyst_type": catalyst,
        "catalyst_freshness": row.get("catalyst_freshness"),
        "hype_vs_fundamental": row.get("hype_vs_fundamental"),
        "key_headline": key_headline,
        "reasoning": reasoning,
        "sources": sources[:10],
        "risk_factors": _parse_risk_factors(risks_raw)[:5],
        "market_mood": row.get("market_mood"),
        "signal_timestamp": row.get("signal_timestamp"),
        "trade_reason": trade_reason,
    }


def _sl_tp_distances(entry: float, current: float,
                     sl_pct: float | None, tp_pct: float | None) -> dict:
    """Compute SL/TP prices and 'distance to trigger' as % of current price."""
    out = {"sl_price": None, "tp_price": None,
           "sl_distance_pct": None, "tp_distance_pct": None}
    if not entry or not current:
        return out
    if sl_pct is not None:
        sl_price = entry * (1.0 - float(sl_pct))
        out["sl_price"] = sl_price
        # Positive = distance remaining (good). Negative = past stop.
        out["sl_distance_pct"] = ((current - sl_price) / current * 100.0)
    if tp_pct is not None:
        tp_price = entry * (1.0 + float(tp_pct))
        out["tp_price"] = tp_price
        out["tp_distance_pct"] = ((tp_price - current) / current * 100.0)
    return out


def positions_data() -> dict:
    """Live unrealized PnL for every open trade, USD-normalized,
    with rationale (Gemini catalyst, headline, reasoning, sources, risks)
    and SL/TP distances joined from signal_attribution + gemini_assessments.
    """
    from src.notify.telegram_dashboard import _get_position_price
    from src.database import get_db_connection, release_db_connection, _cursor

    conn = get_db_connection()
    if not conn:
        return {"positions": [], "as_of_ts": _now_iso(), "stale_prices": [],
                "fx_method": FX_METHOD_NOTE, "error": "db_unavailable"}

    try:
        with _cursor(conn) as cur:
            cur.execute(_POSITIONS_WITH_RATIONALE_SQL)
            rows = _rows_to_dicts(cur, cur.fetchall())
    finally:
        release_db_connection(conn)

    out = []
    stale: list[str] = []
    seen_ids: set = set()
    for r in rows:
        # The join can produce duplicates when multiple assessments match the
        # 30-min window; dedupe on trade id.
        if r["id"] in seen_ids:
            continue
        seen_ids.add(r["id"])

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

        distances = _sl_tp_distances(
            entry, price, r.get("dynamic_sl_pct"), r.get("dynamic_tp_pct"))

        out.append({
            "id": r["id"],
            "order_id": r.get("order_id"),
            "symbol": sym,
            "display_name": display_name(sym),
            "strategy": r.get("trading_strategy") or "unknown",
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
            "sl_pct": r.get("dynamic_sl_pct"),
            "tp_pct": r.get("dynamic_tp_pct"),
            **distances,
            "rationale": _build_rationale(r),
        })

    return {
        "positions": out,
        "as_of_ts": _now_iso(),
        "stale_prices": stale,
        "fx_method": FX_METHOD_NOTE,
    }


# ---------------------------------------------------------------- capital

# Strategies the UI knows about; aligned with STRATEGY_ORDER in app.js so
# the Mini App's group ordering matches the capital section.
_KNOWN_STRATEGIES = ("auto", "conservative", "longterm")
_DEFAULT_STARTING_CAPITAL = 10000.0
_warned_strategies: set[str] = set()


def _starting_capital_for_strategy(strategy: str) -> float:
    """Mirror the precedence used by binance_trader._get_paper_balance so the
    Mini App's capital numbers stay consistent with Telegram /status output.

    - 'auto'  → settings.auto_trading.paper_trading_initial_capital
    - everything else → settings.paper_trading_initial_capital (shared pool)
    - missing → $10,000 default, warn once per unknown strategy name
    """
    settings = app_config.get('settings', {}) or {}
    if strategy == 'auto':
        cfg = settings.get('auto_trading', {}) or {}
        v = cfg.get('paper_trading_initial_capital')
        if v is not None:
            return float(v)
    v = settings.get('paper_trading_initial_capital')
    if v is not None:
        return float(v)
    if strategy not in _warned_strategies:
        log.warning("Unknown strategy '%s' for starting-capital lookup, "
                    "defaulting to $%.0f", strategy, _DEFAULT_STARTING_CAPITAL)
        _warned_strategies.add(strategy)
    return _DEFAULT_STARTING_CAPITAL


def compute_capital_breakdown(
    realized_by_strategy: dict[str, float],
    deployed_by_strategy: dict[str, float],
    unrealized_by_strategy: dict[str, float],
    open_count_by_strategy: dict[str, int],
    include_empty: bool = False,
) -> dict:
    """Build the ``capital`` payload for /api/miniapp/summary.

    Pure function — no DB calls, no FX conversion. Caller passes already-
    USD-normalized per-strategy aggregates (FX is done upstream by
    positions_data + the closed-trade loop in summary_data).

    Per-strategy invariants:
        free      = starting + realized − deployed
        total     = free + deployed + unrealized
                  = starting + realized + unrealized
        util_pct  = 100 × deployed / (deployed + free), clamped [0, 100]
                    (None if denominator <= 0)

    Aggregate:
        capital.total_value_usd == sum(s.total for s in by_strategy)
    """
    seen = set(realized_by_strategy) | set(deployed_by_strategy) \
        | set(unrealized_by_strategy) | set(open_count_by_strategy)
    # Always emit auto/conservative/longterm if include_empty. Manual is
    # removed; any stale rows still tagged 'manual' end up under 'unknown'
    # via the fallback in summary_data and are only shown when active.
    candidates = list(_KNOWN_STRATEGIES)
    for s in seen:
        if s not in candidates and s != 'manual':
            candidates.append(s)

    by_strategy: list[dict] = []
    for s in candidates:
        realized = float(realized_by_strategy.get(s) or 0)
        deployed = float(deployed_by_strategy.get(s) or 0)
        unrealized = float(unrealized_by_strategy.get(s) or 0)
        open_count = int(open_count_by_strategy.get(s) or 0)

        is_active = (open_count > 0) or (abs(realized) > 0.005)
        if not is_active and not include_empty:
            continue

        starting = _starting_capital_for_strategy(s)
        free = starting + realized - deployed
        total = free + deployed + unrealized
        denom = deployed + free
        util_pct = (100.0 * deployed / denom) if denom > 0 else None
        if util_pct is not None:
            util_pct = max(0.0, min(100.0, util_pct))

        # Return % on starting capital. None when starting==0 so the UI can
        # render a dash instead of dividing by zero.
        if starting > 0:
            return_pct = round((total - starting) / starting * 100.0, 2)
            realized_return_pct = round(realized / starting * 100.0, 2)
        else:
            return_pct = None
            realized_return_pct = None

        by_strategy.append({
            "name": s,
            "starting_usd": round(starting, 2),
            "realized_usd": round(realized, 2),
            "deployed_usd": round(deployed, 2),
            "unrealized_usd": round(unrealized, 2),
            "free_usd": round(free, 2),
            "total_usd": round(total, 2),
            "open_count": open_count,
            "utilization_pct": (round(util_pct, 1) if util_pct is not None else None),
            "return_pct": return_pct,
            "realized_return_pct": realized_return_pct,
        })

    total_value = sum(s["total_usd"] for s in by_strategy)
    locked_total = sum(s["deployed_usd"] for s in by_strategy)
    free_total = sum(s["free_usd"] for s in by_strategy)
    starting_total = sum(s["starting_usd"] for s in by_strategy)
    realized_total = sum(s["realized_usd"] for s in by_strategy)
    denom = locked_total + free_total
    overall_util = (100.0 * locked_total / denom) if denom > 0 else None
    if overall_util is not None:
        overall_util = max(0.0, min(100.0, overall_util))

    if starting_total > 0:
        overall_return_pct = round(
            (total_value - starting_total) / starting_total * 100.0, 2)
        overall_realized_pct = round(
            realized_total / starting_total * 100.0, 2)
    else:
        overall_return_pct = None
        overall_realized_pct = None

    return {
        "total_value_usd": round(total_value, 2),
        "cash_locked_usd": round(locked_total, 2),
        "cash_free_usd": round(free_total, 2),
        "utilization_pct": (round(overall_util, 1) if overall_util is not None else None),
        "return_pct": overall_return_pct,
        "realized_return_pct": overall_realized_pct,
        "by_strategy": by_strategy,
        "fx_method": FX_METHOD_NOTE,
        "as_of_ts": _now_iso(),
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
            open_by_strat = {r["trading_strategy"] or "unknown": int(r["n"])
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
    realized_by_strategy: dict[str, float] = {}
    for t in closed:
        ccy = currency_for_symbol(t["symbol"])
        pnl_usd = to_usd(float(t["pnl"] or 0), ccy)
        bucket["all"] += pnl_usd
        strat = (t.get("trading_strategy") or "unknown")
        realized_by_strategy[strat] = realized_by_strategy.get(strat, 0.0) + pnl_usd
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

    # Unrealized now — reuse positions_data + per-strategy buckets for capital
    pos_blob = positions_data()
    unrealized_now = sum(float(p["pnl_usd"] or 0) for p in pos_blob.get("positions", []))
    capital_deployed = sum(
        to_usd(float(p["entry_price"]) * float(p["quantity"]), p["currency"])
        for p in pos_blob.get("positions", [])
    )
    deployed_by_strategy: dict[str, float] = {}
    unrealized_by_strategy: dict[str, float] = {}
    for p in pos_blob.get("positions", []):
        s = p.get("strategy") or "unknown"
        deployed_by_strategy[s] = deployed_by_strategy.get(s, 0.0) + to_usd(
            float(p["entry_price"]) * float(p["quantity"]), p["currency"])
        unrealized_by_strategy[s] = unrealized_by_strategy.get(s, 0.0) + float(
            p["pnl_usd"] or 0)

    capital = compute_capital_breakdown(
        realized_by_strategy=realized_by_strategy,
        deployed_by_strategy=deployed_by_strategy,
        unrealized_by_strategy=unrealized_by_strategy,
        open_count_by_strategy={k: int(v) for k, v in open_by_strat.items()},
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
        "capital": capital,
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
        series.append((ts, cumulative, r.get("trading_strategy") or "unknown"))

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


# ------------------------------------------------------------- recent trades

_RECENT_TRADES_SQL = """
    SELECT t.id, t.order_id, t.symbol, t.trading_strategy, t.asset_type,
           t.entry_price, t.exit_price, t.quantity, t.pnl,
           t.entry_timestamp, t.exit_timestamp, t.exit_reason,
           t.dynamic_sl_pct, t.dynamic_tp_pct, t.trade_reason,
           sa.gemini_direction, sa.gemini_confidence, sa.catalyst_type,
           sa.source_names, sa.signal_timestamp,
           ga.reasoning, ga.key_headline, ga.risk_factors,
           ga.catalyst_freshness, ga.hype_vs_fundamental, ga.market_mood
    FROM trades t
    LEFT JOIN signal_attribution sa ON sa.trade_order_id = t.order_id
    LEFT JOIN gemini_assessments ga
      ON ga.symbol = t.symbol
     AND ga.created_at <= t.entry_timestamp
     AND ga.created_at >= datetime(t.entry_timestamp, '-30 minutes')
    WHERE t.status = 'CLOSED'
      AND t.exit_timestamp IS NOT NULL
      AND (ga.id IS NULL
           OR ga.id = (
             SELECT g2.id FROM gemini_assessments g2
             WHERE g2.symbol = t.symbol
               AND g2.created_at <= t.entry_timestamp
               AND g2.created_at >= datetime(t.entry_timestamp, '-30 minutes')
             ORDER BY g2.created_at DESC LIMIT 1))
    ORDER BY t.exit_timestamp DESC
    LIMIT ?
"""


def recent_trades_data(limit: int = 10) -> dict:
    """Most recent closed trades with the rationale that drove the entry.

    Used by the new v2 Mini App 'Recent closed trades' section.
    """
    from src.database import get_db_connection, release_db_connection, _cursor

    conn = get_db_connection()
    if not conn:
        return {"trades": [], "as_of_ts": _now_iso(),
                "fx_method": FX_METHOD_NOTE, "error": "db_unavailable"}

    try:
        with _cursor(conn) as cur:
            cur.execute(_RECENT_TRADES_SQL, (int(limit),))
            rows = _rows_to_dicts(cur, cur.fetchall())
    finally:
        release_db_connection(conn)

    out = []
    seen_ids: set = set()
    for r in rows:
        if r["id"] in seen_ids:
            continue
        seen_ids.add(r["id"])

        sym = r["symbol"]
        entry = float(r["entry_price"] or 0)
        exit_p = float(r["exit_price"] or 0) if r["exit_price"] is not None else None
        qty = float(r["quantity"] or 0)
        ccy = currency_for_symbol(sym)
        raw_pnl = float(r["pnl"] or 0)
        pnl_usd = to_usd(raw_pnl, ccy)
        pnl_pct = ((exit_p - entry) / entry * 100.0) if (entry and exit_p) else None

        # Duration in hours
        dur_h = None
        entry_ts = _parse_ts(r.get("entry_timestamp"))
        exit_ts = _parse_ts(r.get("exit_timestamp"))
        if entry_ts and exit_ts:
            dur_h = max(0.0, (exit_ts - entry_ts) / 3600.0)

        out.append({
            "id": r["id"],
            "order_id": r.get("order_id"),
            "symbol": sym,
            "display_name": display_name(sym),
            "strategy": r.get("trading_strategy") or "unknown",
            "asset_type": r.get("asset_type") or "crypto",
            "currency": ccy,
            "entry_price": entry,
            "exit_price": exit_p,
            "quantity": qty,
            "pnl_local": raw_pnl,
            "pnl_usd": pnl_usd,
            "pnl_pct": pnl_pct,
            "entry_timestamp": r.get("entry_timestamp"),
            "exit_timestamp": r.get("exit_timestamp"),
            "duration_hours": dur_h,
            "exit_reason": r.get("exit_reason") or "unknown",
            "rationale": _build_rationale(r),
        })

    return {
        "trades": out,
        "limit": int(limit),
        "as_of_ts": _now_iso(),
        "fx_method": FX_METHOD_NOTE,
    }
