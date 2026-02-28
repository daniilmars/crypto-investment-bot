import pytest
from unittest.mock import patch, MagicMock

from src.collectors.news_data import (
    RSS_FEEDS,
    _build_query_string,
    _deduplicate_articles,
    _fetch_newsapi_articles,
    _fetch_rss_feeds,
    _fetch_single_rss_feed,
    _match_article_to_symbols,
    collect_news_sentiment,
)


class TestRSSFeedConfig:
    """Validates the RSS_FEEDS list structure and category distribution."""

    def test_total_feed_count(self):
        assert len(RSS_FEEDS) == 47

    def test_all_feeds_have_required_keys(self):
        for feed in RSS_FEEDS:
            assert 'url' in feed, f"Feed missing 'url': {feed}"
            assert 'category' in feed, f"Feed missing 'category': {feed}"
            assert feed['url'].startswith('http'), f"Invalid URL: {feed['url']}"

    def test_press_release_feed_count(self):
        pr_feeds = [f for f in RSS_FEEDS if f['category'] == 'press_release']
        assert len(pr_feeds) == 4

    def test_google_news_feed_count(self):
        gn_feeds = [f for f in RSS_FEEDS if f['category'] == 'google_news']
        assert len(gn_feeds) == 10

    def test_google_news_feeds_have_time_filter(self):
        gn_feeds = [f for f in RSS_FEEDS if f['category'] == 'google_news']
        for feed in gn_feeds:
            assert 'when:1d' in feed['url'], f"Google News feed missing when:1d filter: {feed['url']}"

    def test_original_feeds_preserved(self):
        categories = {f['category'] for f in RSS_FEEDS}
        for expected in ('financial', 'crypto', 'tech', 'wire', 'european'):
            assert expected in categories, f"Missing original category: {expected}"

    def test_no_duplicate_urls(self):
        urls = [f['url'] for f in RSS_FEEDS]
        assert len(urls) == len(set(urls)), "Duplicate feed URLs found"

    def test_regulatory_feed_count(self):
        reg_feeds = [f for f in RSS_FEEDS if f['category'] == 'regulatory']
        assert len(reg_feeds) == 7

    def test_sector_feed_count(self):
        sector_feeds = [f for f in RSS_FEEDS if f['category'] == 'sector']
        assert len(sector_feeds) == 5

    def test_kol_feed_count(self):
        kol_feeds = [f for f in RSS_FEEDS if f['category'] == 'kol']
        assert len(kol_feeds) == 5

    def test_ipo_feed_count(self):
        ipo_feeds = [f for f in RSS_FEEDS if f['category'] == 'ipo']
        assert len(ipo_feeds) == 2

    def test_all_categories_present(self):
        categories = {f['category'] for f in RSS_FEEDS}
        expected = {'financial', 'european', 'crypto', 'tech', 'wire',
                    'press_release', 'google_news', 'regulatory', 'sector', 'kol', 'ipo'}
        assert categories == expected

    def test_ipo_feeds_have_time_filter(self):
        ipo_feeds = [f for f in RSS_FEEDS if f['category'] == 'ipo']
        for feed in ipo_feeds:
            assert 'when:1d' in feed['url'], f"IPO feed missing when:1d filter: {feed['url']}"


