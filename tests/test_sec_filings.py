# tests/test_sec_filings.py
"""Tests for SEC filings collector — Form 4 + 13F."""

from unittest.mock import patch

from src.collectors.sec_filings import (
    _parse_dollar, _make_article, fetch_openinsider_recent,
    fetch_13f_top_funds, collect_sec_filings,
)


# --- Helpers ---------------------------------------------------------------

class TestParseDollar:
    def test_plain_dollars(self):
        assert _parse_dollar("$1,234,567") == 1_234_567.0

    def test_with_million_suffix(self):
        assert _parse_dollar("$2.5M") == 2_500_000.0

    def test_with_billion_suffix(self):
        assert _parse_dollar("$1.2B") == 1_200_000_000.0

    def test_with_thousand_suffix(self):
        assert _parse_dollar("$500K") == 500_000.0

    def test_invalid_returns_none(self):
        assert _parse_dollar("") is None
        assert _parse_dollar("not-a-number") is None
        assert _parse_dollar(None) is None


class TestMakeArticle:
    def test_required_fields(self):
        a = _make_article(
            title="AAPL — Insider BUY: Tim Cook (CEO) bought $5M",
            source="OpenInsider",
            source_url="https://openinsider.com/",
            symbol="AAPL",
        )
        assert a["title"].startswith("AAPL")
        assert a["symbol"] == "AAPL"
        assert a["source"] == "OpenInsider"
        assert len(a["title_hash"]) == 64  # SHA-256 hex
        assert a["category"] == "filings"

    def test_title_hash_stable(self):
        a1 = _make_article("X", "s", "u", "X")
        a2 = _make_article("X", "s2", "u2", "X")
        # Same title → same hash regardless of source/url
        assert a1["title_hash"] == a2["title_hash"]


# --- Form 4 ---------------------------------------------------------------

_OPENINSIDER_HTML = """
<html><body>
<table class="tinytable"><tbody>
<tr>
  <td>X</td><td>2026-04-09</td><td>2026-04-08</td>
  <td><a>AAPL</a></td><td>Apple Inc</td>
  <td>Cook Tim</td><td>CEO</td>
  <td>P - Purchase</td><td>$175.00</td><td>50000</td>
  <td>200000</td><td>+33%</td><td>$8,750,000</td>
</tr>
<tr>
  <td>X</td><td>2026-04-09</td><td>2026-04-08</td>
  <td><a>NVDA</a></td><td>Nvidia</td>
  <td>Huang Jensen</td><td>CEO</td>
  <td>S - Sale</td><td>$700.00</td><td>5000</td>
  <td>10000</td><td>-33%</td><td>$3,500,000</td>
</tr>
<tr>
  <td>X</td><td>2026-04-09</td><td>2026-04-08</td>
  <td><a>SMALLCO</a></td><td>Tiny Inc</td>
  <td>Smith John</td><td>Director</td>
  <td>P - Purchase</td><td>$10.00</td><td>100</td>
  <td>500</td><td>+25%</td><td>$1,000</td>
</tr>
</tbody></table>
</body></html>
"""


class TestFetchOpenInsider:

    @patch("src.collectors.sec_filings._http_get", return_value=_OPENINSIDER_HTML)
    def test_filters_to_watchlist(self, mock_get):
        # SMALLCO is in the HTML but not in the watchlist → ignored
        articles = fetch_openinsider_recent(["AAPL", "NVDA"], min_dollar=100_000)
        symbols = {a["symbol"] for a in articles}
        assert symbols == {"AAPL", "NVDA"}

    @patch("src.collectors.sec_filings._http_get", return_value=_OPENINSIDER_HTML)
    def test_min_dollar_filter(self, mock_get):
        # Setting min_dollar very high → no AAPL/NVDA either
        articles = fetch_openinsider_recent(["AAPL", "NVDA"], min_dollar=10_000_000_000)
        assert articles == []

    @patch("src.collectors.sec_filings._http_get", return_value=_OPENINSIDER_HTML)
    def test_buy_vs_sell_in_title(self, mock_get):
        articles = fetch_openinsider_recent(["AAPL", "NVDA"], min_dollar=100_000)
        aapl = next(a for a in articles if a["symbol"] == "AAPL")
        nvda = next(a for a in articles if a["symbol"] == "NVDA")
        assert "Insider BUY" in aapl["title"]
        assert "Insider SELL" in nvda["title"]

    @patch("src.collectors.sec_filings._http_get", return_value=None)
    def test_http_failure_returns_empty(self, mock_get):
        assert fetch_openinsider_recent(["AAPL"]) == []

    @patch("src.collectors.sec_filings._http_get", return_value="<html>no table</html>")
    def test_no_table_returns_empty(self, mock_get):
        assert fetch_openinsider_recent(["AAPL"]) == []


