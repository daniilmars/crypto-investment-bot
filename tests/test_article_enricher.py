import pytest
from unittest.mock import patch, MagicMock

from src.collectors.article_enricher import (
    BeautifulSoupExtractor,
    is_important_article,
    enrich_article,
    enrich_articles_batch,
    MAX_BODY_CHARS,
)


class TestIsImportantArticle:
    def test_regulatory_is_important(self):
        assert is_important_article({'category': 'regulatory'}) is True

    def test_kol_is_important(self):
        assert is_important_article({'category': 'kol'}) is True

    def test_ipo_is_important(self):
        assert is_important_article({'category': 'ipo'}) is True

    def test_financial_not_important(self):
        assert is_important_article({'category': 'financial'}) is False

    def test_crypto_not_important(self):
        assert is_important_article({'category': 'crypto'}) is False

    def test_missing_category_not_important(self):
        assert is_important_article({}) is False

    def test_empty_category_not_important(self):
        assert is_important_article({'category': ''}) is False


class TestBeautifulSoupExtractor:
    def test_extracts_from_article_tag(self):
        html = '''
        <html><body>
            <nav>Navigation</nav>
            <article>
                <p>This is the first paragraph of the article body text.</p>
                <p>This is the second paragraph with more content here.</p>
            </article>
            <footer>Footer</footer>
        </body></html>
        '''
        extractor = BeautifulSoupExtractor()
        result = extractor._extract_body(html)
        assert result is not None
        assert 'first paragraph' in result
        assert 'second paragraph' in result

    def test_extracts_from_css_class(self):
        html = '''
        <html><body>
            <div class="article-body">
                <p>Content from the article body class selector.</p>
                <p>More article body content for extraction test.</p>
            </div>
        </body></html>
        '''
        extractor = BeautifulSoupExtractor()
        result = extractor._extract_body(html)
        assert result is not None
        assert 'article body class' in result

    def test_div_fallback_with_paragraphs(self):
        html = '''
        <html><body>
            <div>
                <p>Paragraph one in the main content div block.</p>
                <p>Paragraph two in the main content div block.</p>
                <p>Paragraph three in the main content div block.</p>
            </div>
        </body></html>
        '''
        extractor = BeautifulSoupExtractor()
        result = extractor._extract_body(html)
        assert result is not None
        assert 'Paragraph one' in result

    def test_empty_page_returns_none(self):
        html = '<html><body></body></html>'
        extractor = BeautifulSoupExtractor()
        result = extractor._extract_body(html)
        assert result is None

    @patch('src.collectors.article_enricher.requests.get', side_effect=Exception('Network error'))
    def test_network_error_returns_none(self, mock_get):
        extractor = BeautifulSoupExtractor()
        result = extractor.extract('https://example.com/article')
        assert result is None

    def test_truncates_to_max_chars(self):
        # Build HTML with paragraphs exceeding MAX_BODY_CHARS
        long_para = 'A' * 500
        html = f'''
        <html><body><article>
            <p>{long_para}</p>
            <p>{long_para}</p>
            <p>{long_para}</p>
            <p>{long_para}</p>
            <p>{long_para}</p>
        </article></body></html>
        '''
        extractor = BeautifulSoupExtractor()
        result = extractor._extract_body(html)
        assert result is not None
        assert len(result) <= MAX_BODY_CHARS


class TestEnrichArticle:
    @patch('src.collectors.article_enricher.BeautifulSoupExtractor.extract')
    def test_enriches_with_body(self, mock_extract):
        mock_extract.return_value = 'Full article body text from the page.'
        article = {
            'title': 'FOMC Statement',
            'description': '',
            'source_url': 'https://fed.gov/press',
            'category': 'regulatory',
        }
        result = enrich_article(article)
        assert result['description'] == 'Full article body text from the page.'
        assert result['_enriched'] is True

    @patch('src.collectors.article_enricher.BeautifulSoupExtractor.extract')
    def test_graceful_degradation_on_none(self, mock_extract):
        mock_extract.return_value = None
        article = {
            'title': 'Test Article',
            'description': 'original desc',
            'source_url': 'https://example.com/page',
            'category': 'regulatory',
        }
        result = enrich_article(article)
        assert result['description'] == 'original desc'
        assert '_enriched' not in result

    def test_missing_url_returns_unchanged(self):
        article = {
            'title': 'No URL Article',
            'description': 'some desc',
            'category': 'regulatory',
        }
        result = enrich_article(article)
        assert result['description'] == 'some desc'
        assert '_enriched' not in result

    def test_invalid_url_returns_unchanged(self):
        article = {
            'title': 'Bad URL',
            'description': 'original',
            'source_url': 'not-a-url',
            'category': 'regulatory',
        }
        result = enrich_article(article)
        assert result['description'] == 'original'
        assert '_enriched' not in result


class TestEnrichArticlesBatch:
    @patch('src.collectors.article_enricher.BeautifulSoupExtractor.extract')
    def test_only_enriches_important_categories(self, mock_extract):
        mock_extract.return_value = 'enriched body text from the article.'
        articles = [
            {'title': 'A', 'description': '', 'source_url': 'https://a.com', 'category': 'regulatory'},
            {'title': 'B', 'description': '', 'source_url': 'https://b.com', 'category': 'financial'},
            {'title': 'C', 'description': '', 'source_url': 'https://c.com', 'category': 'kol'},
        ]
        result = enrich_articles_batch(articles)
        assert len(result) == 3
        # regulatory and kol should be enriched, financial should not
        assert result[0].get('_enriched') is True
        assert '_enriched' not in result[1]
        assert result[2].get('_enriched') is True

    @patch('src.collectors.article_enricher.BeautifulSoupExtractor.extract')
    def test_skips_articles_without_url(self, mock_extract):
        mock_extract.return_value = 'body text'
        articles = [
            {'title': 'A', 'description': '', 'source_url': '', 'category': 'regulatory'},
            {'title': 'B', 'description': '', 'source_url': 'https://b.com', 'category': 'regulatory'},
        ]
        result = enrich_articles_batch(articles)
        assert '_enriched' not in result[0]
        assert result[1].get('_enriched') is True

    def test_empty_list_returns_empty(self):
        result = enrich_articles_batch([])
        assert result == []

    def test_no_important_articles_returns_unchanged(self):
        articles = [
            {'title': 'A', 'description': 'desc', 'source_url': 'https://a.com', 'category': 'financial'},
            {'title': 'B', 'description': 'desc', 'source_url': 'https://b.com', 'category': 'crypto'},
        ]
        result = enrich_articles_batch(articles)
        assert len(result) == 2
        assert '_enriched' not in result[0]
        assert '_enriched' not in result[1]
