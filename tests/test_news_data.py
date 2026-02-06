import pytest
from unittest.mock import patch, MagicMock

from src.collectors.news_data import (
    _build_query_string,
    _deduplicate_articles,
    _fetch_newsapi_articles,
    _fetch_rss_feeds,
    _match_article_to_symbols,
    collect_news_sentiment,
)


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
        {'title': 'Bitcoin whale accumulation rising', 'description': 'BTC whales', 'published_at': '', 'source': 'Test'},
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
