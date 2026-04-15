"""SEC institutional filings collector — Form 4 (insiders) + 13F (institutions).

Produces synthetic articles into the `scraped_articles` table so they flow
through the existing Gemini scoring pipeline. The article title encodes the
key signal (insider buy/sell, fund add/trim, position size in dollars).

Sources:
  - OpenInsider (Form 4): https://openinsider.com — HTML scraping, no API key
  - 13f.info (13F): https://13f.info/funds — top hedge fund holdings

Both endpoints expose latest filings without authentication. We fetch
recent filings for our watchlist, format them as articles, dedupe via the
existing UNIQUE INDEX on title_hash, and let Gemini judge significance.
"""

from __future__ import annotations

import re
from typing import Iterable

import requests
from bs4 import BeautifulSoup

from src.database import compute_title_hash
from src.logger import log

OPENINSIDER_URL = "https://openinsider.com/screener"
THIRTEENF_FUND_URL = "https://13f.info/manager/{cik}"

# Top 20 "smart money" funds — well-known long-term holders.
# (cik, label) — CIK numbers from SEC EDGAR.
TOP_FUNDS: list[tuple[str, str]] = [
    ("0001067983", "Berkshire Hathaway"),
    ("0001336528", "Bridgewater Associates"),
    ("0001037389", "Renaissance Technologies"),
    ("0001423053", "Citadel Advisors"),
    ("0001029160", "Tiger Global"),
    ("0001135730", "Lone Pine Capital"),
    ("0001167483", "Coatue Management"),
    ("0001179392", "Viking Global"),
    ("0001061165", "Soros Fund Management"),
    ("0001321655", "Pershing Square"),
]

DEFAULT_TIMEOUT = 15
USER_AGENT = (
    "Mozilla/5.0 (compatible; CryptoInvestBot/1.0; +https://github.com/daniilmars)"
)


# --- Helpers ---------------------------------------------------------------

def _http_get(url: str, params: dict | None = None) -> str | None:
    """GET a URL with our UA + sane timeout. Returns text or None."""
    try:
        resp = requests.get(
            url, params=params,
            headers={"User-Agent": USER_AGENT, "Accept": "text/html"},
            timeout=DEFAULT_TIMEOUT,
        )
        if resp.status_code != 200:
            log.debug(f"[sec_filings] {url} returned {resp.status_code}")
            return None
        return resp.text
    except requests.RequestException as e:
        log.debug(f"[sec_filings] {url} failed: {e}")
        return None


def _parse_dollar(text: str) -> float | None:
    """Parse '$1,234,567' or '$1.2M' / '$1.2B' into a float."""
    if not text:
        return None
    t = text.strip().replace("$", "").replace(",", "")
    mult = 1.0
    if t.endswith("M"):
        mult = 1_000_000
        t = t[:-1]
    elif t.endswith("B"):
        mult = 1_000_000_000
        t = t[:-1]
    elif t.endswith("K"):
        mult = 1_000
        t = t[:-1]
    try:
        return float(t) * mult
    except ValueError:
        return None


def _make_article(
    title: str, source: str, source_url: str, symbol: str,
    description: str = "", category: str = "filings",
) -> dict:
    """Shape a row for save_articles_batch()."""
    return {
        "title": title,
        "title_hash": compute_title_hash(title),
        "source": source,
        "source_url": source_url,
        "description": description,
        "symbol": symbol,
        "category": category,
        "vader_score": None,
    }


# --- Form 4 (OpenInsider) --------------------------------------------------

