"""Deep article scraping for high-impact news categories.

Fetches full article body text for important articles (regulatory, kol, ipo),
giving Gemini richer context for direction/confidence assessments.

Uses a pluggable BodyExtractor ABC — default is BeautifulSoupExtractor.
When Chrome MCP is available, swap in a ChromeMCPExtractor with zero changes
to calling code.
"""

from abc import ABC, abstractmethod
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests
from bs4 import BeautifulSoup

from src.logger import log

IMPORTANT_CATEGORIES = {'regulatory', 'kol', 'ipo'}

MAX_BODY_CHARS = 2000
MIN_PARAGRAPH_CHARS = 20
FETCH_TIMEOUT = 10
ENRICHMENT_WORKERS = 4

# CSS selectors tried in priority order for article body extraction
BODY_SELECTORS = [
    'article',
    '[class*="article-body"]',
    '[class*="article-content"]',
    '[class*="post-content"]',
    '[class*="entry-content"]',
    '[class*="story-body"]',
    '.caas-body',            # Yahoo Finance
    '.article__body',        # Reuters
    '[class*="content-body"]',
    '[class*="page-content"]',
    'main',
]

USER_AGENT = (
    'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) '
    'AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
)


class BodyExtractor(ABC):
    """Interface for article body extraction."""

    @abstractmethod
    def extract(self, url: str) -> str | None:
        """Fetch and extract article body text from a URL.

        Returns extracted text (up to MAX_BODY_CHARS) or None on failure.
        """


class BeautifulSoupExtractor(BodyExtractor):
    """Extracts article body using requests + BeautifulSoup."""

    def extract(self, url: str) -> str | None:
        try:
            resp = requests.get(
                url,
                headers={'User-Agent': USER_AGENT},
                timeout=FETCH_TIMEOUT,
            )
            resp.raise_for_status()
            return self._extract_body(resp.text)
        except Exception as e:
            log.debug(f"BS4 extraction failed for {url}: {e}")
            return None

    def _extract_body(self, html: str) -> str | None:
        soup = BeautifulSoup(html, 'html.parser')

        # Remove noise elements
        for tag in soup.find_all(['script', 'style', 'nav', 'footer', 'aside', 'header']):
            tag.decompose()

        # Try selectors in priority order
        container = None
        for selector in BODY_SELECTORS:
            container = soup.select_one(selector)
            if container:
                break

        # Fallback: largest div with 3+ <p> children
        if not container:
            container = self._find_largest_text_div(soup)

        if not container:
            return None

        return self._extract_paragraphs(container)

    def _find_largest_text_div(self, soup: BeautifulSoup):
        best = None
        best_count = 0
        for div in soup.find_all('div'):
            p_tags = div.find_all('p', recursive=False)
            if len(p_tags) >= 3 and len(p_tags) > best_count:
                best = div
                best_count = len(p_tags)
        return best

    def _extract_paragraphs(self, container) -> str | None:
        paragraphs = []
        total = 0
        for p in container.find_all('p'):
            text = p.get_text(strip=True)
            if len(text) < MIN_PARAGRAPH_CHARS:
                continue
            paragraphs.append(text)
            total += len(text)
            if total >= MAX_BODY_CHARS:
                break

        if not paragraphs:
            return None

        body = '\n\n'.join(paragraphs)
        return body[:MAX_BODY_CHARS]


def is_important_article(article: dict) -> bool:
    """Returns True if the article's category qualifies for deep scraping."""
    category = article.get('category', '').lower().strip()
    return category in IMPORTANT_CATEGORIES


def enrich_article(article: dict, extractor: BodyExtractor | None = None) -> dict:
    """Fetches full article body and updates the description field.

    Sets article['_enriched'] = True on success for downstream tracking.
    Gracefully returns the original article on any failure.
    """
    url = article.get('source_url', '').strip()
    if not url or not url.startswith('http'):
        return article

    if extractor is None:
        extractor = BeautifulSoupExtractor()

    try:
        body = extractor.extract(url)
        if body:
            article['description'] = body
            article['_enriched'] = True
            log.debug(f"Enriched article: {article.get('title', '')[:60]}... ({len(body)} chars)")
    except Exception as e:
        log.debug(f"Enrichment failed for {url}: {e}")

    return article


def enrich_articles_batch(
    articles: list,
    extractor: BodyExtractor | None = None,
) -> list:
    """Enriches important articles with full body text in parallel.

    Filters for articles that:
    - Have a category in IMPORTANT_CATEGORIES
    - Have a non-empty source_url

    Returns the full article list (enriched + non-enriched).
    """
    if extractor is None:
        extractor = BeautifulSoupExtractor()

    to_enrich = [
        (i, a) for i, a in enumerate(articles)
        if is_important_article(a) and a.get('source_url', '').strip()
    ]

    if not to_enrich:
        return articles

    log.info(f"Deep scraping {len(to_enrich)} important articles...")

    def _enrich(idx_article):
        idx, article = idx_article
        return idx, enrich_article(article, extractor)

    with ThreadPoolExecutor(max_workers=ENRICHMENT_WORKERS) as executor:
        futures = {executor.submit(_enrich, item): item for item in to_enrich}
        for future in as_completed(futures, timeout=FETCH_TIMEOUT * len(to_enrich) + 10):
            try:
                idx, enriched = future.result(timeout=FETCH_TIMEOUT + 5)
                articles[idx] = enriched
            except Exception as e:
                item = futures[future]
                log.debug(f"Enrichment future failed for article index {item[0]}: {e}")

    enriched_count = sum(1 for a in articles if a.get('_enriched'))
    log.info(f"Deep scraping complete: {enriched_count}/{len(to_enrich)} articles enriched.")

    return articles
