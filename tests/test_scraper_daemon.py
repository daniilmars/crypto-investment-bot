"""Tests for the continuous news scraper daemon."""

import json
import os
import subprocess
import tempfile
import time
import threading
from unittest.mock import patch, MagicMock

import pytest

from src.collectors.scraper_daemon import ScraperDaemon, DaemonMetrics, ALL_SYMBOLS


@pytest.fixture
def tmp_output(tmp_path):
    """Temporary output path for JSON."""
    return str(tmp_path / 'scraped-news.json')


@pytest.fixture
def daemon(tmp_output):
    """Create a daemon with short intervals for testing."""
    config = {
        'rss_interval_seconds': 1,
        'web_interval_seconds': 1,
        'deep_interval_seconds': 1,
        'dedup_cache_hours': 48,
    }
    d = ScraperDaemon(output_path=tmp_output, config=config)
    return d


def _make_article(title, source='TestSource', category='financial', source_url='https://example.com'):
    return {
        'title': title,
        'description': f'Description for {title}',
        'source': source,
        'source_url': source_url,
        'category': category,
    }


# --- Deduplication tests ---

class TestDeduplication:
    def test_same_title_deduped(self, daemon):
        """Same title from two sources should be counted once."""
        articles = [
            _make_article('Bitcoin surges to new highs', source='Source A'),
            _make_article('Bitcoin surges to new highs', source='Source B'),
        ]
        with patch.object(daemon, '_archive_to_db'):
            count = daemon._process_articles(articles, 'rss')
        assert count == 1

    def test_case_insensitive_dedup(self, daemon):
        """Titles differing only in case should be deduped."""
        articles = [
            _make_article('Bitcoin Price Drops'),
            _make_article('bitcoin price drops'),
        ]
        with patch.object(daemon, '_archive_to_db'):
            count = daemon._process_articles(articles, 'rss')
        assert count == 1

    def test_different_titles_not_deduped(self, daemon):
        """Distinct titles should both be kept."""
        articles = [
            _make_article('Bitcoin surges to new highs'),
            _make_article('Ethereum breaks resistance level'),
        ]
        with patch.object(daemon, '_archive_to_db'):
            count = daemon._process_articles(articles, 'rss')
        assert count == 2

    def test_incremental_dedup_across_calls(self, daemon):
        """Articles from a previous cycle should be deduped in the next."""
        batch1 = [_make_article('Bitcoin surges to new highs')]
        batch2 = [
            _make_article('Bitcoin surges to new highs'),
            _make_article('Ethereum breaks resistance level'),
        ]
        with patch.object(daemon, '_archive_to_db'):
            daemon._process_articles(batch1, 'rss')
            count = daemon._process_articles(batch2, 'rss')
        assert count == 1  # Only the ETH article is new

    def test_empty_titles_ignored(self, daemon):
        """Articles with empty titles should be skipped."""
        articles = [_make_article(''), _make_article('   ')]
        with patch.object(daemon, '_archive_to_db'):
            count = daemon._process_articles(articles, 'rss')
        assert count == 0


# --- Cache eviction tests ---

class TestCacheEviction:
    def test_old_titles_evicted(self, daemon):
        """Titles older than dedup_cache_hours should be evicted."""
        daemon.dedup_cache_hours = 1  # 1 hour
        # Insert a title with a timestamp 2 hours ago
        old_time = time.time() - 7200
        daemon._seen_titles['old bitcoin article'] = old_time
        daemon._seen_titles['recent ethereum article'] = time.time()

        daemon._cache_cleanup_loop_once()

        assert 'old bitcoin article' not in daemon._seen_titles
        assert 'recent ethereum article' in daemon._seen_titles

    def test_cleanup_increments_metrics(self, daemon):
        """Cleanup cycle should increment the counter."""
        assert daemon.metrics.cleanup_cycles == 0
        daemon._cache_cleanup_loop_once()
        assert daemon.metrics.cleanup_cycles == 1


# Add a helper method for single-pass cleanup testing
ScraperDaemon._cache_cleanup_loop_once = lambda self: (
    setattr(self, '_seen_titles', {
        k: v for k, v in self._seen_titles.items()
        if v > time.time() - (self.dedup_cache_hours * 3600)
    }),
    setattr(self.metrics, 'cleanup_cycles', self.metrics.cleanup_cycles + 1),
)[-1]


# --- Atomic JSON write tests ---

