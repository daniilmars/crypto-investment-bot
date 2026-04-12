import pytest
from unittest.mock import patch, MagicMock

from src.collectors.news_data import (
    RSS_FEEDS,
    SYMBOL_KEYWORDS,
    _deduplicate_articles,
    _fetch_rss_feeds,
    _fetch_single_rss_feed,
    _match_article_to_symbols,
    collect_news_sentiment,
)


class TestRSSFeedConfig:
    """Validates the RSS_FEEDS list structure and category distribution."""

    def test_total_feed_count(self):
        assert len(RSS_FEEDS) == 100

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
        assert len(gn_feeds) == 26

    def test_google_news_feeds_have_time_filter(self):
        gn_feeds = [f for f in RSS_FEEDS if f['category'] == 'google_news']
        for feed in gn_feeds:
            assert 'when:1d' in feed['url'], f"Google News feed missing when:1d filter: {feed['url']}"

    def test_original_feeds_preserved(self):
        categories = {f['category'] for f in RSS_FEEDS}
        for expected in ('financial', 'crypto', 'tech', 'european'):
            assert expected in categories, f"Missing original category: {expected}"

    def test_no_duplicate_urls(self):
        urls = [f['url'] for f in RSS_FEEDS]
        assert len(urls) == len(set(urls)), "Duplicate feed URLs found"

    def test_regulatory_feed_count(self):
        reg_feeds = [f for f in RSS_FEEDS if f['category'] == 'regulatory']
        assert len(reg_feeds) == 14

    def test_sector_feed_count(self):
        sector_feeds = [f for f in RSS_FEEDS if f['category'] == 'sector']
        assert len(sector_feeds) == 5

    def test_kol_feed_count(self):
        kol_feeds = [f for f in RSS_FEEDS if f['category'] == 'kol']
        assert len(kol_feeds) == 5

    def test_ipo_feed_count(self):
        ipo_feeds = [f for f in RSS_FEEDS if f['category'] == 'ipo']
        assert len(ipo_feeds) == 4

    def test_asia_feed_count(self):
        asia_feeds = [f for f in RSS_FEEDS if f['category'] == 'asia']
        assert len(asia_feeds) == 11

    def test_european_feed_count(self):
        eu_feeds = [f for f in RSS_FEEDS if f['category'] == 'european']
        assert len(eu_feeds) == 6

    def test_all_categories_present(self):
        categories = {f['category'] for f in RSS_FEEDS}
        expected = {'financial', 'european', 'crypto', 'tech',
                    'press_release', 'google_news', 'regulatory', 'sector', 'kol', 'ipo', 'asia',
                    'ai', 'ai_research'}
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

    def test_filters_non_english(self):
        """Non-English articles (German, Chinese, etc.) are filtered out."""
        articles = [
            {'title': 'Bitcoin hits new high', 'description': ''},
            {'title': 'PU Prime sichert sich CMA-Lizenz in den VAE und erweitert', 'description': ''},
            {'title': 'PU Prime 获得阿联酋 CMA 牌照，进一步拓展全球监管布局', 'description': ''},
        ]
        result = _deduplicate_articles(articles)
        assert len(result) == 2  # German passes (mostly ASCII), Chinese doesn't
        titles = [a['title'] for a in result]
        assert 'PU Prime 获得阿联酋 CMA 牌照，进一步拓展全球监管布局' not in titles


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

    def test_no_substring_false_positive(self):
        """Short tickers must not match as substrings of longer words."""
        # "GE" should not match "general", "BA" should not match "back"
        result = _match_article_to_symbols(
            'A general back and forth discussion', '', ['GE', 'BA', 'CAT', 'DIS'])
        assert result == []

    def test_no_sol_in_solution(self):
        """SOL should not match 'solution' — only 'Solana' keyword."""
        result = _match_article_to_symbols(
            'New solution for enterprise problems', '', ['SOL'])
        assert result == []

    def test_solana_matches_sol(self):
        """SOL matches via its 'Solana' keyword."""
        result = _match_article_to_symbols(
            'Solana hits new all-time high', '', ['SOL'])
        assert result == ['SOL']

    def test_word_boundary_ticker_match(self):
        """Tickers like AAPL/NVDA still match as standalone words."""
        result = _match_article_to_symbols(
            'AAPL and NVDA lead tech rally', '', ['AAPL', 'NVDA'])
        assert 'AAPL' in result
        assert 'NVDA' in result

    def test_unknown_symbol_skipped(self):
        """Symbols without SYMBOL_KEYWORDS entries are silently skipped."""
        result = _match_article_to_symbols('UNKNOWN token rallies', '', ['UNKNOWN'])
        assert result == []


