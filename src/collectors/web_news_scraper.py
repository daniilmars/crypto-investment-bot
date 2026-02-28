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


def scrape_reuters_business():
    """Scrape Reuters business section for macro/financial headlines."""
    soup = _fetch_page('https://www.reuters.com/business/')
    if not soup:
        return []

    articles = []
    for tag in soup.select('a[href*="/business/"]'):
        headline = tag.get_text(strip=True)
        href = tag.get('href', '')
        if not headline or len(headline) < 15:
            continue
        url = href if href.startswith('http') else f'https://www.reuters.com{href}'
        articles.append({
            'title': headline,
            'description': '',
            'source': 'Reuters',
            'source_url': url,
        })
    return articles or _generic_article_fallback(soup, 'Reuters', 'https://www.reuters.com')


def scrape_marketwatch():
    """Scrape MarketWatch latest news for market headlines."""
    soup = _fetch_page('https://www.marketwatch.com/latest-news')
    if not soup:
        return []

    articles = []
    for tag in soup.select('a[href*="/story/"]'):
        headline = tag.get_text(strip=True)
        href = tag.get('href', '')
        if not headline or len(headline) < 15:
            continue
        url = href if href.startswith('http') else f'https://www.marketwatch.com{href}'
        articles.append({
            'title': headline,
            'description': '',
            'source': 'MarketWatch',
            'source_url': url,
        })
    return articles or _generic_article_fallback(soup, 'MarketWatch', 'https://www.marketwatch.com')


def scrape_yahoo_finance():
    """Scrape Yahoo Finance for stock/market headlines."""
    soup = _fetch_page('https://finance.yahoo.com/topic/stock-market-news/')
    if not soup:
        return []

    articles = []
    for tag in soup.select('a[href*="/news/"]'):
        headline = tag.get_text(strip=True)
        href = tag.get('href', '')
        if not headline or len(headline) < 15:
            continue
        url = href if href.startswith('http') else f'https://finance.yahoo.com{href}'
        articles.append({
            'title': headline,
            'description': '',
            'source': 'Yahoo Finance',
            'source_url': url,
        })
    return articles or _generic_article_fallback(soup, 'Yahoo Finance', 'https://finance.yahoo.com')


def scrape_theblock():
    """Scrape The Block for crypto news headlines."""
    soup = _fetch_page('https://www.theblock.co/latest')
    if not soup:
        return []

    articles = []
    seen = set()
    for tag in soup.select('a[href*="/post/"], h2, h3'):
        headline = tag.get_text(strip=True)
        if not headline or len(headline) < 20 or headline.lower() in seen:
            continue
        seen.add(headline.lower())
        articles.append({
            'title': headline,
            'description': '',
            'source': 'The Block',
            'source_url': 'https://www.theblock.co/latest',
        })
    return articles or _generic_article_fallback(soup, 'The Block', 'https://www.theblock.co')


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


ALL_SCRAPERS = [
    ('CoinDesk', scrape_coindesk),
    ('CoinTelegraph', scrape_cointelegraph),
    ('The Block', scrape_theblock),
    ('Decrypt', scrape_decrypt),
    ('Reuters', scrape_reuters_business),
    ('CNBC', scrape_cnbc),
    ('AP News', scrape_apnews),
    ('MarketWatch', scrape_marketwatch),
    ('Yahoo Finance', scrape_yahoo_finance),
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
            except Exception as e:
                log.warning(f"Web scraper [{source_name}] failed: {e}")

    log.info(f"Web scraping complete: {len(all_articles)} total articles from "
             f"{len(scrapers)} sources")
    return all_articles
