"""Tests for the web news scraper module."""

from unittest.mock import patch, MagicMock

from src.collectors.web_news_scraper import (
    scrape_coindesk,
    scrape_cointelegraph,
    scrape_decrypt,
    scrape_all_sources,
    _generic_article_fallback,
    _scraper_health,
    _update_scraper_health,
    _check_scraper_health,
    get_scraper_health,
)


# --- Mock HTML fixtures ---

COINDESK_HTML = """
<html><body>
<a href="/article/bitcoin-etf-record-inflows-2026">Bitcoin ETF Sees Record Weekly Inflows of $2B</a>
<a href="/markets/ethereum-price-surge">Ethereum Surges Past $4000 Mark</a>
<a href="/article/x">Too short</a>
</body></html>
"""

COINTELEGRAPH_HTML = """
<html><body>
<a href="/news/solana-defi-tvl-hits-new-high">Solana DeFi TVL Hits New All-Time High</a>
<a href="/news/btc">Short headline</a>
</body></html>
"""

DECRYPT_HTML = """
<html><body>
<h3>Bitcoin Mining Difficulty Reaches Record Level in February</h3>
<a href="/other-page">Not a news article here</a>
</body></html>
"""



def _mock_fetch(html):
    """Create a mock _fetch_page that returns parsed HTML."""
    from bs4 import BeautifulSoup
    return BeautifulSoup(html, 'html.parser')


class TestIndividualScrapers:

    @patch('src.collectors.web_news_scraper._fetch_page')
    def test_scrape_coindesk(self, mock_fetch):
        mock_fetch.return_value = _mock_fetch(COINDESK_HTML)
        articles = scrape_coindesk()
        assert len(articles) == 2
        assert 'Bitcoin ETF' in articles[0]['title']
        assert articles[0]['source'] == 'CoinDesk'
        assert articles[0]['source_url'].startswith('https://www.coindesk.com/')

    @patch('src.collectors.web_news_scraper._fetch_page')
    def test_scrape_cointelegraph(self, mock_fetch):
        mock_fetch.return_value = _mock_fetch(COINTELEGRAPH_HTML)
        articles = scrape_cointelegraph()
        assert len(articles) == 1
        assert 'Solana' in articles[0]['title']
        assert articles[0]['source'] == 'CoinTelegraph'

    @patch('src.collectors.web_news_scraper._fetch_page')
    def test_scrape_decrypt(self, mock_fetch):
        mock_fetch.return_value = _mock_fetch(DECRYPT_HTML)
        articles = scrape_decrypt()
        assert len(articles) == 1
        assert 'Mining Difficulty' in articles[0]['title']

    @patch('src.collectors.web_news_scraper._fetch_page')
    def test_scraper_returns_empty_on_failure(self, mock_fetch):
        mock_fetch.return_value = None
        assert scrape_coindesk() == []
        assert scrape_cointelegraph() == []
        assert scrape_decrypt() == []


class TestScrapeAllSources:
    """Tests for scrape_all_sources. Patches ALL_SCRAPERS since it captures
    function references at import time."""

    def _make_scrapers(self, results):
        """Build a mock ALL_SCRAPERS list from {name: return_value} dict."""
        scrapers = []
        for name, ret in results.items():
            fn = MagicMock(return_value=ret)
            fn.__name__ = f'scrape_{name.lower()}'
            scrapers.append((name, fn))
        return scrapers

    @patch('src.collectors.web_news_scraper.ALL_SCRAPERS')
    def test_scrape_all_combines_results(self, mock_all):
        mock_all.__iter__ = MagicMock(return_value=iter([
            ('CoinDesk', MagicMock(return_value=[{'title': 'BTC news'}])),
            ('Decrypt', MagicMock(return_value=[{'title': 'DeFi news'}])),
            ('CNBC', MagicMock(return_value=[{'title': 'Market news'}])),
        ]))
        # Patch to list for len()
        mock_all.__len__ = MagicMock(return_value=3)

        articles = scrape_all_sources()
        assert len(articles) == 3

    @patch('src.collectors.web_news_scraper.ALL_SCRAPERS')
    def test_scrape_all_with_source_filter(self, mock_all):
        cd_fn = MagicMock(return_value=[{'title': 'BTC news'}])
        ct_fn = MagicMock(return_value=[{'title': 'ETH news'}])
        mock_all.__iter__ = MagicMock(return_value=iter([
            ('CoinDesk', cd_fn),
            ('CoinTelegraph', ct_fn),
        ]))

        articles = scrape_all_sources(enabled_sources=['CoinDesk'])
        assert len(articles) == 1
        assert cd_fn.called
        assert not ct_fn.called

    @patch('src.collectors.web_news_scraper.ALL_SCRAPERS')
    def test_scrape_all_handles_failures(self, mock_all):
        ok_fn = MagicMock(return_value=[{'title': 'BTC news', 'source': 'CoinDesk'}])
        fail_fn = MagicMock(side_effect=Exception("Connection timeout"))
        mock_all.__iter__ = MagicMock(return_value=iter([
            ('CoinDesk', ok_fn),
            ('CoinTelegraph', fail_fn),
        ]))

        # Should not raise — failed scrapers are skipped
        articles = scrape_all_sources()
        assert len(articles) >= 1