class TestLegacySubstringFalsePositives:
    """Regression tests for the audited DIS false positives."""

    def test_dis_not_matched_on_distribution(self):
        result = _match_article_to_symbols(
            'US government moves bitcoin possibly linked to steroid distribution conspiracy',
            '', ['DIS', 'BTC'])
        assert 'DIS' not in result
        assert 'BTC' in result

    def test_dis_not_matched_on_nordisk(self):
        result = _match_article_to_symbols(
            'Eli Lilly market share drops, Novo Nordisk holds firm as generic '
            'weight-loss drugs flood India',
            '', ['DIS', 'LLY'])
        assert 'DIS' not in result
        assert 'LLY' in result

    def test_dis_not_matched_on_distribution_etf(self):
        result = _match_article_to_symbols(
            'Roundhill AMZN WeeklyPay ETF announces weekly distribution of $0.3305',
            '', ['DIS', 'AMZN'])
        assert 'DIS' not in result
        assert 'AMZN' in result

    def test_dis_not_matched_on_discloses(self):
        result = _match_article_to_symbols(
            'Bitcoin Depot discloses $3.7M BTC theft in cybersecurity breach',
            '', ['DIS', 'BTC'])
        assert 'DIS' not in result
        assert 'BTC' in result


class TestShortTickerCoOccurrence:
    """Short tickers like DIS/LLY/BA should only match with company context."""

    def test_dis_matches_real_disney_headline(self):
        assert 'DIS' in _match_article_to_symbols(
            'Disney reports Q4 earnings beat, streaming subs rise', '', ['DIS'])

    def test_dis_matches_walt_disney_headline(self):
        assert 'DIS' in _match_article_to_symbols(
            'Walt Disney Company announces new park in Texas', '', ['DIS'])

    def test_bare_dis_with_disney_context_matches(self):
        # DIS ticker with Disney co-occurrence → match via required-context path
        assert 'DIS' in _match_article_to_symbols(
            'DIS upgraded to buy: Disney stock now a top pick', '', ['DIS'])

    def test_bare_dis_without_company_context_rejects(self):
        # DIS ticker alone without any Disney word → no match
        assert 'DIS' not in _match_article_to_symbols(
            'DIS token launches on new DEX', '', ['DIS'])

    def test_bare_lly_with_eli_lilly_context_matches(self):
        assert 'LLY' in _match_article_to_symbols(
            'LLY jumps on Eli Lilly weight loss pill launch', '', ['LLY'])

    def test_bare_lly_without_company_context_rejects(self):
        assert 'LLY' not in _match_article_to_symbols(
            'LLY is a local acronym for local labs', '', ['LLY'])


class TestCaseSensitiveTickers:
    """All-caps tickers should match case-sensitively to avoid noise."""

    def test_lowercase_btc_does_not_match(self):
        # 'btc' substring (lowercase) is common in many words
        result = _match_article_to_symbols(
            'the btc abbreviation is common in boats', '', ['BTC'])
        assert result == []

    def test_uppercase_btc_matches(self):
        assert 'BTC' in _match_article_to_symbols(
            'BTC price rallies to new all time high', '', ['BTC'])

    def test_company_name_still_case_insensitive(self):
        # 'Bitcoin' is a company-name keyword, case-insensitive.
        assert 'BTC' in _match_article_to_symbols(
            'bitcoin surges overnight on ETF inflows', '', ['BTC'])


class TestTrailingPunctuationKeyword:
    """Keywords ending in + or . must match with lookaround guards."""

    def test_disney_plus_matches(self):
        result = _match_article_to_symbols(
            'Disney+ adds 5M subs in Q4', '', ['DIS'])
        assert 'DIS' in result

    def test_amazon_com_matches(self):
        result = _match_article_to_symbols(
            'Amazon.com beats Q3 revenue expectations', '', ['AMZN'])
        assert 'AMZN' in result


class TestMacroKeywordRouting:
    """Macro keyword → sector routing for articles that match no specific symbol."""

    def test_oil_routes_to_energy(self):
        from src.collectors.news_data import _match_article_to_macro_sectors
        result = _match_article_to_macro_sectors(
            'Empty oil tankers heading to the United States', '')
        assert 'energy' in result

    def test_hormuz_routes_to_energy(self):
        from src.collectors.news_data import _match_article_to_macro_sectors
        result = _match_article_to_macro_sectors(
            'Trump orders blockade of Strait of Hormuz', '')
        assert 'energy' in result

    def test_tariff_routes_to_multiple_sectors(self):
        from src.collectors.news_data import _match_article_to_macro_sectors
        result = _match_article_to_macro_sectors(
            'New tariff on Chinese goods announced by White House', '')
        assert 'semiconductors' in result or 'industrials' in result

    def test_defense_routes_correctly(self):
        from src.collectors.news_data import _match_article_to_macro_sectors
        result = _match_article_to_macro_sectors(
            'Pentagon awards new defense contract for missile systems', '')
        assert 'defense' in result

    def test_no_macro_match_returns_empty(self):
        from src.collectors.news_data import _match_article_to_macro_sectors
        result = _match_article_to_macro_sectors(
            'Bruce Springsteen prior to plastic surgery???', '')
        assert result == []

    def test_direct_symbol_match_skips_macro(self):
        """Articles matching a specific symbol should NOT trigger macro routing."""
        result = _match_article_to_symbols(
            'Exxon reports record Q4 profits from oil boom', '', ['XOM'])
        assert 'XOM' in result  # Direct match — macro routing not needed


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


