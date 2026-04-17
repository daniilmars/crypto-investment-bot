"""Tests for FX conversion helpers."""

import time
from unittest.mock import patch, MagicMock

import pandas as pd
import pytest

from src.analysis.fx import (
    SUFFIX_TO_CCY,
    currency_for_symbol,
    to_usd,
    clear_cache,
    refresh_all_rates,
    _rate_cache,
    _FALLBACK_USD_PER_UNIT,
)


@pytest.fixture(autouse=True)
def _reset_cache():
    clear_cache()
    yield
    clear_cache()


# --- currency_for_symbol --------------------------------------------------

def test_currency_for_bare_usd_symbols():
    assert currency_for_symbol("AAPL") == "USD"
    assert currency_for_symbol("NVDA") == "USD"
    assert currency_for_symbol("BTC") == "USD"


def test_currency_for_lse_ticker():
    assert currency_for_symbol("BP.L") == "GBP"
    assert currency_for_symbol("IHG.L") == "GBP"


def test_currency_for_euro_exchanges():
    assert currency_for_symbol("BBVA.MC") == "EUR"
    assert currency_for_symbol("NESTE.HE") == "EUR"
    assert currency_for_symbol("BAS.DE") == "EUR"
    assert currency_for_symbol("AI.PA") == "EUR"


def test_currency_for_nordic_and_asia():
    assert currency_for_symbol("NOVO-B.CO") == "DKK"
    assert currency_for_symbol("1299.HK") == "HKD"
    assert currency_for_symbol("9984.T") == "JPY"
    assert currency_for_symbol("BHP.AX") == "AUD"


def test_currency_for_empty_or_none_defaults_to_usd():
    assert currency_for_symbol("") == "USD"
    assert currency_for_symbol(None) == "USD"


def test_suffix_table_covers_expected_set():
    expected = {"GBP", "EUR", "DKK", "HKD", "JPY", "CAD", "CHF", "AUD", "KRW", "CNY"}
    assert set(SUFFIX_TO_CCY.values()) == expected


# --- to_usd ---------------------------------------------------------------

def test_to_usd_bypasses_usd():
    assert to_usd(100.0, "USD") == 100.0


def test_to_usd_returns_zero_for_zero_amount():
    assert to_usd(0, "GBP") == 0.0
    assert to_usd(None, "GBP") == 0.0


def test_to_usd_uses_fallback_when_db_unavailable():
    with patch("src.database.get_db_connection", return_value=None):
        result = to_usd(100.0, "GBP")
    # GBP fallback is 1.27 → 100 * 1.27 = 127
    assert result == pytest.approx(100 * _FALLBACK_USD_PER_UNIT["GBP"])


def test_to_usd_uses_cache_on_repeat_call():
    with patch("src.database.get_db_connection", return_value=None):
        to_usd(100.0, "EUR")
        assert "EUR" in _rate_cache
        cached_rate, _ = _rate_cache["EUR"]
        # Monkey-patch fallback map to a different value — cache should win
        with patch.dict(_FALLBACK_USD_PER_UNIT, {"EUR": 99.0}, clear=False):
            result = to_usd(100.0, "EUR")
    assert result == pytest.approx(100.0 * cached_rate)


def test_to_usd_returns_unconverted_when_no_rate_available():
    with patch("src.database.get_db_connection", return_value=None):
        result = to_usd(100.0, "ZZZ")
    assert result == 100.0


# --- refresh_all_rates ----------------------------------------------------

def _fake_ticker_history(rate: float):
    """Build a MagicMock yf.Ticker whose .history() returns a 1-row frame."""
    mock = MagicMock()
    mock.history.return_value = pd.DataFrame({"Close": [rate]})
    return mock


def test_refresh_all_rates_fetches_every_currency_and_persists(tmp_path, monkeypatch):
    import os
    os.environ["DATABASE_URL"] = f"sqlite:///{tmp_path}/fx_test.db"
    from src import database
    database.initialize_database()

    fake_rates = {
        "GBP": 1.30, "EUR": 1.10, "DKK": 0.15, "HKD": 0.13, "JPY": 0.0068,
        "CAD": 0.75, "CHF": 1.15, "AUD": 0.66, "KRW": 0.00075, "CNY": 0.14,
    }

    def fake_ticker(pair):
        ccy = pair.replace("USD=X", "")
        return _fake_ticker_history(fake_rates[ccy])

    with patch("yfinance.Ticker", side_effect=fake_ticker):
        fetched = refresh_all_rates()

    assert set(fetched.keys()) == set(fake_rates.keys())
    for ccy, expected in fake_rates.items():
        assert fetched[ccy] == pytest.approx(expected)

    # Verify persistence: to_usd now finds the rate in DB (not fallback)
    clear_cache()
    assert to_usd(100.0, "GBP") == pytest.approx(100.0 * 1.30)


def test_refresh_all_rates_skips_bad_data(tmp_path, monkeypatch):
    import os
    os.environ["DATABASE_URL"] = f"sqlite:///{tmp_path}/fx_bad.db"
    from src import database
    database.initialize_database()

    def fake_ticker(pair):
        mock = MagicMock()
        if "GBP" in pair:
            mock.history.return_value = pd.DataFrame({"Close": [1.30]})
        elif "EUR" in pair:
            mock.history.return_value = pd.DataFrame()  # empty
        else:
            mock.history.return_value = pd.DataFrame({"Close": [-1.0]})  # invalid
        return mock

    with patch("yfinance.Ticker", side_effect=fake_ticker):
        fetched = refresh_all_rates()

    assert fetched == {"GBP": pytest.approx(1.30)}


def test_refresh_all_rates_returns_empty_on_total_failure(tmp_path):
    import os
    os.environ["DATABASE_URL"] = f"sqlite:///{tmp_path}/fx_fail.db"
    from src import database
    database.initialize_database()

    def fake_ticker(pair):
        raise RuntimeError("network down")

    with patch("yfinance.Ticker", side_effect=fake_ticker):
        fetched = refresh_all_rates()

    assert fetched == {}


def test_cache_ttl_expiry():
    from src.analysis import fx
    # Populate cache with a known stale entry
    fx._rate_cache["EUR"] = (0.50, time.time() - (fx._CACHE_TTL_SECONDS + 1))
    # Force the DB path to no-op so the fallback map is used
    with patch("src.database.get_db_connection", return_value=None):
        result = to_usd(100.0, "EUR")
    assert result == pytest.approx(100.0 * _FALLBACK_USD_PER_UNIT["EUR"])
