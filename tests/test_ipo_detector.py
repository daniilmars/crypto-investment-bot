"""Tests for IPO event detection from article titles."""

from src.collectors.ipo_detector import detect_ipo_events, _normalize_company_name, _is_valid_company_name


class TestNormalizeCompanyName:
    def test_strips_whitespace(self):
        assert _normalize_company_name('  Acme Corp  ') == 'Acme Corp'

    def test_strips_leading_articles(self):
        assert _normalize_company_name('The Widget Co') == 'Widget Co'
        assert _normalize_company_name('A Big Startup') == 'Big Startup'

    def test_no_article(self):
        assert _normalize_company_name('CoreWeave') == 'CoreWeave'


class TestIsValidCompanyName:
    def test_valid_names(self):
        assert _is_valid_company_name('CoreWeave')
        assert _is_valid_company_name('Arm Holdings')
        assert _is_valid_company_name('C3.ai')

    def test_too_short(self):
        assert not _is_valid_company_name('X')
        assert not _is_valid_company_name('')

    def test_too_long(self):
        assert not _is_valid_company_name('A' * 81)

    def test_noise_words(self):
        assert not _is_valid_company_name('the')
        assert not _is_valid_company_name('this')

    def test_no_letters(self):
        assert not _is_valid_company_name('123')


class TestDetectIpoEvents:
    def _make_article(self, title, category='ipo', description=''):
        return {
            'title': title,
            'description': description,
            'category': category,
            'source_url': 'https://example.com/article',
            'title_hash': 'hash123',
        }

    def test_detects_s1_filing(self):
        articles = [self._make_article('CoreWeave files for IPO at $23 billion valuation')]
        events = detect_ipo_events(articles)
        assert len(events) == 1
        assert events[0]['company_name'] == 'CoreWeave'
        assert events[0]['event_type'] == 's1_filed'

    def test_detects_ipo_announced(self):
        articles = [self._make_article('Cerebras plans IPO after strong AI chip demand')]
        events = detect_ipo_events(articles)
        assert len(events) == 1
        assert events[0]['event_type'] == 'ipo_announced'

    def test_detects_ipo_priced(self):
        articles = [self._make_article('SoundHound IPO priced at $15 per share')]
        events = detect_ipo_events(articles)
        assert len(events) == 1
        assert events[0]['event_type'] == 'ipo_priced'

    def test_detects_begins_trading(self):
        articles = [self._make_article('Arm Holdings begins trading on Nasdaq')]
        events = detect_ipo_events(articles)
        assert len(events) == 1
        assert events[0]['event_type'] == 'listed'

    def test_detects_debuts_on_exchange(self):
        articles = [self._make_article('CoreWeave debuts on Nasdaq after AI-fueled IPO')]
        events = detect_ipo_events(articles)
        assert len(events) == 1
        assert events[0]['event_type'] == 'listed'

    def test_detects_ipo_of_pattern(self):
        articles = [self._make_article('IPO of Cerebras Systems draws huge investor interest')]
        events = detect_ipo_events(articles)
        assert len(events) == 1
        assert events[0]['event_type'] == 'ipo_announced'

    def test_skips_non_ipo_categories(self):
        articles = [self._make_article('CoreWeave files for IPO', category='financial')]
        events = detect_ipo_events(articles)
        assert len(events) == 0

    def test_ai_category_scanned(self):
        articles = [self._make_article('Anthropic files for IPO amid AI boom', category='ai')]
        events = detect_ipo_events(articles)
        assert len(events) == 1

    def test_press_release_category_scanned(self):
        articles = [self._make_article('BigBear.ai plans IPO this quarter', category='press_release')]
        events = detect_ipo_events(articles)
        assert len(events) == 1

    def test_deduplicates_within_batch(self):
        articles = [
            self._make_article('CoreWeave files for IPO at $23B'),
            self._make_article('CoreWeave files for IPO, targeting March listing'),
        ]
        events = detect_ipo_events(articles)
        assert len(events) == 1

    def test_different_events_same_company(self):
        articles = [
            self._make_article('CoreWeave files for IPO at $23B'),
            self._make_article('CoreWeave begins trading on Nasdaq'),
        ]
        events = detect_ipo_events(articles)
        assert len(events) == 2

    def test_no_match_returns_empty(self):
        articles = [self._make_article('Bitcoin hits new all-time high')]
        events = detect_ipo_events(articles)
        assert len(events) == 0

    def test_empty_articles(self):
        events = detect_ipo_events([])
        assert events == []

    def test_event_has_source_info(self):
        articles = [self._make_article('Acme Corp files for IPO')]
        events = detect_ipo_events(articles)
        assert events[0]['source_url'] == 'https://example.com/article'
        assert events[0]['source_article_hash'] == 'hash123'

    def test_description_also_scanned(self):
        articles = [self._make_article(
            'Major tech news today',
            description='SoundHound AI files for IPO in Q1 2026'
        )]
        events = detect_ipo_events(articles)
        assert len(events) == 1