class TestGenericFallback:

    def test_fallback_extracts_headlines(self):
        """Generic fallback scraper extracts headlines from standard HTML patterns."""
        from bs4 import BeautifulSoup
        html = """
        <html><body>
            <h2><a href="/article/breaking-news">Breaking: Major Crypto Exchange Announces New Feature</a></h2>
            <h3><a href="/article/market-update">Market Update for Today Shows Growth</a></h3>
            <article><a href="/post/deep-dive">Deep Dive into DeFi Lending Protocols</a></article>
            <a href="/short">Short</a>
        </body></html>
        """
        soup = BeautifulSoup(html, 'html.parser')
        articles = _generic_article_fallback(soup, 'TestSource', 'https://example.com')
        assert len(articles) >= 2
        titles = [a['title'] for a in articles]
        assert any('Breaking' in t for t in titles)
        assert all(a['source'] == 'TestSource' for a in articles)
        assert all(a['source_url'].startswith('https://') for a in articles)


class TestNewsDataIntegration:
    """Tests that web scraping integrates with the existing news_data pipeline."""

    @patch('src.collectors.news_data.save_news_sentiment_batch')
    @patch('src.collectors.news_data.save_articles_batch')
    @patch('src.collectors.news_data.get_latest_news_sentiment')
    @patch('src.collectors.news_data._score_with_gemini',
           side_effect=lambda articles: {a['title_hash']: 0.6 for a in articles})
    @patch('src.collectors.news_data._fetch_rss_feeds')
    @patch('src.collectors.web_news_scraper.scrape_all_sources')
    def test_web_scraping_supplements_rss(self, mock_scrape,
                                           mock_rss, mock_gemini, mock_prev,
                                           mock_save_articles, mock_save):
        mock_rss.return_value = [
            {'title': 'Bitcoin hits 100k from RSS', 'description': 'BTC milestone'},
        ]
        mock_scrape.return_value = [
            {'title': 'Ethereum DeFi boom from web scraper', 'description': 'ETH DeFi'},
        ]
        mock_prev.return_value = {}
        mock_save.return_value = None

        from src.collectors.news_data import collect_news_sentiment

        with patch.dict('src.collectors.news_data.app_config', {
            'settings': {
                'news_analysis': {
                    'enabled': True,
                    'web_scraping': {'enabled': True},
                    'volume_spike_multiplier': 3.0,
                    'sentiment_shift_threshold': 0.3,
                }
            }
        }):
            result = collect_news_sentiment(['BTC', 'ETH'])

        per_symbol = result['per_symbol']
        # BTC should have the RSS article
        assert 'BTC' in per_symbol
        # ETH should have the web-scraped article
        assert 'ETH' in per_symbol

    @patch('src.collectors.news_data.save_news_sentiment_batch')
    @patch('src.collectors.news_data.save_articles_batch')
    @patch('src.collectors.news_data.get_latest_news_sentiment')
    @patch('src.collectors.news_data._score_with_gemini',
           side_effect=lambda articles: {a['title_hash']: 0.5 for a in articles})
    @patch('src.collectors.news_data._fetch_rss_feeds')
    def test_web_scraping_disabled_by_default(self, mock_rss,
                                               mock_gemini, mock_prev,
                                               mock_save_articles, mock_save):
        mock_rss.return_value = [
            {'title': 'Bitcoin news from RSS only', 'description': 'BTC'},
        ]
        mock_prev.return_value = {}
        mock_save.return_value = None

        from src.collectors.news_data import collect_news_sentiment

        with patch.dict('src.collectors.news_data.app_config', {
            'settings': {
                'news_analysis': {
                    'enabled': True,
                    'volume_spike_multiplier': 3.0,
                    'sentiment_shift_threshold': 0.3,
                }
            }
        }):
            result = collect_news_sentiment(['BTC'])

        # Should work without web scraping
        assert 'BTC' in result['per_symbol']