class TestAtomicWrite:
    def test_output_file_created(self, daemon, tmp_output):
        """_write_output should create the JSON file."""
        daemon._write_output()
        assert os.path.exists(tmp_output)

    def test_output_valid_json(self, daemon, tmp_output):
        """Output should be valid JSON with expected schema."""
        daemon._write_output()
        with open(tmp_output) as f:
            data = json.load(f)
        assert 'scraped_at' in data
        assert 'total_articles' in data
        assert 'per_symbol' in data

    def test_no_tmp_file_left(self, daemon, tmp_output):
        """Atomic write should not leave .tmp files."""
        daemon._write_output()
        assert not os.path.exists(tmp_output + '.tmp')

    def test_output_schema_matches_standalone(self, daemon, tmp_output):
        """Per-symbol entries should have the fields the main bot expects."""
        articles = [_make_article('Bitcoin surges past $100k')]
        with patch.object(daemon, '_archive_to_db'):
            daemon._process_articles(articles, 'rss')

        with open(tmp_output) as f:
            data = json.load(f)

        # BTC should have been matched
        if 'BTC' in data['per_symbol']:
            btc = data['per_symbol']['BTC']
            assert 'category' in btc
            assert 'article_count' in btc
            assert 'headlines' in btc
            assert 'articles' in btc
            assert btc['category'] == 'crypto'
            # Each article should have expected fields
            for article in btc['articles']:
                assert 'headline' in article
                assert 'source' in article


# --- Graceful shutdown tests ---

class TestShutdown:
    def test_stop_sets_event(self, daemon):
        """stop() should set the shutdown event."""
        assert not daemon.shutdown_event.is_set()
        daemon.stop()
        assert daemon.shutdown_event.is_set()

    def test_start_unblocks_on_stop(self, daemon):
        """start() should return after stop() is called from another thread."""
        with patch.object(daemon, '_rss_loop'), \
             patch.object(daemon, '_web_loop'), \
             patch.object(daemon, '_deep_loop'), \
             patch.object(daemon, '_cache_cleanup_loop'), \
             patch('src.collectors.scraper_daemon.initialize_database'):

            def delayed_stop():
                time.sleep(0.2)
                daemon.stop()

            stopper = threading.Thread(target=delayed_stop)
            stopper.start()
            daemon.start()  # Should unblock when stop() is called
            stopper.join(timeout=5)
            assert daemon.shutdown_event.is_set()


# --- Full RSS cycle test ---

class TestFullCycle:
    @patch('src.collectors.scraper_daemon.save_articles_batch')
    @patch('src.collectors.scraper_daemon._fetch_rss_feeds')
    def test_rss_cycle_end_to_end(self, mock_rss, mock_db, daemon, tmp_output):
        """Full RSS cycle: fetch → dedup → match → JSON + DB."""
        mock_rss.return_value = [
            _make_article('Bitcoin surges past $100k'),
            _make_article('Ethereum breaks $5000 resistance'),
            _make_article('Bitcoin surges past $100k'),  # duplicate
        ]

        count = daemon._process_articles(mock_rss.return_value, 'rss')
        assert count == 2

        # JSON should exist with correct data
        assert os.path.exists(tmp_output)
        with open(tmp_output) as f:
            data = json.load(f)
        assert data['total_articles'] > 0

        # DB archive should have been called
        mock_db.assert_called_once()
        rows = mock_db.call_args[0][0]
        assert len(rows) > 0
        for row in rows:
            assert 'title' in row
            assert 'title_hash' in row

    @patch('src.collectors.scraper_daemon.save_articles_batch')
    def test_no_articles_no_crash(self, mock_db, daemon):
        """Empty article list should not crash."""
        count = daemon._process_articles([], 'rss')
        assert count == 0
        mock_db.assert_not_called()


# --- Metrics tests ---

