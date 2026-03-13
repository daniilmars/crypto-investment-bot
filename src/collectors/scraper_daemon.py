"""Continuous news scraper daemon with tiered collection intervals.

Runs as a long-lived process with separate threads for:
  - RSS feed collection (default: every 5 min)
  - Web page scraping (default: every 15 min)
  - Deep article enrichment (default: every 30 min)
  - Dedup cache cleanup (every 60 min)

Output: data/scraped-news.json (atomic write, same schema as standalone scraper)
        + DB archive via save_articles_batch()
        + data/scraper-daemon-status.json (metrics)
"""

import json
import os
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone

from src.collectors.news_data import (
    _fetch_rss_feeds, _match_article_to_symbols,
)
from src.collectors.web_news_scraper import scrape_all_sources
from src.collectors.article_enricher import enrich_articles_batch
from src.database import save_articles_batch, compute_title_hash, initialize_database
from src.logger import log

CRYPTO_SYMBOLS = ['BTC', 'ETH', 'SOL', 'XRP', 'ADA', 'AVAX', 'DOGE', 'MATIC', 'BNB', 'TRX']
STOCK_SYMBOLS = [
    'AAPL', 'MSFT', 'GOOGL', 'AMZN', 'NVDA', 'META', 'TSLA', 'AVGO', 'CRM', 'ORCL',
    'AMD', 'ADBE', 'INTC', 'JPM', 'BAC', 'GS', 'MS', 'V', 'MA', 'BRK-B', 'WFC',
    'UNH', 'JNJ', 'LLY', 'PFE', 'ABBV', 'MRK', 'TMO', 'XOM', 'CVX', 'COP', 'SLB',
    'HD', 'MCD', 'NKE', 'SBUX', 'WMT', 'COST', 'KO', 'CAT', 'BA', 'GE', 'HON', 'RTX',
    'DIS', 'NFLX', 'CMCSA', 'NEE', 'SO', 'AMT',
]
ALL_SYMBOLS = CRYPTO_SYMBOLS + STOCK_SYMBOLS


@dataclass
class DaemonMetrics:
    """Tracks runtime metrics for the scraper daemon."""
    started_at: str = ''
    articles_total: int = 0
    rss_cycles: int = 0
    web_cycles: int = 0
    deep_cycles: int = 0
    cleanup_cycles: int = 0
    last_rss_success: str = ''
    last_web_success: str = ''
    last_deep_success: str = ''
    dedup_cache_size: int = 0
    chrome_mcp_successes: int = 0
    chrome_mcp_failures: int = 0
    last_chrome_mcp_source: str = ''
    errors: list = field(default_factory=list)

    def to_dict(self):
        return {
            'started_at': self.started_at,
            'articles_total': self.articles_total,
            'rss_cycles': self.rss_cycles,
            'web_cycles': self.web_cycles,
            'deep_cycles': self.deep_cycles,
            'cleanup_cycles': self.cleanup_cycles,
            'last_rss_success': self.last_rss_success,
            'last_web_success': self.last_web_success,
            'last_deep_success': self.last_deep_success,
            'dedup_cache_size': self.dedup_cache_size,
            'chrome_mcp_successes': self.chrome_mcp_successes,
            'chrome_mcp_failures': self.chrome_mcp_failures,
            'last_chrome_mcp_source': self.last_chrome_mcp_source,
            'last_errors': self.errors[-5:],
        }