class TestFeedParserCompatibility:
    """Tests that _fetch_single_rss_feed handles various RSS formats correctly."""

    @patch('src.collectors.news_data.feedparser.parse')
    def test_google_news_rss_format(self, mock_parse):
        """Google News entries use 'title' and 'summary' like standard RSS."""
        mock_entry = MagicMock()
        mock_entry.title = 'Bitcoin surges past $100k - Reuters'
        mock_entry.summary = 'Bitcoin hit a new all-time high on Monday...'
        mock_entry.published = 'Mon, 14 Feb 2026 10:00:00 GMT'
        mock_entry.link = 'https://news.google.com/articles/123'
        mock_feed = MagicMock()
        mock_feed.entries = [mock_entry]
        mock_feed.feed.title = 'Google News - Bitcoin crypto'
        mock_parse.return_value = mock_feed

        feed_info = {'url': 'https://news.google.com/rss/search?q=Bitcoin+crypto+when:1d', 'category': 'google_news'}
        result = _fetch_single_rss_feed(feed_info)

        assert len(result) == 1
        assert result[0]['title'] == 'Bitcoin surges past $100k - Reuters'
        assert result[0]['source'] == 'Google News - Bitcoin crypto'
        assert result[0]['category'] == 'google_news'
        assert result[0]['source_url'] == 'https://news.google.com/articles/123'

    @patch('src.collectors.news_data.feedparser.parse')
    def test_globenewswire_rss_format(self, mock_parse):
        """GlobeNewsWire entries have standard RSS fields."""
        mock_entry = MagicMock()
        mock_entry.title = 'XYZ Corp Announces Q4 Earnings'
        mock_entry.summary = 'Revenue exceeds expectations...'
        mock_entry.published = 'Fri, 14 Feb 2026 08:30:00 EST'
        mock_entry.link = 'https://www.globenewswire.com/news-release/xyz'
        mock_feed = MagicMock()
        mock_feed.entries = [mock_entry]
        mock_feed.feed.title = 'GlobeNewsWire - Public Companies'
        mock_parse.return_value = mock_feed

        feed_info = {'url': 'https://www.globenewswire.com/RssFeed/subjectcode/25-PER/feedTitle/GlobeNewsWire - Public Companies', 'category': 'press_release'}
        result = _fetch_single_rss_feed(feed_info)

        assert len(result) == 1
        assert result[0]['title'] == 'XYZ Corp Announces Q4 Earnings'
        assert result[0]['source'] == 'GlobeNewsWire - Public Companies'
        assert result[0]['category'] == 'press_release'
        assert result[0]['source_url'] == 'https://www.globenewswire.com/news-release/xyz'

    @patch('src.collectors.news_data.feedparser.parse')
    def test_prnewswire_rss_format(self, mock_parse):
        """PRNewswire entries follow standard RSS structure."""
        mock_entry = MagicMock()
        mock_entry.title = 'ACME Inc Reports Record Revenue'
        mock_entry.summary = 'Full year results show 15% growth...'
        mock_entry.published = 'Thu, 13 Feb 2026 14:00:00 GMT'
        mock_entry.link = 'https://www.prnewswire.com/news/acme'
        mock_feed = MagicMock()
        mock_feed.entries = [mock_entry]
        mock_feed.feed.title = 'PR Newswire: Financial Services'
        mock_parse.return_value = mock_feed

        feed_info = {'url': 'https://www.prnewswire.com/rss/financial-services-latest-news/financial-services-latest-news-list.rss', 'category': 'press_release'}
        result = _fetch_single_rss_feed(feed_info)

        assert len(result) == 1
        assert result[0]['title'] == 'ACME Inc Reports Record Revenue'
        assert result[0]['category'] == 'press_release'
        assert result[0]['source_url'] == 'https://www.prnewswire.com/news/acme'

    @patch('src.collectors.news_data.feedparser.parse')
    def test_empty_feed_returns_empty_list(self, mock_parse):
        mock_feed = MagicMock()
        mock_feed.entries = []
        mock_feed.feed.title = 'Empty Feed'
        mock_parse.return_value = mock_feed

        result = _fetch_single_rss_feed({'url': 'https://example.com/rss', 'category': 'test'})
        assert result == []

    @patch('src.collectors.news_data.feedparser.parse', side_effect=Exception('Parse error'))
    def test_parse_failure_returns_empty_list(self, mock_parse):
        result = _fetch_single_rss_feed({'url': 'https://example.com/bad', 'category': 'test'})
        assert result == []

    @patch('src.collectors.news_data.feedparser.parse')
    def test_source_url_present_in_all_entries(self, mock_parse):
        """Every parsed entry should include a source_url field."""
        mock_entry = MagicMock()
        mock_entry.title = 'Test article'
        mock_entry.summary = 'Summary text'
        mock_entry.published = 'Mon, 14 Feb 2026 10:00:00 GMT'
        mock_entry.link = 'https://example.com/article/1'
        mock_feed = MagicMock()
        mock_feed.entries = [mock_entry]
        mock_feed.feed.title = 'Test Feed'
        mock_parse.return_value = mock_feed

        result = _fetch_single_rss_feed({'url': 'https://example.com/rss', 'category': 'test'})
        assert len(result) == 1
        assert 'source_url' in result[0]
        assert result[0]['source_url'] == 'https://example.com/article/1'

    @patch('src.collectors.news_data.feedparser.parse')
    def test_source_url_empty_when_link_missing(self, mock_parse):
        """Entries without a link attribute should get empty source_url."""
        mock_entry = MagicMock(spec=[])  # no auto-generated attributes
        mock_entry.title = 'No link entry'
        mock_entry.summary = 'Summary'
        mock_entry.published = 'Mon, 14 Feb 2026 10:00:00 GMT'
        mock_feed = MagicMock()
        mock_feed.entries = [mock_entry]
        mock_feed.feed.title = 'Test Feed'
        mock_parse.return_value = mock_feed

        result = _fetch_single_rss_feed({'url': 'https://example.com/rss', 'category': 'test'})
        assert len(result) == 1
        assert result[0]['source_url'] == ''

    @patch('src.collectors.news_data.feedparser.parse')
    def test_fda_rss_format(self, mock_parse):
        """FDA press releases use standard RSS fields."""
        mock_entry = MagicMock()
        mock_entry.title = 'FDA Approves New Drug for Diabetes Treatment'
        mock_entry.summary = 'The FDA today approved a novel therapy...'
        mock_entry.published = 'Mon, 10 Feb 2026 09:00:00 EST'
        mock_entry.link = 'https://www.fda.gov/news/drug-approval-123'
        mock_feed = MagicMock()
        mock_feed.entries = [mock_entry]
        mock_feed.feed.title = 'FDA Press Releases'
        mock_parse.return_value = mock_feed

        feed_info = {'url': 'https://www.fda.gov/about-fda/contact-fda/stay-informed/rss-feeds/press-releases/rss.xml', 'category': 'regulatory'}
        result = _fetch_single_rss_feed(feed_info)

        assert len(result) == 1
        assert result[0]['title'] == 'FDA Approves New Drug for Diabetes Treatment'
        assert result[0]['category'] == 'regulatory'
        assert result[0]['source_url'] == 'https://www.fda.gov/news/drug-approval-123'

    @patch('src.collectors.news_data.feedparser.parse')
    def test_fed_rss_format(self, mock_parse):
        """Federal Reserve monetary policy entries."""
        mock_entry = MagicMock()
        mock_entry.title = 'FOMC Statement: Rates Unchanged at 5.25-5.50%'
        mock_entry.summary = 'The Federal Open Market Committee decided...'
        mock_entry.published = 'Wed, 12 Feb 2026 14:00:00 EST'
        mock_entry.link = 'https://www.federalreserve.gov/newsevents/pressreleases/monetary20260212a.htm'
        mock_feed = MagicMock()
        mock_feed.entries = [mock_entry]
        mock_feed.feed.title = 'Federal Reserve Board - Monetary Policy'
        mock_parse.return_value = mock_feed

        feed_info = {'url': 'https://www.federalreserve.gov/feeds/press_monetary.xml', 'category': 'regulatory'}
        result = _fetch_single_rss_feed(feed_info)

        assert len(result) == 1
        assert 'FOMC' in result[0]['title']
        assert result[0]['category'] == 'regulatory'
        assert 'federalreserve.gov' in result[0]['source_url']

    @patch('src.collectors.news_data.feedparser.parse')
    def test_trump_truth_social_format(self, mock_parse):
        """Trump Truth Social RSS feed format."""
        mock_entry = MagicMock()
        mock_entry.title = 'MASSIVE TARIFFS on China starting Monday!'
        mock_entry.summary = 'We are putting 25% tariffs...'
        mock_entry.published = 'Sun, 09 Feb 2026 08:15:00 EST'
        mock_entry.link = 'https://trumpstruth.org/post/12345'
        mock_feed = MagicMock()
        mock_feed.entries = [mock_entry]
        mock_feed.feed.title = 'Trump Truth Social'
        mock_parse.return_value = mock_feed

        feed_info = {'url': 'https://trumpstruth.org/feed', 'category': 'kol'}
        result = _fetch_single_rss_feed(feed_info)

        assert len(result) == 1
        assert result[0]['title'] == 'MASSIVE TARIFFS on China starting Monday!'
        assert result[0]['category'] == 'kol'
        assert result[0]['source_url'] == 'https://trumpstruth.org/post/12345'

    @patch('src.collectors.news_data.feedparser.parse')
    def test_ipo_google_news_format(self, mock_parse):
        """IPO-specific Google News feed."""
        mock_entry = MagicMock()
        mock_entry.title = 'Stripe IPO Expected in Q2 2026 - Bloomberg'
        mock_entry.summary = 'Stripe is preparing for its long-awaited IPO...'
        mock_entry.published = 'Fri, 14 Feb 2026 11:00:00 GMT'
        mock_entry.link = 'https://news.google.com/articles/ipo-stripe'
        mock_feed = MagicMock()
        mock_feed.entries = [mock_entry]
        mock_feed.feed.title = 'Google News - IPO'
        mock_parse.return_value = mock_feed

        feed_info = {'url': 'https://news.google.com/rss/search?q=IPO+%22initial+public+offering%22+stock+when:1d', 'category': 'ipo'}
        result = _fetch_single_rss_feed(feed_info)

        assert len(result) == 1
        assert 'IPO' in result[0]['title']
        assert result[0]['category'] == 'ipo'
        assert result[0]['source_url'] == 'https://news.google.com/articles/ipo-stripe'