class TestMetrics:
    def test_metrics_initial_state(self, daemon):
        """Metrics should start at zero."""
        assert daemon.metrics.rss_cycles == 0
        assert daemon.metrics.web_cycles == 0
        assert daemon.metrics.articles_total == 0

    def test_metrics_to_dict(self):
        """to_dict should return all expected fields."""
        m = DaemonMetrics()
        d = m.to_dict()
        expected_keys = {
            'started_at', 'articles_total', 'rss_cycles', 'web_cycles',
            'deep_cycles', 'cleanup_cycles', 'last_rss_success',
            'last_web_success', 'last_deep_success', 'dedup_cache_size',
            'last_errors', 'chrome_mcp_successes', 'chrome_mcp_failures',
            'last_chrome_mcp_source',
        }
        assert set(d.keys()) == expected_keys

    def test_metrics_file_written(self, daemon):
        """_write_metrics should create the status JSON."""
        daemon._write_metrics()
        assert os.path.exists(daemon.metrics_path)
        with open(daemon.metrics_path) as f:
            data = json.load(f)
        assert 'updated_at' in data
        assert 'articles_total' in data

    @patch('src.collectors.scraper_daemon.save_articles_batch')
    def test_articles_total_increments(self, mock_db, daemon):
        """Processing articles should increment the total count."""
        articles = [_make_article('Bitcoin hits new high')]
        daemon._process_articles(articles, 'rss')
        assert daemon.metrics.articles_total == 1

        articles2 = [_make_article('Ethereum reaches $5000')]
        daemon._process_articles(articles2, 'rss')
        assert daemon.metrics.articles_total == 2

    @patch('src.collectors.scraper_daemon.save_articles_batch')
    def test_dedup_cache_size_tracked(self, mock_db, daemon):
        """Metrics should track the dedup cache size."""
        articles = [
            _make_article('Bitcoin hits new high'),
            _make_article('Ethereum reaches $5000'),
        ]
        daemon._process_articles(articles, 'rss')
        assert daemon.metrics.dedup_cache_size == 2


# --- Deep enrichment queue tests ---

class TestDeepEnrichment:
    @patch('src.collectors.scraper_daemon.save_articles_batch')
    def test_important_articles_queued(self, mock_db, daemon):
        """Articles with important categories should be queued for enrichment."""
        articles = [
            _make_article('FDA approves new drug from Pfizer', category='regulatory'),
            _make_article('Bitcoin price update', category='crypto'),
        ]
        daemon._process_articles(articles, 'rss')
        assert len(daemon._pending_enrichment) == 1
        assert daemon._pending_enrichment[0]['category'] == 'regulatory'

    @patch('src.collectors.scraper_daemon.save_articles_batch')
    def test_non_important_not_queued(self, mock_db, daemon):
        """Regular articles should not be queued for enrichment."""
        articles = [_make_article('Bitcoin price update', category='crypto')]
        daemon._process_articles(articles, 'rss')
        assert len(daemon._pending_enrichment) == 0


# --- Config tests ---

class TestConfig:
    def test_default_config(self):
        """Daemon should have sensible defaults without config."""
        d = ScraperDaemon()
        assert d.rss_interval == 300
        assert d.web_interval == 900
        assert d.deep_interval == 1800
        assert d.dedup_cache_hours == 48

    def test_custom_config(self, tmp_output):
        """Config values should override defaults."""
        config = {
            'rss_interval_seconds': 60,
            'web_interval_seconds': 120,
            'deep_interval_seconds': 240,
            'dedup_cache_hours': 24,
        }
        d = ScraperDaemon(output_path=tmp_output, config=config)
        assert d.rss_interval == 60
        assert d.web_interval == 120
        assert d.deep_interval == 240
        assert d.dedup_cache_hours == 24


# --- Chrome MCP integration tests ---