class TestGeminiArticleScoring:
    """Tests for Gemini per-article scoring integration in collect_news_sentiment."""

    @patch('src.collectors.news_data.get_latest_news_sentiment', return_value={})
    @patch('src.collectors.news_data.save_news_sentiment_batch')
    @patch('src.collectors.news_data.save_articles_batch')
    @patch('src.collectors.news_data._score_with_gemini', return_value={'hash_btc': 0.8})
    @patch('src.collectors.news_data._fetch_rss_feeds', return_value=[
        {'title': 'Bitcoin ETF approved by SEC', 'description': 'Major milestone for BTC',
         'published_at': '', 'source': 'Reuters'},
    ])
    @patch('src.collectors.news_data.app_config', {
        'settings': {'news_analysis': {'enabled': True, 'use_gemini_scoring': True,
                                        'volume_spike_multiplier': 3.0, 'sentiment_shift_threshold': 0.3}},
    })
    def test_collect_uses_gemini_score_when_available(
        self, mock_rss, mock_gemini, mock_save_articles, mock_save_sent, mock_prev
    ):
        """When Gemini score is available, it's used instead of VADER."""
        # Patch compute_title_hash to return our known hash
        with patch('src.collectors.news_data.compute_title_hash', return_value='hash_btc'):
            result = collect_news_sentiment(['BTC'])

        assert 'BTC' in result['per_symbol']
        # Gemini score of 0.8 should be used (VADER would give something different)
        assert result['per_symbol']['BTC']['avg_sentiment_score'] == 0.8

    @patch('src.collectors.news_data.get_latest_news_sentiment', return_value={})
    @patch('src.collectors.news_data.save_news_sentiment_batch')
    @patch('src.collectors.news_data.save_articles_batch')
    @patch('src.collectors.news_data._score_with_gemini', return_value={})
    @patch('src.collectors.news_data._fetch_rss_feeds', return_value=[
        {'title': 'Bitcoin amazing surge wonderful', 'description': 'BTC great',
         'published_at': '', 'source': 'Test'},
    ])
    @patch('src.collectors.news_data.app_config', {
        'settings': {'news_analysis': {'enabled': True, 'use_gemini_scoring': True,
                                        'volume_spike_multiplier': 3.0, 'sentiment_shift_threshold': 0.3}},
    })
    def test_no_gemini_score_excludes_article(
        self, mock_rss, mock_gemini, mock_save_articles, mock_save_sent, mock_prev
    ):
        """When Gemini returns no score, article is excluded from aggregates."""
        result = collect_news_sentiment(['BTC'])

        # No Gemini score → article not included → BTC not in per_symbol
        assert 'BTC' not in result['per_symbol']

    @patch('src.collectors.news_data.get_latest_news_sentiment', return_value={})
    @patch('src.collectors.news_data.save_news_sentiment_batch')
    @patch('src.collectors.news_data.save_articles_batch')
    @patch('src.collectors.news_data._fetch_rss_feeds', return_value=[
        {'title': 'Bitcoin price update', 'description': 'BTC moved',
         'published_at': '', 'source': 'Test'},
    ])
    @patch('src.collectors.news_data.app_config', {
        'settings': {'news_analysis': {'enabled': True, 'use_gemini_scoring': False,
                                        'volume_spike_multiplier': 3.0, 'sentiment_shift_threshold': 0.3}},
    })
    def test_gemini_scoring_disabled_by_config(
        self, mock_rss, mock_save_articles, mock_save_sent, mock_prev
    ):
        """When use_gemini_scoring is False, _score_with_gemini is not called."""
        with patch('src.collectors.news_data._score_with_gemini') as mock_gemini:
            result = collect_news_sentiment(['BTC'])
            mock_gemini.assert_not_called()