class TestBuildQueryString:
    def test_single_symbol(self):
        query = _build_query_string(['BTC'])
        assert 'BTC' in query
        assert 'Bitcoin' in query

    def test_multiple_symbols(self):
        query = _build_query_string(['BTC', 'ETH'])
        assert 'BTC' in query
        assert 'Ethereum' in query
        assert ' OR ' in query

    def test_unknown_symbol_uses_itself(self):
        query = _build_query_string(['UNKNOWN'])
        assert 'UNKNOWN' in query


class TestDeduplicateArticles:
    def test_removes_duplicates(self):
        articles = [
            {'title': 'Bitcoin hits new high', 'description': ''},
            {'title': 'Bitcoin hits new high', 'description': ''},
            {'title': 'Ethereum update released', 'description': ''},
        ]
        result = _deduplicate_articles(articles)
        assert len(result) == 2

    def test_case_insensitive(self):
        articles = [
            {'title': 'Bitcoin Price Rises', 'description': ''},
            {'title': 'bitcoin price rises', 'description': ''},
        ]
        result = _deduplicate_articles(articles)
        assert len(result) == 1

    def test_empty_titles_skipped(self):
        articles = [
            {'title': '', 'description': 'some desc'},
            {'title': 'Valid Title', 'description': ''},
        ]
        result = _deduplicate_articles(articles)
        assert len(result) == 1
        assert result[0]['title'] == 'Valid Title'

    def test_empty_list(self):
        assert _deduplicate_articles([]) == []