def fetch_openinsider_recent(
    symbols: Iterable[str], min_dollar: float = 100_000,
) -> list[dict]:
    """Scrape OpenInsider for recent insider trades for the given symbols.

    Filters out trades below `min_dollar` to keep only material moves.
    Returns a list of article dicts ready for save_articles_batch().
    """
    articles: list[dict] = []
    sym_set = {s.upper() for s in symbols}

    # OpenInsider's "latest cluster buys" page covers all tickers; we filter.
    # `xp=1` includes purchases (P) and sales (S). `s=Last 30 Days`.
    params = {"s": "30", "xp": "1", "xs": "1"}
    html = _http_get(OPENINSIDER_URL, params=params)
    if not html:
        return []

    try:
        soup = BeautifulSoup(html, "html.parser")
        table = soup.find("table", class_="tinytable")
        if not table or not table.tbody:
            return []

        for row in table.tbody.find_all("tr"):
            cols = [c.get_text(strip=True) for c in row.find_all("td")]
            if len(cols) < 13:
                continue
            # Layout (approx): X, FilingDate, TradeDate, Ticker, CompanyName,
            # InsiderName, Title, TradeType, Price, Qty, Owned, dOwn, Value
            ticker = cols[3].upper()
            if ticker not in sym_set:
                continue
            insider = cols[5]
            insider_title = cols[6]
            trade_type = cols[7]  # "P - Purchase" or "S - Sale"
            value = _parse_dollar(cols[12])
            if value is None or value < min_dollar:
                continue

            action = "BUY" if trade_type.startswith("P") else "SELL"
            value_disp = f"${value/1_000_000:.2f}M" if value >= 1_000_000 \
                else f"${value/1_000:.0f}K"
            title = (f"{ticker} — Insider {action}: {insider} ({insider_title}) "
                     f"{trade_type.split(' - ')[-1].lower()} {value_disp}")
            articles.append(_make_article(
                title=title,
                source="OpenInsider",
                source_url="https://openinsider.com/",
                symbol=ticker,
                description=f"Form 4 filing on {cols[1]} (trade {cols[2]}). Value: {value_disp}.",
                category="filings_form4",
            ))
    except Exception as e:
        log.warning(f"[sec_filings] OpenInsider parse failed: {e}")

    log.info(f"[sec_filings] Form 4: {len(articles)} insider filings for watchlist.")
    return articles


# --- 13F (13f.info) --------------------------------------------------------

_QUARTER_RE = re.compile(r"Q[1-4]\s*\d{4}")


def fetch_13f_top_funds(symbols: Iterable[str]) -> list[dict]:
    """Scrape 13f.info for top funds' latest holdings touching our symbols.

    Returns one article per (fund, symbol) where the fund opened, added to,
    or trimmed a position in the latest filed quarter.
    """
    articles: list[dict] = []
    sym_set = {s.upper() for s in symbols}

    for cik, fund_name in TOP_FUNDS:
        url = THIRTEENF_FUND_URL.format(cik=cik)
        html = _http_get(url)
        if not html:
            continue
        try:
            soup = BeautifulSoup(html, "html.parser")
            # 13f.info renders the latest quarter holdings as a sortable table.
            # We pull rows where ticker matches our watchlist.
            table = soup.find("table")
            if not table:
                continue
            quarter_match = _QUARTER_RE.search(soup.get_text() or "")
            quarter = quarter_match.group(0) if quarter_match else "latest"
            for row in table.find_all("tr"):
                cells = [c.get_text(strip=True) for c in row.find_all("td")]
                if len(cells) < 5:
                    continue
                # Heuristic: the ticker appears in one of the first 3 columns.
                ticker_candidates = [c.upper() for c in cells[:3]
                                     if 1 <= len(c) <= 5 and c.isalpha()]
                ticker = next((t for t in ticker_candidates if t in sym_set), None)
                if not ticker:
                    continue
                # Last column usually carries % change in shares ("+12%", "NEW").
                change = cells[-1].strip() if cells else ""
                if not change or change == "—":
                    continue
                title = (f"{ticker} — 13F: {fund_name} {change} "
                         f"position ({quarter})")
                articles.append(_make_article(
                    title=title,
                    source=f"13F:{fund_name}",
                    source_url=url,
                    symbol=ticker,
                    description=f"13F filing for {quarter}.",
                    category="filings_13f",
                ))
        except Exception as e:
            log.debug(f"[sec_filings] 13F parse failed for {fund_name}: {e}")

    log.info(f"[sec_filings] 13F: {len(articles)} institutional filings for watchlist.")
    return articles


# --- Public entry point ----------------------------------------------------

def collect_sec_filings(
    symbols: Iterable[str], include_form4: bool = True, include_13f: bool = True,
    min_form4_dollar: float = 100_000,
) -> list[dict]:
    """Collect both Form 4 + 13F filings for the watchlist.

    Returns a deduped list (by title_hash) of articles ready to persist.
    """
    out: list[dict] = []
    if include_form4:
        out.extend(fetch_openinsider_recent(symbols, min_dollar=min_form4_dollar))
    if include_13f:
        out.extend(fetch_13f_top_funds(symbols))

    # In-batch dedup by title_hash (DB UNIQUE INDEX is the second line of defense)
    seen: set[str] = set()
    deduped: list[dict] = []
    for a in out:
        h = a["title_hash"]
        if h in seen:
            continue
        seen.add(h)
        deduped.append(a)
    return deduped
