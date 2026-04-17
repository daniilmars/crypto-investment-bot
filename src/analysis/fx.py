"""
FX conversion for mixed-currency portfolio normalization.

The ``trades.pnl`` column stores PnL in the trade's local currency — trades on
``.L`` tickers are in GBP, ``.CO`` in DKK, etc. Any sum across strategies
requires normalizing to a single display currency (USD).

Currency is derived from the Yahoo ticker suffix. Rates are fetched from
yfinance via the ``<CCY>USD=X`` synthetic pair, persisted to the ``fx_rates``
table, and cached in-process for 5 minutes.
"""

import time
from datetime import datetime, timezone

from src.logger import log

SUFFIX_TO_CCY = {
    ".L": "GBP",       # London
    ".MC": "EUR",      # Madrid
    ".HE": "EUR",      # Helsinki
    ".CO": "DKK",      # Copenhagen
    ".HK": "HKD",      # Hong Kong
    ".DE": "EUR",      # Xetra / Frankfurt
    ".PA": "EUR",      # Paris
    ".AS": "EUR",      # Amsterdam
    ".MI": "EUR",      # Milan
    ".BR": "EUR",      # Brussels
    ".T": "JPY",       # Tokyo
    ".TO": "CAD",      # Toronto
    ".SW": "CHF",      # Swiss
    ".AX": "AUD",      # Australia
    ".KS": "KRW",      # Korea
    ".SS": "CNY",      # Shanghai
    ".SZ": "CNY",      # Shenzhen
}

# Hard-coded fallbacks (approximate mid-market rates, April 2026) used only
# when the fx_rates table is empty AND yfinance is unreachable.
_FALLBACK_USD_PER_UNIT = {
    "USD": 1.0,
    "EUR": 1.09,
    "GBP": 1.27,
    "DKK": 0.146,
    "HKD": 0.128,
    "JPY": 0.0067,
    "CAD": 0.74,
    "CHF": 1.13,
    "AUD": 0.65,
    "KRW": 0.00074,
    "CNY": 0.14,
}

_CACHE_TTL_SECONDS = 300  # 5 min
_rate_cache: dict[str, tuple[float, float]] = {}  # {ccy: (usd_per_unit, fetched_ts)}


def currency_for_symbol(symbol: str) -> str:
    """Return the ISO currency code for a ticker, defaulting to USD."""
    if not symbol:
        return "USD"
    for suffix, ccy in SUFFIX_TO_CCY.items():
        if symbol.endswith(suffix):
            return ccy
    return "USD"


def _lookup_rate(ccy: str) -> float | None:
    """Resolve a USD-per-unit rate: in-process cache → DB → fallback map → None."""
    if ccy == "USD":
        return 1.0

    now = time.time()
    cached = _rate_cache.get(ccy)
    if cached and now - cached[1] < _CACHE_TTL_SECONDS:
        return cached[0]

    conn = None
    try:
        import psycopg2
        from src.database import get_db_connection, release_db_connection, _cursor
        conn = get_db_connection()
        if conn:
            is_pg = isinstance(conn, psycopg2.extensions.connection)
            placeholder = "%s" if is_pg else "?"
            with _cursor(conn) as cur:
                cur.execute(
                    f"SELECT usd_per_unit FROM fx_rates WHERE currency = {placeholder}",
                    (ccy,),
                )
                row = cur.fetchone()
            if row:
                rate = row[0] if not isinstance(row, dict) else row.get("usd_per_unit")
                if rate:
                    rate = float(rate)
                    _rate_cache[ccy] = (rate, now)
                    return rate
    except Exception as e:
        log.debug("FX DB lookup failed for %s: %s", ccy, e)
    finally:
        if conn is not None:
            try:
                from src.database import release_db_connection
                release_db_connection(conn)
            except Exception:
                pass

    fallback = _FALLBACK_USD_PER_UNIT.get(ccy)
    if fallback is not None:
        log.warning("Using hard-coded FX fallback for %s: %.4f USD/unit", ccy, fallback)
        _rate_cache[ccy] = (fallback, now)
        return fallback

    log.error("No FX rate available for %s", ccy)
    return None


def to_usd(amount: float, ccy: str, *, as_of=None) -> float:
    """Convert ``amount`` in ``ccy`` to USD.

    ``as_of`` is accepted for future historical-rate support but currently
    ignored — MVP applies the current rate to all values.
    """
    if amount is None or amount == 0 or ccy == "USD":
        return amount or 0.0
    rate = _lookup_rate(ccy)
    if rate is None:
        return amount  # graceful degradation: return unconverted amount
    return amount * rate


def clear_cache() -> None:
    """Drop the in-process rate cache. Used by tests and the refresh loop."""
    _rate_cache.clear()


def refresh_all_rates() -> dict[str, float]:
    """Fetch fresh rates for every currency in ``SUFFIX_TO_CCY`` via yfinance.

    Persists to ``fx_rates``, clears the in-process cache, and returns a
    ``{ccy: usd_per_unit}`` dict of what was fetched.
    """
    import yfinance as yf  # local import — optional at module load time
    from src.database import get_db_connection, _cursor

    needed = {c for c in SUFFIX_TO_CCY.values()} - {"USD"}
    fetched: dict[str, float] = {}

    for ccy in sorted(needed):
        ticker = f"{ccy}USD=X"
        try:
            hist = yf.Ticker(ticker).history(period="1d")
            if hist is None or hist.empty:
                log.warning("FX refresh: yfinance returned no data for %s", ticker)
                continue
            rate = float(hist["Close"].iloc[-1])
            if rate <= 0:
                log.warning("FX refresh: non-positive rate for %s: %s", ticker, rate)
                continue
            fetched[ccy] = rate
        except Exception as e:
            log.warning("FX refresh: failed for %s: %s", ticker, e)

    if not fetched:
        log.error("FX refresh fetched zero rates — leaving fx_rates unchanged")
        return {}

    import psycopg2
    from src.database import release_db_connection
    conn = get_db_connection()
    if not conn:
        log.error("FX refresh: no DB connection, skipping persistence")
        return fetched

    try:
        is_pg = isinstance(conn, psycopg2.extensions.connection)
        with _cursor(conn) as cur:
            now_iso = datetime.now(timezone.utc).isoformat()
            for ccy, rate in fetched.items():
                if is_pg:
                    cur.execute(
                        "INSERT INTO fx_rates (currency, usd_per_unit, fetched_at) "
                        "VALUES (%s, %s, %s) "
                        "ON CONFLICT (currency) DO UPDATE SET "
                        "usd_per_unit = EXCLUDED.usd_per_unit, "
                        "fetched_at = EXCLUDED.fetched_at",
                        (ccy, rate, now_iso),
                    )
                else:
                    cur.execute(
                        "INSERT OR REPLACE INTO fx_rates (currency, usd_per_unit, fetched_at) "
                        "VALUES (?, ?, ?)",
                        (ccy, rate, now_iso),
                    )
        conn.commit()
        log.info("FX refresh: persisted %d rates: %s",
                 len(fetched), ", ".join(f"{c}={r:.4f}" for c, r in fetched.items()))
    finally:
        release_db_connection(conn)

    clear_cache()
    return fetched