class TestMatchArticleToSymbols:
    def test_matches_ticker(self):
        result = _match_article_to_symbols('BTC price surges', '', ['BTC', 'ETH'])
        assert result == ['BTC']

    def test_matches_full_name(self):
        result = _match_article_to_symbols('Bitcoin hits all time high', '', ['BTC', 'ETH'])
        assert result == ['BTC']

    def test_matches_multiple_symbols(self):
        result = _match_article_to_symbols('Bitcoin and Ethereum rally', '', ['BTC', 'ETH', 'SOL'])
        assert 'BTC' in result
        assert 'ETH' in result
        assert 'SOL' not in result

    def test_matches_in_description(self):
        result = _match_article_to_symbols('Market update', 'Bitcoin sees gains', ['BTC'])
        assert result == ['BTC']

    def test_no_match(self):
        result = _match_article_to_symbols('Weather forecast today', 'Sunny skies', ['BTC', 'ETH'])
        assert result == []

    def test_stock_symbol_match(self):
        result = _match_article_to_symbols('Apple stock rises after earnings', '', ['AAPL', 'MSFT'])
        assert 'AAPL' in result


class TestFetchNewsAPIArticles:
    @patch('src.collectors.news_data.app_config', {'api_keys': {'newsapi': None}})
    def test_missing_api_key_returns_empty(self):
        result = _fetch_newsapi_articles('Bitcoin')
        assert result == []

    @patch('src.collectors.news_data.app_config', {'api_keys': {'newsapi': 'YOUR_NEWSAPI_ORG_KEY'}})
    def test_placeholder_api_key_returns_empty(self):
        result = _fetch_newsapi_articles('Bitcoin')
        assert result == []

    @patch('src.collectors.news_data.requests.get')
    @patch('src.collectors.news_data.app_config', {'api_keys': {'newsapi': 'real-key'}})
    def test_successful_fetch(self, mock_get):
        mock_response = MagicMock()
        mock_response.json.return_value = {
            'articles': [
                {
                    'title': 'Bitcoin price up',
                    'description': 'BTC rose 5%',
                    'publishedAt': '2025-01-01T00:00:00Z',
                    'source': {'name': 'CoinDesk'},
                }
            ]
        }
        mock_response.raise_for_status = MagicMock()
        mock_get.return_value = mock_response

        result = _fetch_newsapi_articles('Bitcoin')
        assert len(result) == 1
        assert result[0]['title'] == 'Bitcoin price up'
        assert result[0]['source'] == 'CoinDesk'

    @patch('src.collectors.news_data.requests.get', side_effect=Exception('Network error'))
    @patch('src.collectors.news_data.app_config', {'api_keys': {'newsapi': 'real-key'}})
    def test_network_error_returns_empty(self, mock_get):
        result = _fetch_newsapi_articles('Bitcoin')
        assert result == []


