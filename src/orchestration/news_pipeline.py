"""News pipeline — collects news, runs Gemini analysis, and triggers market alerts."""

import asyncio

from src.analysis.gemini_news_analyzer import analyze_news_impact, analyze_news_with_search
from src.analysis.news_velocity import compute_news_velocity
from src.analysis.market_alerts import run_market_alerts
from src.collectors.news_data import collect_news_sentiment
from src.database import get_recent_articles
from src.logger import log
from src.notify.telegram_bot import send_news_alert, send_market_event_alert


async def collect_and_analyze_news(
    all_symbols: list,
    current_prices_dict: dict,
    settings: dict,
) -> tuple:
    """Collect news and run Gemini analysis for all symbols.

    Returns (gemini_assessments, news_per_symbol).
    """
    news_config = settings.get('news_analysis', {})
    gemini_assessments = None
    news_per_symbol = {}
    use_grounded_search = news_config.get('use_grounded_search', False)

    # --- Optional: Gemini with Google Search grounding (expensive) ---
    if news_config.get('enabled', False) and use_grounded_search:
        cache_ttl = news_config.get('cache_ttl_minutes', 30)
        gemini_assessments = await asyncio.to_thread(
            analyze_news_with_search,
            all_symbols, current_prices_dict, cache_ttl_minutes=cache_ttl)

    # --- Primary path: RSS + web scraping + plain Gemini (cheap) ---
    if gemini_assessments is None:
        if use_grounded_search:
            log.info("Grounded search unavailable — falling back to RSS+scraping pipeline.")
        news_result = await asyncio.to_thread(collect_news_sentiment, all_symbols)
        news_per_symbol = news_result.get('per_symbol', {})
        triggered_symbols = news_result.get('triggered_symbols', [])

        symbols_with_news = [sym for sym in all_symbols if sym in news_per_symbol]
        if symbols_with_news and news_config.get('enabled', False):
            headlines_by_symbol = {}
            current_prices_for_news = {}
            for sym in symbols_with_news:
                sym_data = news_per_symbol.get(sym, {})
                headlines_by_symbol[sym] = sym_data.get('headlines', [])
                current_prices_for_news[sym] = current_prices_dict.get(
                    sym, sym_data.get('current_price', 0))

            # Enrich Gemini prompt with archived articles from DB
            archived_articles_by_symbol = {}
            for sym in symbols_with_news:
                try:
                    archived = await get_recent_articles(sym, hours=24)
                    if archived:
                        archived_articles_by_symbol[sym] = archived
                except Exception as e:
                    log.warning(f"Failed to fetch archived articles for {sym}: {e}")

            # Build news stats per symbol for Gemini context
            news_stats_by_symbol = _build_news_stats(symbols_with_news, news_per_symbol)

            gemini_assessments = await asyncio.to_thread(
                analyze_news_impact,
                headlines_by_symbol, current_prices_for_news,
                archived_articles_by_symbol=archived_articles_by_symbol or None,
                news_stats_by_symbol=news_stats_by_symbol or None,
            )
            if triggered_symbols:
                await send_news_alert(triggered_symbols, news_per_symbol,
                                      gemini_assessments=gemini_assessments)

    return gemini_assessments, news_per_symbol


async def run_proactive_market_alerts(
    all_symbols: list,
    settings: dict,
    gemini_assessments,
    news_per_symbol: dict,
):
    """Run proactive market event alerts for all symbols."""
    try:
        def _compute_velocities():
            cache = {}
            for sym in all_symbols:
                try:
                    cache[sym] = compute_news_velocity(sym)
                except Exception:
                    pass
            return cache

        news_velocity_cache = await asyncio.to_thread(_compute_velocities)

        stock_wl = settings.get('stock_trading', {}).get('watch_list', [])
        market_alerts = await asyncio.to_thread(
            run_market_alerts,
            gemini_assessments=gemini_assessments,
            news_per_symbol=news_per_symbol,
            news_velocity_cache=news_velocity_cache,
            stock_watchlist=stock_wl,
        )
        for alert in market_alerts:
            await send_market_event_alert(alert)
    except Exception as e:
        log.warning(f"Market alerts failed: {e}")


def _build_news_stats(symbols_with_news: list, news_per_symbol: dict) -> dict:
    """Build per-symbol news statistics for Gemini context."""
    stats = {}
    for sym in symbols_with_news:
        sym_data = news_per_symbol.get(sym, {})
        if not sym_data:
            continue
        scores = sym_data.get('sentiment_scores', [])
        volume = sym_data.get('news_volume', len(sym_data.get('headlines', [])))
        positive = sum(1 for s in scores if s > 0.05) if scores else 0
        negative = sum(1 for s in scores if s < -0.05) if scores else 0
        total = max(len(scores), 1)
        if len(scores) >= 2:
            mean_s = sum(scores) / len(scores)
            variance = sum((s - mean_s) ** 2 for s in scores) / len(scores)
            sent_vol = variance ** 0.5
        else:
            sent_vol = 0.0
        stats[sym] = {
            'news_volume': volume,
            'positive_ratio': positive / total,
            'negative_ratio': negative / total,
            'sentiment_volatility': sent_vol,
        }
    return stats
