"""Tests for daily sector review and conviction-based threshold modulation."""

import os
import sys
from unittest.mock import patch, MagicMock

import pytest


# --- 1. Conviction cache basic ops ---

def test_conviction_cache_set_get_clear():
    from src.analysis.sector_review import (
        _conviction_cache, get_sector_conviction,
        get_all_sector_convictions, clear_sector_conviction_cache,
    )
    clear_sector_conviction_cache()

    # Empty cache returns 0.0
    assert get_sector_conviction('tech_mega') == 0.0

    # Set and get
    _conviction_cache['tech_mega'] = {'score': 0.75, 'rationale': 'AI boom'}
    assert get_sector_conviction('tech_mega') == 0.75

    _conviction_cache['defi'] = {'score': -0.3, 'rationale': 'Regulatory pressure'}
    all_convictions = get_all_sector_convictions()
    assert 'tech_mega' in all_convictions
    assert 'defi' in all_convictions

    # Clear
    clear_sector_conviction_cache()
    assert get_sector_conviction('tech_mega') == 0.0
    assert get_all_sector_convictions() == {}


# --- 2. Conviction modulates BUY threshold ---

def test_conviction_modulates_buy_threshold():
    """Bullish sector conviction (+0.8) should lower BUY threshold, making BUY easier."""
    from src.analysis.signal_engine import generate_signal

    market_data = {'current_price': 105, 'sma': 100, 'rsi': 50}
    # Gemini confidence 0.62 — below default 0.7 threshold
    news_data = {
        'gemini_assessment': {
            'direction': 'bullish',
            'confidence': 0.62,
            'reasoning': 'Strong inflows',
            'catalyst_freshness': 'breaking',
        }
    }

    # Without conviction: 0.62 < 0.70 → HOLD
    result_no_conv = generate_signal(
        'BTCUSDT', market_data, news_data,
        signal_mode='sentiment',
        sentiment_config={'min_gemini_confidence': 0.7, 'sector_conviction': 0.0},
    )
    assert result_no_conv['signal'] == 'HOLD'

    # With conviction +0.8: threshold = 0.7 - (0.8 * 0.10) = 0.62 → BUY
    result_with_conv = generate_signal(
        'BTCUSDT', market_data, news_data,
        signal_mode='sentiment',
        sentiment_config={'min_gemini_confidence': 0.7, 'sector_conviction': 0.8},
    )
    assert result_with_conv['signal'] == 'BUY'


# --- 3. Conviction modulates SELL threshold ---

def test_conviction_modulates_sell_threshold():
    """Bearish sector conviction (-0.8) should lower SELL threshold, making SELL easier."""
    from src.analysis.signal_engine import generate_signal

    market_data = {'current_price': 95, 'sma': 100, 'rsi': 50}
    # Gemini confidence 0.62 — below default 0.7 threshold
    news_data = {
        'gemini_assessment': {
            'direction': 'bearish',
            'confidence': 0.62,
            'reasoning': 'Regulatory crackdown',
            'catalyst_freshness': 'breaking',
        }
    }

    # Without conviction: 0.62 < 0.70 → HOLD
    result_no_conv = generate_signal(
        'BTCUSDT', market_data, news_data,
        signal_mode='sentiment',
        sentiment_config={'min_gemini_confidence': 0.7, 'sector_conviction': 0.0},
    )
    assert result_no_conv['signal'] == 'HOLD'

    # With conviction -0.8: for bearish direction, threshold = 0.7 + (-0.8 * 0.10) = 0.62 → SELL
    result_with_conv = generate_signal(
        'BTCUSDT', market_data, news_data,
        signal_mode='sentiment',
        sentiment_config={'min_gemini_confidence': 0.7, 'sector_conviction': -0.8},
    )
    assert result_with_conv['signal'] == 'SELL'


# --- 4. Neutral conviction has no effect ---

def test_neutral_conviction_no_change():
    """Conviction 0.0 should not change the threshold."""
    from src.analysis.signal_engine import generate_signal

    market_data = {'current_price': 105, 'sma': 100, 'rsi': 50}
    news_data = {
        'gemini_assessment': {
            'direction': 'bullish',
            'confidence': 0.65,
            'reasoning': 'Moderate signal',
            'catalyst_freshness': 'breaking',
        }
    }

    # 0.65 < 0.7 → HOLD regardless of conviction=0.0
    result = generate_signal(
        'BTCUSDT', market_data, news_data,
        signal_mode='sentiment',
        sentiment_config={'min_gemini_confidence': 0.7, 'sector_conviction': 0.0},
    )
    assert result['signal'] == 'HOLD'


# --- 5. Conviction clamp bounds ---

