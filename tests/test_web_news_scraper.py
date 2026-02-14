"""Tests for the web news scraper module."""

from unittest.mock import patch, MagicMock

import pytest

from src.collectors.web_news_scraper import (
    scrape_coindesk,
    scrape_cointelegraph,
    scrape_decrypt,
    scrape_reuters_business,
    scrape_marketwatch,
    scrape_yahoo_finance,
    scrape_all_sources,
    _fetch_page,
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

REUTERS_HTML = """
<html><body>
<a href="/business/fed-holds-rates-steady-march">Fed Holds Interest Rates Steady Amid Economic Uncertainty</a>
<a href="/business/x">Short one here</a>
</body></html>
"""

MARKETWATCH_HTML = """
<html><body>
<a href="/story/nvidia-earnings-beat-expectations-2026">Nvidia Earnings Beat Wall Street Expectations Again</a>
</body></html>
"""

YAHOO_HTML = """
<html><body>
<a href="/news/tesla-deliveries-surge-q1-2026">Tesla Deliveries Surge 30% in Q1 2026</a>
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
    def test_scrape_reuters(self, mock_fetch):
        mock_fetch.return_value = _mock_fetch(REUTERS_HTML)
        articles = scrape_reuters_business()
        assert len(articles) == 1
        assert 'Fed' in articles[0]['title']

    @patch('src.collectors.web_news_scraper._fetch_page')
    def test_scrape_marketwatch(self, mock_fetch):
        mock_fetch.return_value = _mock_fetch(MARKETWATCH_HTML)
        articles = scrape_marketwatch()
        assert len(articles) == 1
        assert 'Nvidia' in articles[0]['title']

    @patch('src.collectors.web_news_scraper._fetch_page')
    def test_scrape_yahoo_finance(self, mock_fetch):
        mock_fetch.return_value = _mock_fetch(YAHOO_HTML)
        articles = scrape_yahoo_finance()
        assert len(articles) == 1
        assert 'Tesla' in articles[0]['title']

    @patch('src.collectors.web_news_scraper._fetch_page')
    def test_scraper_returns_empty_on_failure(self, mock_fetch):
        mock_fetch.return_value = None
        assert scrape_coindesk() == []
        assert scrape_cointelegraph() == []
        assert scrape_decrypt() == []
        assert scrape_reuters_business() == []


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
            ('Reuters', MagicMock(return_value=[{'title': 'Fed news'}])),
            ('Yahoo Finance', MagicMock(return_value=[{'title': 'AAPL news'}])),
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


class TestNewsDataIntegration:
    """Tests that web scraping integrates with the existing news_data pipeline."""

    @patch('src.collectors.news_data.save_news_sentiment_batch')
    @patch('src.collectors.news_data.get_latest_news_sentiment')
    @patch('src.collectors.news_data._fetch_rss_feeds')
    @patch('src.collectors.news_data._fetch_newsapi_articles')
    @patch('src.collectors.web_news_scraper.scrape_all_sources')
    def test_web_scraping_supplements_rss(self, mock_scrape, mock_newsapi,
                                           mock_rss, mock_prev, mock_save):
        mock_newsapi.return_value = []
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
    @patch('src.collectors.news_data.get_latest_news_sentiment')
    @patch('src.collectors.news_data._fetch_rss_feeds')
    @patch('src.collectors.news_data._fetch_newsapi_articles')
    def test_web_scraping_disabled_by_default(self, mock_newsapi, mock_rss,
                                               mock_prev, mock_save):
        mock_newsapi.return_value = []
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
