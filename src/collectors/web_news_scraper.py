"""
Web news scraper that supplements RSS feeds with direct page scraping.

Uses requests + BeautifulSoup to extract headlines from financial news sites.
Zero API cost — all parsing is done locally.
"""

import random
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests
from bs4 import BeautifulSoup

from src.logger import log

SCRAPE_TIMEOUT = 15

_USER_AGENTS = [
    'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36',
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36',
    'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36',
    'Mozilla/5.0 (Macintosh; Intel Mac OS X 14_0) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Safari/605.1.15',
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:121.0) Gecko/20100101 Firefox/121.0',
]


def _get_headers():
    """Return request headers with a randomly selected User-Agent."""
    return {
        'User-Agent': random.choice(_USER_AGENTS),
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
        'Accept-Language': 'en-US,en;q=0.9',
    }


def _fetch_page(url):
    """Fetch a page and return a BeautifulSoup object, or None on failure."""
    try:
        resp = requests.get(url, headers=_get_headers(), timeout=SCRAPE_TIMEOUT)
        resp.raise_for_status()
        return BeautifulSoup(resp.text, 'html.parser')
    except Exception as e:
        log.warning(f"Failed to fetch {url}: {e}")
        return None


def _generic_article_fallback(soup, source_name, base_url):
    """Generic fallback scraper: extracts headlines from common HTML patterns."""
    articles = []
    seen = set()
    selectors = ['h1 a', 'h2 a', 'h3 a', 'article a', '[class*="headline"] a']
    for selector in selectors:
        for tag in soup.select(selector):
            headline = tag.get_text(strip=True)
            if not headline or len(headline) < 15 or headline.lower() in seen:
                continue
            seen.add(headline.lower())
            href = tag.get('href', '')
            url = href if href.startswith('http') else f'{base_url.rstrip("/")}/{href.lstrip("/")}'
            articles.append({
                'title': headline,
                'description': '',
                'source': source_name,
                'source_url': url,
            })
    return articles


# --- Individual site scrapers ---


def scrape_coindesk():
    """Scrape CoinDesk front page for crypto headlines."""
    soup = _fetch_page('https://www.coindesk.com/')
    if not soup:
        return []

    articles = []
    for tag in soup.select('a[href*="/article/"], a[href*="/markets/"], a[href*="/policy/"]'):
        headline = tag.get_text(strip=True)
        href = tag.get('href', '')
        if not headline or len(headline) < 15:
            continue
        url = href if href.startswith('http') else f'https://www.coindesk.com{href}'
        articles.append({
            'title': headline,
            'description': '',
            'source': 'CoinDesk',
            'source_url': url,
        })
    return articles or _generic_article_fallback(soup, 'CoinDesk', 'https://www.coindesk.com')


def scrape_cointelegraph():
    """Scrape CoinTelegraph for crypto headlines."""
    soup = _fetch_page('https://cointelegraph.com/')
    if not soup:
        return []

    articles = []
    for tag in soup.select('a[href*="/news/"]'):
        headline = tag.get_text(strip=True)
        href = tag.get('href', '')
        if not headline or len(headline) < 15:
            continue
        url = href if href.startswith('http') else f'https://cointelegraph.com{href}'
        articles.append({
            'title': headline,
            'description': '',
            'source': 'CoinTelegraph',
            'source_url': url,
        })
    return articles or _generic_article_fallback(soup, 'CoinTelegraph', 'https://cointelegraph.com')


def scrape_decrypt():
    """Scrape Decrypt news page for crypto headlines."""
    soup = _fetch_page('https://decrypt.co/news')
    if not soup:
        return []

    articles = []
    for tag in soup.select('h2, h3, h4, [class*="article"] a, [class*="post"] a, [class*="card"] a'):
        headline = tag.get_text(strip=True)
        if not headline or len(headline) < 25 or len(headline) > 250:
            continue
        articles.append({
            'title': headline,
            'description': '',
            'source': 'Decrypt',
            'source_url': 'https://decrypt.co/news',
        })
    return articles or _generic_article_fallback(soup, 'Decrypt', 'https://decrypt.co')




def scrape_cnbc():
    """Scrape CNBC world/markets page for stock and macro headlines."""
    soup = _fetch_page('https://www.cnbc.com/world/?region=world')
    if not soup:
        return []

    articles = []
    seen = set()
    for tag in soup.select('a[href*="/2026/"], a[href*="/2025/"], h2, h3'):
        headline = tag.get_text(strip=True)
        if not headline or len(headline) < 25 or len(headline) > 250:
            continue
        if headline.lower() in seen:
            continue
        seen.add(headline.lower())
        articles.append({
            'title': headline,
            'description': '',
            'source': 'CNBC',
            'source_url': 'https://www.cnbc.com/world/',
        })
    return articles or _generic_article_fallback(soup, 'CNBC', 'https://www.cnbc.com')


def scrape_apnews():
    """Scrape AP News business section for macro/business headlines."""
    soup = _fetch_page('https://apnews.com/hub/business')
    if not soup:
        return []

    articles = []
    seen = set()
    for tag in soup.select('h2, h3, [class*="PagePromo"] span'):
        headline = tag.get_text(strip=True)
        if not headline or len(headline) < 25 or len(headline) > 250:
            continue
        if headline.lower() in seen:
            continue
        seen.add(headline.lower())
        articles.append({
            'title': headline,
            'description': '',
            'source': 'AP News',
            'source_url': 'https://apnews.com/hub/business',
        })
    return articles or _generic_article_fallback(soup, 'AP News', 'https://apnews.com')