class TestFetchRSSFeeds:
    @patch('src.collectors.news_data.feedparser.parse')
    def test_parses_rss_entries(self, mock_parse):
        mock_feed = MagicMock()
        mock_feed.entries = [
            MagicMock(title='RSS Article 1', summary='desc 1', published='2025-01-01'),
        ]
        mock_feed.feed.title = 'Test Feed'
        mock_parse.return_value = mock_feed

        result = _fetch_rss_feeds()
        assert len(result) > 0
        assert result[0]['title'] == 'RSS Article 1'


class TestCollectNewsSentiment:
    @patch('src.collectors.news_data.app_config', {'settings': {'news_analysis': {'enabled': False}}})
    def test_disabled_returns_empty(self):
        result = collect_news_sentiment(['BTC'])
        assert result == {'per_symbol': {}, 'triggered_symbols': []}

    @patch('src.collectors.news_data.get_latest_news_sentiment', return_value={})
    @patch('src.collectors.news_data.save_news_sentiment_batch')
    @patch('src.collectors.news_data._fetch_rss_feeds', return_value=[])
    @patch('src.collectors.news_data._fetch_newsapi_articles', return_value=[])
    @patch('src.collectors.news_data.app_config', {
        'settings': {'news_analysis': {'enabled': True, 'volume_spike_multiplier': 3.0, 'sentiment_shift_threshold': 0.3}},
        'api_keys': {'newsapi': 'key'}
    })
    def test_no_articles_returns_empty(self, mock_newsapi, mock_rss, mock_save, mock_prev):
        result = collect_news_sentiment(['BTC'])
        assert result['per_symbol'] == {}
        assert result['triggered_symbols'] == []

    @patch('src.collectors.news_data.get_latest_news_sentiment', return_value={
        'BTC': {'news_volume': 2, 'avg_sentiment_score': 0.0}
    })
    @patch('src.collectors.news_data.save_news_sentiment_batch')
    @patch('src.collectors.news_data._fetch_rss_feeds', return_value=[])
    @patch('src.collectors.news_data._fetch_newsapi_articles', return_value=[
        {'title': 'Bitcoin price surges to new ATH', 'description': 'BTC up', 'published_at': '', 'source': 'Test'},
        {'title': 'Bitcoin rally continues strong', 'description': 'BTC gains', 'published_at': '', 'source': 'Test'},
        {'title': 'Bitcoin momentum bullish signals', 'description': 'BTC positive', 'published_at': '', 'source': 'Test'},
        {'title': 'Bitcoin adoption grows worldwide', 'description': 'BTC expands', 'published_at': '', 'source': 'Test'},
        {'title': 'Bitcoin ETF inflows record high', 'description': 'BTC flows', 'published_at': '', 'source': 'Test'},
        {'title': 'Bitcoin smart money accumulation rising', 'description': 'BTC accumulation', 'published_at': '', 'source': 'Test'},
        {'title': 'Bitcoin institutional interest grows', 'description': 'BTC inst', 'published_at': '', 'source': 'Test'},
    ])
    @patch('src.collectors.news_data.app_config', {
        'settings': {'news_analysis': {'enabled': True, 'volume_spike_multiplier': 3.0, 'sentiment_shift_threshold': 0.3}},
        'api_keys': {'newsapi': 'key'}
    })
    def test_volume_spike_triggers(self, mock_newsapi, mock_rss, mock_save, mock_prev):
        result = collect_news_sentiment(['BTC'])
        assert 'BTC' in result['per_symbol']
        assert 'BTC' in result['triggered_symbols']

    @patch('src.collectors.news_data.get_latest_news_sentiment', return_value={
        'BTC': {'news_volume': 5, 'avg_sentiment_score': -0.5}
    })
    @patch('src.collectors.news_data.save_news_sentiment_batch')
    @patch('src.collectors.news_data._fetch_rss_feeds', return_value=[])
    @patch('src.collectors.news_data._fetch_newsapi_articles', return_value=[
        {'title': 'Bitcoin amazing surge wonderful', 'description': 'BTC great', 'published_at': '', 'source': 'T'},
        {'title': 'Bitcoin excellent performance superb', 'description': 'BTC good', 'published_at': '', 'source': 'T'},
        {'title': 'Bitcoin fantastic rally incredible', 'description': 'BTC nice', 'published_at': '', 'source': 'T'},
        {'title': 'Bitcoin outstanding growth brilliant', 'description': 'BTC wow', 'published_at': '', 'source': 'T'},
        {'title': 'Bitcoin spectacular gains awesome', 'description': 'BTC top', 'published_at': '', 'source': 'T'},
    ])
    @patch('src.collectors.news_data.app_config', {
        'settings': {'news_analysis': {'enabled': True, 'volume_spike_multiplier': 3.0, 'sentiment_shift_threshold': 0.3}},
        'api_keys': {'newsapi': 'key'}
    })
    def test_sentiment_shift_triggers(self, mock_newsapi, mock_rss, mock_save, mock_prev):
        result = collect_news_sentiment(['BTC'])
        # With very positive headlines vs previous -0.5, shift should exceed 0.3
        assert 'BTC' in result['per_symbol']
        assert result['per_symbol']['BTC']['avg_sentiment_score'] > -0.2