class TestScoreWithGemini:
    """Tests for the _score_with_gemini helper function."""

    @patch('src.collectors.news_data.update_gemini_scores_batch')
    @patch('src.collectors.news_data.get_gemini_scores_for_hashes', return_value={'hash_a': 0.5})
    def test_returns_cached_scores(self, mock_get, mock_update):
        """Cached scores are returned without calling Gemini API."""
        from src.collectors.news_data import _score_with_gemini

        articles = [{'title': 'Test', 'description': '', 'title_hash': 'hash_a'}]
        result = _score_with_gemini(articles)

        assert result == {'hash_a': 0.5}
        mock_update.assert_not_called()

    @patch('src.collectors.news_data.update_gemini_scores_batch')
    @patch('src.collectors.news_data.get_gemini_scores_for_hashes', return_value={})
    def test_scores_uncached_articles(self, mock_get, mock_update):
        """Uncached articles are sent to Gemini and persisted."""
        from src.collectors.news_data import _score_with_gemini

        articles = [{'title': 'Test', 'description': '', 'title_hash': 'hash_b'}]

        with patch('src.analysis.gemini_news_analyzer.score_articles_batch', return_value={'hash_b': 0.3}) as mock_score:
            result = _score_with_gemini(articles)

        assert result == {'hash_b': 0.3}
        mock_score.assert_called_once()
        mock_update.assert_called_once_with({'hash_b': 0.3})

    def test_empty_input_returns_empty(self):
        """Empty input returns empty dict."""
        from src.collectors.news_data import _score_with_gemini

        result = _score_with_gemini([])
        assert result == {}


class TestCollectNewsSentiment:
    @patch('src.collectors.news_data.app_config', {'settings': {'news_analysis': {'enabled': False}}})
    def test_disabled_returns_empty(self):
        result = collect_news_sentiment(['BTC'])
        assert result == {'per_symbol': {}, 'triggered_symbols': []}

    @patch('src.collectors.news_data.get_latest_news_sentiment', return_value={})
    @patch('src.collectors.news_data.save_news_sentiment_batch')
    @patch('src.collectors.news_data._fetch_rss_feeds', return_value=[])
    @patch('src.collectors.news_data.app_config', {
        'settings': {'news_analysis': {'enabled': True, 'volume_spike_multiplier': 3.0, 'sentiment_shift_threshold': 0.3}},
    })
    def test_no_articles_returns_empty(self, mock_rss, mock_save, mock_prev):
        result = collect_news_sentiment(['BTC'])
        assert result['per_symbol'] == {}
        assert result['triggered_symbols'] == []

    @patch('src.collectors.news_data.get_latest_news_sentiment', return_value={
        'BTC': {'news_volume': 2, 'avg_sentiment_score': 0.0}
    })
    @patch('src.collectors.news_data.save_news_sentiment_batch')
    @patch('src.collectors.news_data.save_articles_batch')
    @patch('src.collectors.news_data._score_with_gemini',
           side_effect=lambda articles: {a['title_hash']: 0.5 for a in articles})
    @patch('src.collectors.news_data._fetch_rss_feeds', return_value=[
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
    })
    def test_volume_spike_triggers(self, mock_rss, mock_gemini, mock_save_articles, mock_save, mock_prev):
        result = collect_news_sentiment(['BTC'])
        assert 'BTC' in result['per_symbol']
        assert 'BTC' in result['triggered_symbols']

    @patch('src.collectors.news_data.get_latest_news_sentiment', return_value={
        'BTC': {'news_volume': 5, 'avg_sentiment_score': -0.5}
    })
    @patch('src.collectors.news_data.save_news_sentiment_batch')
    @patch('src.collectors.news_data.save_articles_batch')
    @patch('src.collectors.news_data._score_with_gemini',
           side_effect=lambda articles: {a['title_hash']: 0.7 for a in articles})
    @patch('src.collectors.news_data._fetch_rss_feeds', return_value=[
        {'title': 'Bitcoin amazing surge wonderful', 'description': 'BTC great', 'published_at': '', 'source': 'T'},
        {'title': 'Bitcoin excellent performance superb', 'description': 'BTC good', 'published_at': '', 'source': 'T'},
        {'title': 'Bitcoin fantastic rally incredible', 'description': 'BTC nice', 'published_at': '', 'source': 'T'},
        {'title': 'Bitcoin outstanding growth brilliant', 'description': 'BTC wow', 'published_at': '', 'source': 'T'},
        {'title': 'Bitcoin spectacular gains awesome', 'description': 'BTC top', 'published_at': '', 'source': 'T'},
    ])
    @patch('src.collectors.news_data.app_config', {
        'settings': {'news_analysis': {'enabled': True, 'volume_spike_multiplier': 3.0, 'sentiment_shift_threshold': 0.3}},
    })
    def test_sentiment_shift_triggers(self, mock_rss, mock_gemini, mock_save_articles, mock_save, mock_prev):
        result = collect_news_sentiment(['BTC'])
        # With Gemini score 0.7 vs previous -0.5, shift of 1.2 exceeds 0.3
        assert 'BTC' in result['per_symbol']
        assert result['per_symbol']['BTC']['avg_sentiment_score'] > -0.2