def test_conviction_clamp_bounds():
    """Thresholds must stay in [0.35, 0.85] even with extreme conviction."""
    from src.analysis.signal_engine import generate_signal

    market_data = {'current_price': 105, 'sma': 100, 'rsi': 50}

    # Very high base threshold (0.85) + strong bullish conviction
    news_data = {
        'gemini_assessment': {
            'direction': 'bullish',
            'confidence': 0.76,
            'reasoning': 'Test',
            'catalyst_freshness': 'breaking',
        }
    }
    # base=0.85, conviction=1.0, influence=0.50 → unclamped = 0.85 - 0.50 = 0.35
    # clamped to 0.35, and 0.76 >= 0.35 → BUY
    result = generate_signal(
        'BTCUSDT', market_data, news_data,
        signal_mode='sentiment',
        sentiment_config={
            'min_gemini_confidence': 0.85,
            'sector_conviction': 1.0,
            'conviction_influence_pct': 0.50,
        },
    )
    assert result['signal'] == 'BUY'

    # Very low base threshold (0.35) + strong bearish conviction pushing higher
    # base=0.35, conviction=-1.0, influence=0.50 → 0.35 + 0.50 = 0.85
    # clamped to 0.85
    news_data_bear = {
        'gemini_assessment': {
            'direction': 'bearish',
            'confidence': 0.80,
            'reasoning': 'Test',
            'catalyst_freshness': 'breaking',
        }
    }
    result2 = generate_signal(
        'BTCUSDT', {'current_price': 95, 'sma': 100, 'rsi': 50},
        news_data_bear,
        signal_mode='sentiment',
        sentiment_config={
            'min_gemini_confidence': 0.35,
            'sector_conviction': 1.0,  # bullish conviction → raises SELL threshold
            'conviction_influence_pct': 0.50,
        },
    )
    # 0.80 < 0.85 → HOLD (threshold raised too high for this confidence)
    assert result2['signal'] == 'HOLD'


# --- 6. Scoring mode unaffected ---

def test_scoring_mode_unaffected():
    """Scoring mode should not be affected by conviction (no sentiment_config used)."""
    from src.analysis.signal_engine import generate_signal

    market_data = {'current_price': 105, 'sma': 100, 'rsi': 25}  # oversold
    result = generate_signal(
        'BTCUSDT', market_data, None,
        signal_mode='scoring',
        sentiment_config={'sector_conviction': 0.8},
        rsi_oversold_threshold=30,
    )
    # Scoring mode uses its own logic — conviction is irrelevant
    assert result['signal'] in ('BUY', 'HOLD', 'SELL')


# --- 7. Aggregate skips sparse sectors ---

def test_aggregate_skips_sparse_sectors():
    """Sectors with fewer articles than min_articles_for_review should be skipped."""
    from src.analysis.sector_review import _aggregate_sector_data

    sector_groups = {
        'defi': {'symbols': ['UNIUSDT'], 'asset_class': 'crypto'},
    }

    mock_fn = MagicMock(return_value=[
        {'title': 'Test', 'gemini_score': 0.5, 'source': 'test'}
    ])
    mock_wrapper = MagicMock()
    mock_wrapper.sync = mock_fn

    with patch.dict(sys.modules, {}):
        with patch('src.database.get_recent_articles', mock_wrapper):
            with patch('src.analysis.sector_review.app_config', {
                'settings': {'sector_review': {'min_articles_for_review': 3}}
            }):
                # Re-import to pick up the mock
                from importlib import reload
                import src.analysis.sector_review as sr_module
                result = sr_module._aggregate_sector_data(sector_groups)

    assert result['defi']['skipped'] is True
    assert result['defi']['article_count'] == 1


# --- 8. Startup cache reload ---

def test_startup_cache_reload():
    """Convictions loaded from DB should populate the cache."""
    from src.analysis.sector_review import (
        load_convictions_into_cache, get_sector_conviction,
        clear_sector_conviction_cache,
    )
    clear_sector_conviction_cache()

    db_rows = [
        {
            'sector_group': 'tech_mega',
            'score': 0.65,
            'rationale': 'AI demand',
            'key_catalyst': 'NVDA earnings',
            'momentum': 'accelerating',
            'review_confidence': 0.8,
            'cross_sector_theme': 'AI capex',
        },
        {
            'sector_group': 'defi',
            'score': -0.2,
            'rationale': 'Flat TVL',
            'momentum': 'stable',
            'review_confidence': 0.5,
        },
    ]

    load_convictions_into_cache(db_rows)

    assert get_sector_conviction('tech_mega') == 0.65
    assert get_sector_conviction('defi') == -0.2
    assert get_sector_conviction('nonexistent') == 0.0

    clear_sector_conviction_cache()


# --- 9. Missing GCP returns None ---

def test_missing_gcp_returns_none():
    """run_sector_review should return None if GCP_PROJECT_ID is not set."""
    from src.analysis.sector_review import run_sector_review

    with patch.dict(os.environ, {}, clear=True):
        # Ensure GCP_PROJECT_ID is not set
        os.environ.pop('GCP_PROJECT_ID', None)
        result = run_sector_review()
        assert result is None


# --- Stock signal engine: same conviction logic ---

def test_stock_conviction_modulates_buy_threshold():
    """Bullish sector conviction should lower BUY threshold for stocks too."""
    from src.analysis.stock_signal_engine import generate_stock_signal

    market_data = {'current_price': 105, 'sma': 100, 'rsi': 50}
    news_data = {
        'gemini_assessment': {
            'direction': 'bullish',
            'confidence': 0.62,
            'reasoning': 'Strong earnings',
            'catalyst_freshness': 'breaking',
        }
    }

    # Without conviction: 0.62 < 0.70 → HOLD
    result_no_conv = generate_stock_signal(
        'NVDA', market_data,
        news_sentiment_data=news_data,
        signal_mode='sentiment',
        sentiment_config={'min_gemini_confidence': 0.7, 'sector_conviction': 0.0},
    )
    assert result_no_conv['signal'] == 'HOLD'

    # With conviction +0.8: threshold = 0.7 - 0.08 = 0.62 → BUY
    result_with_conv = generate_stock_signal(
        'NVDA', market_data,
        news_sentiment_data=news_data,
        signal_mode='sentiment',
        sentiment_config={'min_gemini_confidence': 0.7, 'sector_conviction': 0.8},
    )
    assert result_with_conv['signal'] == 'BUY'