def scrape_techcrunch_ai():
    """Scrape TechCrunch AI category for AI industry headlines."""
    soup = _fetch_page('https://techcrunch.com/category/artificial-intelligence/')
    if not soup:
        return []

    articles = []
    seen = set()
    for tag in soup.select('a[href*="/2026/"], a[href*="/2025/"]'):
        headline = tag.get_text(strip=True)
        href = tag.get('href', '')
        if not headline or len(headline) < 25 or len(headline) > 250:
            continue
        if headline.lower() in seen:
            continue
        seen.add(headline.lower())
        url = href if href.startswith('http') else f'https://techcrunch.com{href}'
        articles.append({
            'title': headline,
            'description': '',
            'source': 'TechCrunch AI',
            'source_url': url,
            'category': 'ai',
        })
    return articles or _generic_article_fallback(soup, 'TechCrunch AI', 'https://techcrunch.com')




# --- Scraper health tracking ---

_HEALTH_WINDOW = 10  # keep last N run counts per source
_scraper_health: dict[str, list[int]] = {}


def _update_scraper_health(source_name: str, article_count: int):
    """Record article count for a source, trimming to window size."""
    history = _scraper_health.setdefault(source_name, [])
    history.append(article_count)
    if len(history) > _HEALTH_WINDOW:
        _scraper_health[source_name] = history[-_HEALTH_WINDOW:]


def _check_scraper_health() -> list[tuple[str, float]]:
    """Check for degraded scrapers and log warnings.

    Alerts when a source that historically averaged 5+ articles
    has returned 0 for 3+ consecutive recent runs.

    Returns list of (source_name, historical_avg) for degraded sources.
    """
    degraded = []
    for source, history in _scraper_health.items():
        if len(history) < 4:
            continue

        # Check last 3 runs are all zero
        if any(c > 0 for c in history[-3:]):
            continue

        # Check if historical average (excluding last 3) was 5+
        older = history[:-3]
        if not older:
            continue
        avg = sum(older) / len(older)
        if avg >= 5.0:
            degraded.append((source, avg))
            log.warning(
                f"Scraper health alert: [{source}] returned 0 articles for "
                f"3 consecutive runs (historical avg: {avg:.1f}). "
                f"CSS selectors may need updating."
            )

    return degraded


def get_scraper_health() -> dict:
    """Return current health status for all tracked scrapers.

    Returns dict of {source_name: {health, last_count, avg_historical, avg_recent, runs_tracked}}.
    """
    status = {}
    for source, history in _scraper_health.items():
        recent = history[-3:] if len(history) >= 3 else history
        older = history[:-3] if len(history) > 3 else []
        avg_historical = sum(older) / len(older) if older else 0
        avg_recent = sum(recent) / len(recent) if recent else 0

        health = 'healthy'
        if len(history) >= 4 and all(c == 0 for c in recent) and avg_historical >= 5:
            health = 'degraded'
        elif recent and all(c == 0 for c in recent):
            health = 'warning'

        status[source] = {
            'health': health,
            'last_count': history[-1] if history else 0,
            'avg_historical': round(avg_historical, 1),
            'avg_recent': round(avg_recent, 1),
            'runs_tracked': len(history),
        }
    return status


ALL_SCRAPERS = [
    ('CoinDesk', scrape_coindesk),
    ('CoinTelegraph', scrape_cointelegraph),
    ('Decrypt', scrape_decrypt),
    ('CNBC', scrape_cnbc),
    ('AP News', scrape_apnews),
    ('TechCrunch AI', scrape_techcrunch_ai),
]


def scrape_all_sources(enabled_sources=None):
    """
    Run all web scrapers in parallel and return combined articles.

    Args:
        enabled_sources: optional list of source names to scrape.
                         If None, scrapes all sources.

    Returns:
        list of article dicts: [{title, description, source, source_url}, ...]
    """
    scrapers = ALL_SCRAPERS
    if enabled_sources:
        enabled_set = {s.lower() for s in enabled_sources}
        scrapers = [(name, fn) for name, fn in ALL_SCRAPERS
                     if name.lower() in enabled_set]

    all_articles = []
    with ThreadPoolExecutor(max_workers=6) as executor:
        futures = {executor.submit(fn): name for name, fn in scrapers}
        for future in as_completed(futures, timeout=SCRAPE_TIMEOUT + 10):
            source_name = futures[future]
            try:
                articles = future.result(timeout=SCRAPE_TIMEOUT)
                log.info(f"Web scraper [{source_name}]: {len(articles)} articles")
                all_articles.extend(articles)
                _update_scraper_health(source_name, len(articles))
            except Exception as e:
                log.warning(f"Web scraper [{source_name}] failed: {e}")
                _update_scraper_health(source_name, 0)

    _check_scraper_health()

    log.info(f"Web scraping complete: {len(all_articles)} total articles from "
             f"{len(scrapers)} sources")
    return all_articles