# --- 13F ------------------------------------------------------------------

_THIRTEENF_HTML = """
<html><body>
<h2>Latest 13F filing — Q4 2025</h2>
<table>
<tr><td>1</td><td>AAPL</td><td>Apple Inc</td><td>$50B</td><td>+12%</td></tr>
<tr><td>2</td><td>MSFT</td><td>Microsoft</td><td>$30B</td><td>NEW</td></tr>
<tr><td>3</td><td>BANANA</td><td>Banana Co</td><td>$10B</td><td>—</td></tr>
</table>
</body></html>
"""


class TestFetch13F:

    @patch("src.collectors.sec_filings._http_get", return_value=_THIRTEENF_HTML)
    def test_extracts_watchlist_holdings(self, mock_get):
        articles = fetch_13f_top_funds(["AAPL", "MSFT"])
        symbols = {a["symbol"] for a in articles}
        assert "AAPL" in symbols
        assert "MSFT" in symbols
        # BANANA had "—" change → filtered
        assert all(a["symbol"] in {"AAPL", "MSFT"} for a in articles)

    @patch("src.collectors.sec_filings._http_get", return_value=_THIRTEENF_HTML)
    def test_quarter_in_title(self, mock_get):
        articles = fetch_13f_top_funds(["AAPL"])
        assert any("Q4 2025" in a["title"] for a in articles)


# --- Public entry ---------------------------------------------------------

class TestCollectSecFilings:

    @patch("src.collectors.sec_filings.fetch_13f_top_funds", return_value=[
        _make_article("AAPL — 13F: Berkshire +12% position (Q4 2025)",
                      "13F:Berkshire", "https://13f.info/x", "AAPL"),
    ])
    @patch("src.collectors.sec_filings.fetch_openinsider_recent", return_value=[
        _make_article("AAPL — Insider BUY: Tim Cook bought $5M",
                      "OpenInsider", "https://openinsider.com/", "AAPL"),
    ])
    def test_combines_both_sources(self, mock_form4, mock_13f):
        articles = collect_sec_filings(["AAPL"])
        assert len(articles) == 2
        sources = {a["source"] for a in articles}
        assert "OpenInsider" in sources
        assert any(s.startswith("13F") for s in sources)

    @patch("src.collectors.sec_filings.fetch_13f_top_funds")
    @patch("src.collectors.sec_filings.fetch_openinsider_recent", return_value=[
        _make_article("DUP", "src", "url", "AAPL"),
        _make_article("DUP", "src", "url", "AAPL"),
    ])
    def test_dedupes_by_title_hash(self, mock_form4, mock_13f):
        mock_13f.return_value = []
        articles = collect_sec_filings(["AAPL"])
        assert len(articles) == 1

    @patch("src.collectors.sec_filings.fetch_13f_top_funds", return_value=[])
    @patch("src.collectors.sec_filings.fetch_openinsider_recent", return_value=[])
    def test_skip_form4(self, mock_form4, mock_13f):
        collect_sec_filings(["AAPL"], include_form4=False)
        mock_form4.assert_not_called()

    @patch("src.collectors.sec_filings.fetch_13f_top_funds", return_value=[])
    @patch("src.collectors.sec_filings.fetch_openinsider_recent", return_value=[])
    def test_skip_13f(self, mock_form4, mock_13f):
        collect_sec_filings(["AAPL"], include_13f=False)
        mock_13f.assert_not_called()
