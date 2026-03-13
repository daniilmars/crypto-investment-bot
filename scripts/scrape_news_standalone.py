#!/usr/bin/env python3
"""
Standalone news scraper — runs web scraping + RSS collection independently.

Outputs structured JSON to data/scraped-news.json for the bot to consume.
Can be run manually, via launchd, or cron.

Usage:
    .venv/bin/python scripts/scrape_news_standalone.py
    .venv/bin/python scripts/scrape_news_standalone.py --output /path/to/output.json
"""

import argparse
import json
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.collectors.web_news_scraper import scrape_all_sources
from src.collectors.news_data import (
    _fetch_rss_feeds, _deduplicate_articles, _match_article_to_symbols,
)
from src.database import save_articles_batch, compute_title_hash, initialize_database
from src.logger import log

DEFAULT_OUTPUT = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    'data', 'scraped-news.json'
)

CRYPTO_SYMBOLS = ['BTC', 'ETH', 'SOL', 'XRP', 'ADA', 'AVAX', 'DOGE', 'MATIC', 'BNB', 'TRX']
STOCK_SYMBOLS = ['AAPL', 'MSFT', 'GOOGL', 'AMZN', 'NVDA', 'META', 'TSLA']
ALL_SYMBOLS = CRYPTO_SYMBOLS + STOCK_SYMBOLS


def run_scraper(output_path=None, archive=False):
    """Scrape news from all sources, match to symbols, write JSON."""
    output_path = output_path or DEFAULT_OUTPUT

    log.info("=== Standalone News Scraper ===")

    # 1. Collect from RSS feeds + web scrapers
    rss_articles = _fetch_rss_feeds()
    web_articles = scrape_all_sources()
    all_articles = _deduplicate_articles(rss_articles + web_articles)

    log.info(f"Total unique articles: {len(all_articles)}")

    if not all_articles:
        log.warning("No articles found. Writing empty output.")
        _write_output(output_path, [], {})
        return

    # 2. Match to symbols
    symbol_articles = {sym: [] for sym in ALL_SYMBOLS}

    for article in all_articles:
        title = article.get('title', '')
        description = article.get('description', '')
        matched = _match_article_to_symbols(title, description, ALL_SYMBOLS)

        if not matched:
            continue

        for sym in matched:
            symbol_articles[sym].append({
                'headline': title,
                'source': article.get('source', 'Unknown'),
                'source_url': article.get('source_url', ''),
            })

    # 3. Build per-symbol summaries
    per_symbol = {}
    for sym in ALL_SYMBOLS:
        articles = symbol_articles[sym]
        if not articles:
            continue

        per_symbol[sym] = {
            'category': 'crypto' if sym in CRYPTO_SYMBOLS else 'stocks',
            'article_count': len(articles),
            'headlines': [a['headline'] for a in articles[:10]],
            'articles': articles[:15],
        }

    log.info(f"Symbols with news: {list(per_symbol.keys())}")

    # Archive to DB if requested
    if archive:
        log.info("Archiving articles to database...")
        initialize_database()
        archive_rows = []
        for sym, data in per_symbol.items():
            for article in data.get('articles', []):
                title = article.get('headline', '')
                if title:
                    archive_rows.append({
                        'title': title,
                        'title_hash': compute_title_hash(title),
                        'source': article.get('source', ''),
                        'source_url': article.get('source_url', ''),
                        'description': '',
                        'symbol': sym,
                        'category': '',
                    })
        if archive_rows:
            save_articles_batch(archive_rows)
            log.info(f"Archived {len(archive_rows)} articles to DB.")
        else:
            log.info("No articles to archive.")

    _write_output(output_path, all_articles, per_symbol)


def _write_output(output_path, all_articles, per_symbol):
    """Write structured JSON output."""
    os.makedirs(os.path.dirname(output_path), exist_ok=True)

    output = {
        'scraped_at': datetime.now(timezone.utc).isoformat(),
        'total_articles': len(all_articles),
        'per_symbol': per_symbol,
    }

    with open(output_path, 'w') as f:
        json.dump(output, f, indent=2)

    log.info(f"Output written to {output_path}")


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Standalone news scraper')
    parser.add_argument('--output', '-o', default=DEFAULT_OUTPUT,
                        help='Output JSON path (default: data/scraped-news.json)')
    parser.add_argument('--archive', action='store_true',
                        help='Also save articles to the scraped_articles DB table')
    args = parser.parse_args()
    run_scraper(args.output, archive=args.archive)