class ScraperDaemon:
    """Long-running news scraper with tiered collection intervals."""

    def __init__(self, output_path=None, config=None):
        config = config or {}
        self.rss_interval = config.get('rss_interval_seconds', 300)
        self.web_interval = config.get('web_interval_seconds', 900)
        self.deep_interval = config.get('deep_interval_seconds', 1800)
        self.dedup_cache_hours = config.get('dedup_cache_hours', 48)

        # Chrome MCP config
        chrome_mcp_config = config.get('chrome_mcp', {})
        self.chrome_mcp_enabled = chrome_mcp_config.get('enabled', False)
        self.chrome_mcp_timeout = chrome_mcp_config.get('timeout_seconds', 180)
        self.chrome_mcp_fallback = chrome_mcp_config.get('fallback_to_bs4', True)

        project_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        self.output_path = output_path or os.path.join(project_root, 'data', 'scraped-news.json')
        self.metrics_path = os.path.join(os.path.dirname(self.output_path), 'scraper-daemon-status.json')
        self.chrome_scraped_path = os.path.join(project_root, 'data', 'chrome-scraped.json')
        self.chrome_mcp_prompt_path = os.path.join(project_root, 'scripts', 'chrome_mcp_scraper_prompt.md')

        self.shutdown_event = threading.Event()
        self._lock = threading.Lock()

        # Dedup cache: lowercased title -> timestamp
        self._seen_titles: dict[str, float] = {}
        # Accumulated articles keyed by symbol
        self._symbol_articles: dict[str, list] = {sym: [] for sym in ALL_SYMBOLS}
        # Raw articles pending deep enrichment
        self._pending_enrichment: list = []

        self.metrics = DaemonMetrics()
        self._threads: list[threading.Thread] = []

    def start(self):
        """Initialize DB, spawn worker threads, block until shutdown."""
        try:
            initialize_database()
        except Exception as e:
            log.warning(f"Database initialization skipped: {e}")

        self.metrics.started_at = datetime.now(timezone.utc).isoformat()
        log.info("Scraper daemon starting...")
        log.info(f"  RSS interval: {self.rss_interval}s, Web: {self.web_interval}s, "
                 f"Deep: {self.deep_interval}s, Dedup TTL: {self.dedup_cache_hours}h")

        thread_targets = [
            ('rss', self._rss_loop),
            ('web', self._web_loop),
            ('deep', self._deep_loop),
            ('cleanup', self._cache_cleanup_loop),
        ]

        for name, target in thread_targets:
            t = threading.Thread(target=target, name=f'scraper-{name}', daemon=True)
            t.start()
            self._threads.append(t)

        log.info(f"Scraper daemon running with {len(self._threads)} threads.")
        self.shutdown_event.wait()
        log.info("Scraper daemon shutdown signal received.")

    def stop(self):
        """Signal all threads to stop and wait for them."""
        log.info("Scraper daemon stopping...")
        self.shutdown_event.set()
        for t in self._threads:
            t.join(timeout=30)
            if t.is_alive():
                log.warning(f"Thread {t.name} did not exit within 30s.")
        log.info("Scraper daemon stopped.")

    # --- Worker loops ---

    def _rss_loop(self):
        """Collect RSS feeds on a regular interval."""
        while not self.shutdown_event.is_set():
            try:
                log.info("[RSS] Fetching feeds...")
                articles = _fetch_rss_feeds()
                new_count = self._process_articles(articles, 'rss')
                self.metrics.rss_cycles += 1
                self.metrics.last_rss_success = datetime.now(timezone.utc).isoformat()
                log.info(f"[RSS] Cycle {self.metrics.rss_cycles}: "
                         f"{len(articles)} fetched, {new_count} new")
            except Exception as e:
                log.error(f"[RSS] Error: {e}")
                self.metrics.errors.append(f"rss: {e}")
            self.shutdown_event.wait(timeout=self.rss_interval)

    def _web_loop(self):
        """Scrape web sources on a regular interval, using Chrome MCP if enabled."""
        while True:
            try:
                articles = []
                used_chrome = False

                if self.chrome_mcp_enabled:
                    log.info("[Web] Scraping via Chrome MCP...")
                    articles = self._scrape_via_chrome_mcp()
                    if articles:
                        used_chrome = True
                        self.metrics.chrome_mcp_successes += 1
                        self.metrics.last_chrome_mcp_source = 'chrome_mcp'
                        log.info(f"[Web] Chrome MCP returned {len(articles)} articles")
                    else:
                        self.metrics.chrome_mcp_failures += 1
                        if self.chrome_mcp_fallback:
                            log.warning("[Web] Chrome MCP returned nothing, falling back to BS4")
                            articles = scrape_all_sources()
                        else:
                            log.warning("[Web] Chrome MCP returned nothing, no fallback configured")
                else:
                    log.info("[Web] Scraping sources via BS4...")
                    articles = scrape_all_sources()

                new_count = self._process_articles(articles, 'web')
                self.metrics.web_cycles += 1
                self.metrics.last_web_success = datetime.now(timezone.utc).isoformat()
                source_label = 'Chrome MCP' if used_chrome else 'BS4'
                log.info(f"[Web] Cycle {self.metrics.web_cycles} ({source_label}): "
                         f"{len(articles)} fetched, {new_count} new")
            except Exception as e:
                log.error(f"[Web] Error: {e}")
                self.metrics.errors.append(f"web: {e}")
            if self.shutdown_event.wait(timeout=self.web_interval):
                break

    def _scrape_via_chrome_mcp(self):
        """Run Chrome MCP scraper as a subprocess, return parsed articles or []."""
        # Pre-flight checks
        if not os.path.exists(self.chrome_mcp_prompt_path):
            log.warning(f"[ChromeMCP] Prompt file not found: {self.chrome_mcp_prompt_path}")
            return []

        if not shutil.which('claude'):
            log.warning("[ChromeMCP] 'claude' CLI not found in PATH")
            return []

        # Delete stale sidecar to avoid reading old data
        if os.path.exists(self.chrome_scraped_path):
            try:
                os.remove(self.chrome_scraped_path)
            except OSError:
                pass

        try:
            with open(self.chrome_mcp_prompt_path, 'r') as f:
                prompt = f.read()

            result = subprocess.run(
                ['claude', '-p', prompt, '--allowedTools', 'mcp__claude-in-chrome__*,Write,Read'],
                timeout=self.chrome_mcp_timeout,
                capture_output=True,
                text=True,
            )

            if result.returncode != 0:
                log.warning(f"[ChromeMCP] Process exited with code {result.returncode}")
                if result.stderr:
                    log.warning(f"[ChromeMCP] stderr: {result.stderr[:500]}")
                return []

            return self._read_chrome_mcp_sidecar()

        except subprocess.TimeoutExpired:
            log.warning(f"[ChromeMCP] Timed out after {self.chrome_mcp_timeout}s")
            return []
        except Exception as e:
            log.warning(f"[ChromeMCP] Error: {e}")
            return []

    def _read_chrome_mcp_sidecar(self):
        """Read and normalize articles from the Chrome MCP sidecar JSON file."""
        if not os.path.exists(self.chrome_scraped_path):
            log.warning("[ChromeMCP] Sidecar file not found after scrape")
            return []

        try:
            with open(self.chrome_scraped_path, 'r') as f:
                data = json.load(f)
        except (json.JSONDecodeError, OSError) as e:
            log.warning(f"[ChromeMCP] Failed to read sidecar: {e}")
            return []

        raw_articles = data.get('articles', [])
        normalized = []

        for item in raw_articles:
            if isinstance(item, str):
                # Legacy format: plain headline string
                title = item.strip()
                source = data.get('source', 'Chrome MCP')
                source_url = ''
                description = ''
            elif isinstance(item, dict):
                title = (item.get('title') or item.get('headline') or '').strip()
                source = item.get('source', 'Chrome MCP')
                source_url = item.get('source_url') or item.get('url') or ''
                description = (item.get('description') or item.get('summary') or '').strip()
            else:
                continue

            if len(title) < 10:
                continue

            normalized.append({
                'title': title,
                'description': description,
                'source': source,
                'source_url': source_url,
            })

        # Also pull in detailed_articles if present
        for detail in data.get('detailed_articles', []):
            if not isinstance(detail, dict):
                continue
            title = (detail.get('title') or '').strip()
            if len(title) < 10:
                continue
            # Only add if not already present
            if any(a['title'].lower() == title.lower() for a in normalized):
                continue
            normalized.append({
                'title': title,
                'description': (detail.get('summary') or detail.get('description') or '').strip(),
                'source': detail.get('source', 'Chrome MCP'),
                'source_url': detail.get('source_url') or detail.get('url') or '',
            })

        log.info(f"[ChromeMCP] Parsed {len(normalized)} articles from sidecar")
        return normalized

    def _deep_loop(self):
        """Enrich un-enriched articles with full body text."""
        while not self.shutdown_event.is_set():
            try:
                with self._lock:
                    batch = list(self._pending_enrichment)
                    self._pending_enrichment.clear()

                if batch:
                    log.info(f"[Deep] Enriching {len(batch)} articles...")
                    enriched = enrich_articles_batch(batch)
                    enriched_count = sum(1 for a in enriched if a.get('_enriched'))
                    log.info(f"[Deep] Enriched {enriched_count}/{len(batch)} articles")

                self.metrics.deep_cycles += 1
                self.metrics.last_deep_success = datetime.now(timezone.utc).isoformat()
            except Exception as e:
                log.error(f"[Deep] Error: {e}")
                self.metrics.errors.append(f"deep: {e}")
            self.shutdown_event.wait(timeout=self.deep_interval)

    def _cache_cleanup_loop(self):
        """Evict old entries from the dedup cache."""
        while not self.shutdown_event.is_set():
            try:
                cutoff = time.time() - (self.dedup_cache_hours * 3600)
                with self._lock:
                    before = len(self._seen_titles)
                    self._seen_titles = {
                        k: v for k, v in self._seen_titles.items() if v > cutoff
                    }
                    evicted = before - len(self._seen_titles)
                self.metrics.cleanup_cycles += 1
                if evicted > 0:
                    log.info(f"[Cleanup] Evicted {evicted} stale titles from dedup cache")
            except Exception as e:
                log.error(f"[Cleanup] Error: {e}")
                self.metrics.errors.append(f"cleanup: {e}")
            self.shutdown_event.wait(timeout=3600)

    # --- Core processing ---

    def _process_articles(self, articles, tier):
        """Dedup, match to symbols, write output + archive to DB.

        Returns the number of new (non-duplicate) articles processed.
        """
        if not articles:
            return 0

        now = time.time()
        new_articles = []

        with self._lock:
            for article in articles:
                key = article.get('title', '').lower().strip()
                if not key or key in self._seen_titles:
                    continue
                self._seen_titles[key] = now
                new_articles.append(article)

        if not new_articles:
            return 0

        # Queue important articles for deep enrichment
        important = [a for a in new_articles if a.get('category', '') in {'regulatory', 'kol', 'ipo'}]
        if important:
            with self._lock:
                self._pending_enrichment.extend(important)

        # Match to symbols and archive
        archive_rows = []
        with self._lock:
            for article in new_articles:
                title = article.get('title', '')
                description = article.get('description', '')
                matched = _match_article_to_symbols(title, description, ALL_SYMBOLS)

                if not matched:
                    continue

                for sym in matched:
                    entry = {
                        'headline': title,
                        'source': article.get('source', 'Unknown'),
                        'source_url': article.get('source_url', ''),
                    }
                    self._symbol_articles[sym].append(entry)
                    # Keep only last 15 per symbol
                    if len(self._symbol_articles[sym]) > 15:
                        self._symbol_articles[sym] = self._symbol_articles[sym][-15:]

                    archive_rows.append({
                        'title': title,
                        'title_hash': compute_title_hash(title),
                        'source': article.get('source', ''),
                        'source_url': article.get('source_url', ''),
                        'description': description,
                        'symbol': sym,
                        'category': article.get('category', ''),
                    })

            self.metrics.articles_total += len(new_articles)
            self.metrics.dedup_cache_size = len(self._seen_titles)

        # Archive to DB
        self._archive_to_db(archive_rows)

        # Write output JSON
        self._write_output()

        # Write metrics
        self._write_metrics()

        return len(new_articles)


    def _write_output(self):
        """Atomically write per-symbol JSON output."""
        with self._lock:
            per_symbol = {}
            for sym in ALL_SYMBOLS:
                articles = self._symbol_articles[sym]
                if not articles:
                    continue
                per_symbol[sym] = {
                    'category': 'crypto' if sym in CRYPTO_SYMBOLS else 'stocks',
                    'article_count': len(articles),
                    'headlines': [a['headline'] for a in articles[:10]],
                    'articles': articles[:15],
                }
            total = sum(d['article_count'] for d in per_symbol.values())

        output = {
            'scraped_at': datetime.now(timezone.utc).isoformat(),
            'total_articles': total,
            'per_symbol': per_symbol,
        }

        os.makedirs(os.path.dirname(self.output_path), exist_ok=True)
        tmp_path = self.output_path + '.tmp'
        with open(tmp_path, 'w') as f:
            json.dump(output, f, indent=2)
        os.replace(tmp_path, self.output_path)

    def _archive_to_db(self, scored_rows):
        """Save scored articles to the database."""
        if not scored_rows:
            return
        try:
            save_articles_batch(scored_rows)
        except Exception as e:
            log.warning(f"Failed to archive articles to DB: {e}")
            self.metrics.errors.append(f"db_archive: {e}")

    def _write_metrics(self):
        """Write daemon status/metrics JSON."""
        try:
            metrics_data = self.metrics.to_dict()
            metrics_data['updated_at'] = datetime.now(timezone.utc).isoformat()
            os.makedirs(os.path.dirname(self.metrics_path), exist_ok=True)
            tmp_path = self.metrics_path + '.tmp'
            with open(tmp_path, 'w') as f:
                json.dump(metrics_data, f, indent=2)
            os.replace(tmp_path, self.metrics_path)
        except Exception as e:
            log.warning(f"Failed to write metrics: {e}")