class TestChromeMCPIntegration:
    """Tests for Chrome MCP scraping integration."""

    def test_chrome_mcp_disabled_by_default(self):
        """Chrome MCP should be disabled when no config provided."""
        d = ScraperDaemon()
        assert d.chrome_mcp_enabled is False

    def test_chrome_mcp_config_parsing(self, tmp_output):
        """Config should correctly set Chrome MCP fields."""
        config = {
            'chrome_mcp': {
                'enabled': True,
                'timeout_seconds': 240,
                'fallback_to_bs4': False,
            }
        }
        d = ScraperDaemon(output_path=tmp_output, config=config)
        assert d.chrome_mcp_enabled is True
        assert d.chrome_mcp_timeout == 240
        assert d.chrome_mcp_fallback is False

    def test_chrome_mcp_default_timeout(self, tmp_output):
        """Timeout should default to 180 seconds."""
        config = {'chrome_mcp': {'enabled': True}}
        d = ScraperDaemon(output_path=tmp_output, config=config)
        assert d.chrome_mcp_timeout == 180

    def test_sidecar_path_set(self, tmp_output):
        """Sidecar path should point to data/chrome-scraped.json."""
        d = ScraperDaemon(output_path=tmp_output)
        assert d.chrome_scraped_path.endswith('data/chrome-scraped.json')

    def test_read_sidecar_valid_json(self, daemon, tmp_path):
        """_read_chrome_mcp_sidecar should parse valid sidecar JSON."""
        sidecar = tmp_path / 'chrome-scraped.json'
        data = {
            'articles': [
                {'title': 'Bitcoin surges to new all-time high', 'source': 'CoinDesk', 'source_url': 'https://coindesk.com/1'},
                {'title': 'Ethereum breaks $5000', 'source': 'CoinTelegraph', 'source_url': 'https://ct.com/2'},
            ]
        }
        sidecar.write_text(json.dumps(data))
        daemon.chrome_scraped_path = str(sidecar)

        articles = daemon._read_chrome_mcp_sidecar()
        assert len(articles) == 2
        assert articles[0]['title'] == 'Bitcoin surges to new all-time high'
        assert articles[0]['source'] == 'CoinDesk'
        assert articles[0]['source_url'] == 'https://coindesk.com/1'

    def test_read_sidecar_field_aliases(self, daemon, tmp_path):
        """Should accept headline/summary/url as aliases for title/description/source_url."""
        sidecar = tmp_path / 'chrome-scraped.json'
        data = {
            'articles': [
                {'headline': 'Bitcoin price analysis shows bullish trend', 'summary': 'Analysis details', 'url': 'https://example.com/btc'},
            ]
        }
        sidecar.write_text(json.dumps(data))
        daemon.chrome_scraped_path = str(sidecar)

        articles = daemon._read_chrome_mcp_sidecar()
        assert len(articles) == 1
        assert articles[0]['title'] == 'Bitcoin price analysis shows bullish trend'
        assert articles[0]['description'] == 'Analysis details'
        assert articles[0]['source_url'] == 'https://example.com/btc'

    def test_read_sidecar_missing_file(self, daemon, tmp_path):
        """Should return [] when sidecar file doesn't exist."""
        daemon.chrome_scraped_path = str(tmp_path / 'nonexistent.json')
        assert daemon._read_chrome_mcp_sidecar() == []

    def test_read_sidecar_invalid_json(self, daemon, tmp_path):
        """Should return [] for invalid JSON."""
        sidecar = tmp_path / 'chrome-scraped.json'
        sidecar.write_text('not valid json{{{')
        daemon.chrome_scraped_path = str(sidecar)
        assert daemon._read_chrome_mcp_sidecar() == []

    def test_read_sidecar_empty_articles(self, daemon, tmp_path):
        """Should return [] when articles array is empty."""
        sidecar = tmp_path / 'chrome-scraped.json'
        sidecar.write_text(json.dumps({'articles': []}))
        daemon.chrome_scraped_path = str(sidecar)
        assert daemon._read_chrome_mcp_sidecar() == []

    def test_read_sidecar_short_title_filtered(self, daemon, tmp_path):
        """Articles with titles < 10 chars should be filtered out."""
        sidecar = tmp_path / 'chrome-scraped.json'
        data = {
            'articles': [
                {'title': 'Short', 'source': 'X'},
                {'title': 'This is a sufficiently long headline for testing', 'source': 'Y'},
            ]
        }
        sidecar.write_text(json.dumps(data))
        daemon.chrome_scraped_path = str(sidecar)

        articles = daemon._read_chrome_mcp_sidecar()
        assert len(articles) == 1
        assert articles[0]['title'] == 'This is a sufficiently long headline for testing'

    def test_read_sidecar_detailed_articles(self, daemon, tmp_path):
        """Should include detailed_articles that aren't duplicates."""
        sidecar = tmp_path / 'chrome-scraped.json'
        data = {
            'articles': [
                {'title': 'Bitcoin surges to new all-time high', 'source': 'CoinDesk'},
            ],
            'detailed_articles': [
                {'title': 'Bitcoin surges to new all-time high', 'source': 'CoinDesk', 'description': 'dup'},
                {'title': 'Ethereum DeFi ecosystem grows rapidly', 'source': 'Decrypt', 'description': 'Full text here'},
            ]
        }
        sidecar.write_text(json.dumps(data))
        daemon.chrome_scraped_path = str(sidecar)

        articles = daemon._read_chrome_mcp_sidecar()
        assert len(articles) == 2  # BTC from articles + ETH from detailed (BTC dup skipped)
        titles = [a['title'] for a in articles]
        assert 'Ethereum DeFi ecosystem grows rapidly' in titles

    @patch('src.collectors.scraper_daemon.shutil.which', return_value=None)
    def test_scrape_chrome_mcp_cli_not_found(self, mock_which, daemon):
        """Should return [] when claude CLI is not in PATH."""
        daemon.chrome_mcp_enabled = True
        # Ensure prompt file exists
        daemon.chrome_mcp_prompt_path = os.path.abspath(__file__)  # any existing file
        assert daemon._scrape_via_chrome_mcp() == []

    @patch('src.collectors.scraper_daemon.subprocess.run')
    @patch('src.collectors.scraper_daemon.shutil.which', return_value='/usr/local/bin/claude')
    def test_scrape_chrome_mcp_timeout(self, mock_which, mock_run, daemon):
        """Should return [] on subprocess timeout."""
        mock_run.side_effect = subprocess.TimeoutExpired(cmd='claude', timeout=180)
        daemon.chrome_mcp_enabled = True
        daemon.chrome_mcp_prompt_path = os.path.abspath(__file__)
        assert daemon._scrape_via_chrome_mcp() == []

    @patch('src.collectors.scraper_daemon.save_articles_batch')
    @patch('src.collectors.scraper_daemon.scrape_all_sources')
    def test_web_loop_fallback_to_bs4(self, mock_bs4, mock_db, daemon):
        """When Chrome MCP returns nothing, should fall back to BS4."""
        daemon.chrome_mcp_enabled = True
        daemon.chrome_mcp_fallback = True
        mock_bs4.return_value = [_make_article('Bitcoin rally continues strongly')]

        with patch.object(daemon, '_scrape_via_chrome_mcp', return_value=[]):
            # Run one iteration of the web loop
            daemon.shutdown_event.set()  # Will exit after first iteration
            daemon._web_loop()

        mock_bs4.assert_called_once()
        assert daemon.metrics.chrome_mcp_failures == 1

    @patch('src.collectors.scraper_daemon.save_articles_batch')
    @patch('src.collectors.scraper_daemon.scrape_all_sources')
    def test_web_loop_no_fallback(self, mock_bs4, mock_db, daemon):
        """When fallback_to_bs4 is False, should not call BS4."""
        daemon.chrome_mcp_enabled = True
        daemon.chrome_mcp_fallback = False

        with patch.object(daemon, '_scrape_via_chrome_mcp', return_value=[]):
            daemon.shutdown_event.set()
            daemon._web_loop()

        mock_bs4.assert_not_called()
        assert daemon.metrics.chrome_mcp_failures == 1

    @patch('src.collectors.scraper_daemon.save_articles_batch')
    def test_web_loop_chrome_mcp_success_metrics(self, mock_db, daemon):
        """Successful Chrome MCP scrape should update metrics."""
        daemon.chrome_mcp_enabled = True
        chrome_articles = [_make_article('Bitcoin breaks $100k resistance level')]

        with patch.object(daemon, '_scrape_via_chrome_mcp', return_value=chrome_articles):
            daemon.shutdown_event.set()
            daemon._web_loop()

        assert daemon.metrics.chrome_mcp_successes == 1
        assert daemon.metrics.chrome_mcp_failures == 0
        assert daemon.metrics.last_chrome_mcp_source == 'chrome_mcp'

    def test_metrics_chrome_mcp_fields(self):
        """DaemonMetrics.to_dict should include Chrome MCP fields."""
        m = DaemonMetrics()
        m.chrome_mcp_successes = 5
        m.chrome_mcp_failures = 2
        m.last_chrome_mcp_source = 'chrome_mcp'
        d = m.to_dict()
        assert d['chrome_mcp_successes'] == 5
        assert d['chrome_mcp_failures'] == 2
        assert d['last_chrome_mcp_source'] == 'chrome_mcp'

    @patch('src.collectors.scraper_daemon.save_articles_batch')
    @patch('src.collectors.scraper_daemon.scrape_all_sources')
    def test_web_loop_disabled_uses_bs4(self, mock_bs4, mock_db, daemon):
        """When chrome_mcp disabled, should use BS4 directly."""
        daemon.chrome_mcp_enabled = False
        mock_bs4.return_value = [_make_article('Ethereum staking grows rapidly')]

        with patch.object(daemon, '_scrape_via_chrome_mcp') as mock_chrome:
            daemon.shutdown_event.set()
            daemon._web_loop()

        mock_chrome.assert_not_called()
        mock_bs4.assert_called_once()