class TestScraperHealth:
    """Tests for scraper health monitoring."""

    def setup_method(self):
        """Clear health state before each test."""
        _scraper_health.clear()

    def test_update_tracks_counts(self):
        _update_scraper_health('CoinDesk', 10)
        _update_scraper_health('CoinDesk', 8)
        _update_scraper_health('CoinDesk', 12)
        assert _scraper_health['CoinDesk'] == [10, 8, 12]

    def test_update_trims_to_window(self):
        for i in range(15):
            _update_scraper_health('CoinDesk', i)
        assert len(_scraper_health['CoinDesk']) == 10
        assert _scraper_health['CoinDesk'][0] == 5  # oldest kept

    def test_check_detects_degraded_source(self):
        """Source with avg 5+ that drops to 0 for 3 runs is flagged."""
        # 4 runs with 10 articles, then 3 runs with 0
        for _ in range(4):
            _update_scraper_health('TestSource', 10)
        for _ in range(3):
            _update_scraper_health('TestSource', 0)

        degraded = _check_scraper_health()
        assert len(degraded) == 1
        assert degraded[0][0] == 'TestSource'
        assert degraded[0][1] == 10.0

    def test_check_no_alert_for_healthy_source(self):
        """Source returning articles is not flagged."""
        for count in [10, 8, 12, 9, 11, 7]:
            _update_scraper_health('CoinDesk', count)
        degraded = _check_scraper_health()
        assert len(degraded) == 0

    def test_check_no_alert_insufficient_history(self):
        """Sources with <4 runs are not evaluated."""
        _update_scraper_health('NewSource', 0)
        _update_scraper_health('NewSource', 0)
        _update_scraper_health('NewSource', 0)
        degraded = _check_scraper_health()
        assert len(degraded) == 0

    def test_check_no_alert_low_historical_avg(self):
        """Source that historically had <5 articles is not flagged."""
        for _ in range(4):
            _update_scraper_health('SmallSite', 3)
        for _ in range(3):
            _update_scraper_health('SmallSite', 0)
        degraded = _check_scraper_health()
        assert len(degraded) == 0

    def test_check_no_alert_when_recent_has_articles(self):
        """Source that returned articles in last 3 runs is not flagged."""
        for _ in range(4):
            _update_scraper_health('CoinDesk', 10)
        _update_scraper_health('CoinDesk', 0)
        _update_scraper_health('CoinDesk', 0)
        _update_scraper_health('CoinDesk', 5)  # recovered
        degraded = _check_scraper_health()
        assert len(degraded) == 0

    def test_get_health_status_healthy(self):
        for count in [10, 8, 12, 9]:
            _update_scraper_health('CoinDesk', count)
        status = get_scraper_health()
        assert status['CoinDesk']['health'] == 'healthy'
        assert status['CoinDesk']['last_count'] == 9
        assert status['CoinDesk']['runs_tracked'] == 4

    def test_get_health_status_degraded(self):
        for _ in range(4):
            _update_scraper_health('TestSource', 10)
        for _ in range(3):
            _update_scraper_health('TestSource', 0)
        status = get_scraper_health()
        assert status['TestSource']['health'] == 'degraded'
        assert status['TestSource']['avg_historical'] == 10.0
        assert status['TestSource']['avg_recent'] == 0.0

    def test_get_health_status_warning(self):
        """Source with 3 zeros but no prior history shows warning."""
        for _ in range(3):
            _update_scraper_health('NewSite', 0)
        status = get_scraper_health()
        assert status['NewSite']['health'] == 'warning'

    @patch('src.collectors.web_news_scraper.ALL_SCRAPERS')
    def test_scrape_all_updates_health(self, mock_all):
        """scrape_all_sources records counts per source."""
        cd_fn = MagicMock(return_value=[{'title': f'Art {i}'} for i in range(5)])
        empty_fn = MagicMock(return_value=[])
        mock_all.__iter__ = MagicMock(return_value=iter([
            ('CoinDesk', cd_fn),
            ('Decrypt', empty_fn),
        ]))

        scrape_all_sources()

        assert 'CoinDesk' in _scraper_health
        assert _scraper_health['CoinDesk'][-1] == 5
        assert 'Decrypt' in _scraper_health
        assert _scraper_health['Decrypt'][-1] == 0

    @patch('src.collectors.web_news_scraper.ALL_SCRAPERS')
    def test_scrape_all_records_zero_on_failure(self, mock_all):
        """Failed scrapers get 0 recorded in health."""
        fail_fn = MagicMock(side_effect=Exception("timeout"))
        mock_all.__iter__ = MagicMock(return_value=iter([
            ('CoinDesk', fail_fn),
        ]))

        scrape_all_sources()

        assert _scraper_health['CoinDesk'][-1] == 0
