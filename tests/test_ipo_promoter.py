"""Tests for IPO watchlist promotion logic."""

from unittest.mock import patch, MagicMock

from src.collectors.ipo_watchlist_promoter import promote_new_listings, _validate_ticker


class TestValidateTicker:
    @patch('src.collectors.ipo_watchlist_promoter.yf', create=True)
    def test_valid_ticker(self, mock_yf_module):
        """Valid ticker returns True when yfinance reports a price."""
        import yfinance as yf
        mock_ticker = MagicMock()
        mock_ticker.info = {'regularMarketPrice': 42.50, 'shortName': 'Acme Corp'}
        with patch.object(yf, 'Ticker', return_value=mock_ticker):
            assert _validate_ticker('ACME') is True

    @patch('yfinance.Ticker')
    def test_invalid_ticker(self, mock_ticker_cls):
        """Invalid ticker returns False when no price is available."""
        mock_ticker = MagicMock()
        mock_ticker.info = {}
        mock_ticker_cls.return_value = mock_ticker
        assert _validate_ticker('ZZZZZZ') is False

    @patch('yfinance.Ticker', side_effect=Exception("network error"))
    def test_exception_returns_false(self, mock_ticker_cls):
        """Network errors return False gracefully."""
        assert _validate_ticker('ACME') is False


class TestPromoteNewListings:
    def _make_settings(self, watch_list=None):
        return {
            'stock_trading': {
                'watch_list': watch_list or ['AAPL', 'MSFT'],
            },
            'ipo_tracking': {
                'enabled': True,
                'validate_ticker': False,  # skip yfinance validation in tests
            },
        }

    @patch('src.collectors.ipo_watchlist_promoter.mark_ipo_watchlist_added')
    @patch('src.collectors.ipo_watchlist_promoter.get_ipo_events')
    def test_adds_new_ticker(self, mock_get_events, mock_mark):
        """A listed event with ticker gets added to watchlist."""
        mock_get_events.return_value = [{
            'id': 1,
            'company_name': 'CoreWeave',
            'ticker': 'CRWV',
            'status': 'listed',
            'event_type': 'listed',
            'auto_added_to_watchlist': False,
        }]

        settings = self._make_settings()
        added = promote_new_listings(settings)

        assert added == ['CRWV']
        assert 'CRWV' in settings['stock_trading']['watch_list']
        mock_mark.assert_called_once_with(1)

    @patch('src.collectors.ipo_watchlist_promoter.mark_ipo_watchlist_added')
    @patch('src.collectors.ipo_watchlist_promoter.get_ipo_events')
    def test_skips_already_in_watchlist(self, mock_get_events, mock_mark):
        """Ticker already in watchlist is skipped but marked as added."""
        mock_get_events.return_value = [{
            'id': 2,
            'company_name': 'Apple',
            'ticker': 'AAPL',
            'status': 'listed',
            'event_type': 'listed',
            'auto_added_to_watchlist': False,
        }]

        settings = self._make_settings()
        added = promote_new_listings(settings)

        assert added == []
        mock_mark.assert_called_once_with(2)

    @patch('src.collectors.ipo_watchlist_promoter.mark_ipo_watchlist_added')
    @patch('src.collectors.ipo_watchlist_promoter.get_ipo_events')
    def test_skips_already_promoted(self, mock_get_events, mock_mark):
        """Events already marked as added to watchlist are skipped."""
        mock_get_events.return_value = [{
            'id': 3,
            'company_name': 'CoreWeave',
            'ticker': 'CRWV',
            'status': 'listed',
            'event_type': 'listed',
            'auto_added_to_watchlist': True,
        }]

        settings = self._make_settings()
        added = promote_new_listings(settings)

        assert added == []
        mock_mark.assert_not_called()

    @patch('src.collectors.ipo_watchlist_promoter.mark_ipo_watchlist_added')
    @patch('src.collectors.ipo_watchlist_promoter.get_ipo_events')
    def test_skips_no_ticker(self, mock_get_events, mock_mark):
        """Events without a ticker are skipped."""
        mock_get_events.return_value = [{
            'id': 4,
            'company_name': 'SomeCompany',
            'ticker': None,
            'status': 'listed',
            'event_type': 'listed',
            'auto_added_to_watchlist': False,
        }]

        settings = self._make_settings()
        added = promote_new_listings(settings)

        assert added == []

    @patch('src.collectors.ipo_watchlist_promoter.mark_ipo_watchlist_added')
    @patch('src.collectors.ipo_watchlist_promoter.get_ipo_events')
    def test_skips_invalid_ticker_format(self, mock_get_events, mock_mark):
        """Tickers with invalid format (too long, special chars) are skipped."""
        mock_get_events.return_value = [{
            'id': 5,
            'company_name': 'Weird Corp',
            'ticker': 'TOOLONG',
            'status': 'listed',
            'event_type': 'listed',
            'auto_added_to_watchlist': False,
        }]

        settings = self._make_settings()
        added = promote_new_listings(settings)

        assert added == []

    @patch('src.collectors.ipo_watchlist_promoter.get_ipo_events')
    def test_empty_events(self, mock_get_events):
        """No events returns empty list."""
        mock_get_events.return_value = []
        settings = self._make_settings()
        added = promote_new_listings(settings)
        assert added == []

    @patch('src.collectors.ipo_watchlist_promoter.mark_ipo_watchlist_added')
    @patch('src.collectors.ipo_watchlist_promoter.get_ipo_events')
    def test_validates_ticker_when_enabled(self, mock_get_events, mock_mark):
        """When validate_ticker is True, yfinance validation is checked."""
        mock_get_events.return_value = [{
            'id': 6,
            'company_name': 'FutureTech',
            'ticker': 'FTCH',
            'status': 'listed',
            'event_type': 'listed',
            'auto_added_to_watchlist': False,
        }]

        settings = self._make_settings()
        settings['ipo_tracking']['validate_ticker'] = True

        with patch('src.collectors.ipo_watchlist_promoter._validate_ticker', return_value=False):
            added = promote_new_listings(settings)

        assert added == []
        mock_mark.assert_not_called()

    @patch('src.collectors.ipo_watchlist_promoter.mark_ipo_watchlist_added')
    @patch('src.collectors.ipo_watchlist_promoter.get_ipo_events')
    def test_adds_symbol_keywords_at_runtime(self, mock_get_events, mock_mark):
        """Newly added tickers also get added to SYMBOL_KEYWORDS."""
        mock_get_events.return_value = [{
            'id': 7,
            'company_name': 'CoreWeave',
            'ticker': 'CRWV',
            'status': 'listed',
            'event_type': 'listed',
            'auto_added_to_watchlist': False,
        }]

        settings = self._make_settings()
        added = promote_new_listings(settings)

        assert 'CRWV' in added

        from src.collectors.news_data import SYMBOL_KEYWORDS, _KEYWORD_PATTERNS
        assert 'CRWV' in SYMBOL_KEYWORDS
        assert 'CRWV' in _KEYWORD_PATTERNS

        # Cleanup
        del SYMBOL_KEYWORDS['CRWV']
        del _KEYWORD_PATTERNS['CRWV']
