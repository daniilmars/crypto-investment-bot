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

    Always runs RSS+scraping first (free, feeds article DB, velocity, IPO).
    Then uses grounded search for Gemini assessment (free within 1,500/day),
    falling back to analyze_news_impact if grounded search fails.

    Returns (gemini_assessments, news_per_symbol).
    """
    news_config = settings.get('news_analysis', {})
    gemini_assessments = None
    news_per_symbol = {}
    use_grounded_search = news_config.get('use_grounded_search', False)

    # Step 1: Always collect news via RSS + web scraping (free)
    # This feeds article DB, velocity detection, IPO detection, and news alerts.
    news_result = await asyncio.to_thread(collect_news_sentiment, all_symbols)
    news_per_symbol = news_result.get('per_symbol', {})
    triggered_symbols = news_result.get('triggered_symbols', [])

    if not news_config.get('enabled', False):
        return gemini_assessments, news_per_symbol

    # Step 2: Prepare collected data for Gemini (used by both paths)
    headlines_by_symbol = {}
    archived_articles_by_symbol = {}
    news_stats_by_symbol = {}
    scored_articles_by_symbol = {}
    symbols_with_news = [sym for sym in all_symbols if sym in news_per_symbol]

    if symbols_with_news:
        for sym in symbols_with_news:
            sym_data = news_per_symbol.get(sym, {})
            headlines_by_symbol[sym] = sym_data.get('headlines', [])
            top_scored = sym_data.get('top_scored_articles', [])
            if top_scored:
                scored_articles_by_symbol[sym] = top_scored

        for sym in symbols_with_news:
            try:
                archived = await get_recent_articles(sym, hours=24)
                if archived:
                    archived_articles_by_symbol[sym] = archived
            except Exception as e:
                log.warning(f"Failed to fetch archived articles for {sym}: {e}")

        news_stats_by_symbol = _build_news_stats(
            symbols_with_news, news_per_symbol)

    # Step 3: Get Gemini assessment — try grounded search first (free tier)
    # Batch by asset class for focused prompts (1,500 free calls/day budget).
    if use_grounded_search:
        cache_ttl = news_config.get('cache_ttl_minutes', 15)
        batches = _split_symbols_into_batches(all_symbols, settings)

        # Limit concurrency to avoid Gemini rate limits
        sem = asyncio.Semaphore(4)

        async def _call_batch(batch):
            async with sem:
                return await asyncio.to_thread(
                    analyze_news_with_search,
                    batch, current_prices_dict,
                    cache_ttl_minutes=cache_ttl,
                    headlines_by_symbol={s: headlines_by_symbol[s]
                                         for s in batch if s in headlines_by_symbol} or None,
                    archived_articles_by_symbol={s: archived_articles_by_symbol[s]
                                                 for s in batch if s in archived_articles_by_symbol} or None,
                    news_stats_by_symbol={s: news_stats_by_symbol[s]
                                          for s in batch if s in news_stats_by_symbol} or None,
                    scored_articles_by_symbol={s: scored_articles_by_symbol[s]
                                               for s in batch if s in scored_articles_by_symbol} or None,
                )

        batch_results = await asyncio.gather(
            *[_call_batch(b) for b in batches])

        # Merge batch results into single gemini_assessments dict
        gemini_assessments = _merge_batch_results(batch_results)
        if gemini_assessments is None:
            log.info("Grounded search unavailable — falling back to "
                     "analyze_news_impact.")

    # Step 4: Fall back to analyze_news_impact if grounded search
    # failed or is disabled
    if gemini_assessments is None and symbols_with_news:
        current_prices_for_news = {}
        for sym in symbols_with_news:
            sym_data = news_per_symbol.get(sym, {})
            current_prices_for_news[sym] = current_prices_dict.get(
                sym, sym_data.get('current_price', 0))

        gemini_assessments = await asyncio.to_thread(
            analyze_news_impact,
            headlines_by_symbol, current_prices_for_news,
            archived_articles_by_symbol=archived_articles_by_symbol or None,
            news_stats_by_symbol=news_stats_by_symbol or None,
            scored_articles_by_symbol=scored_articles_by_symbol or None,
        )

    # Send news alerts for triggered symbols (regardless of assessment path)
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


def _split_symbols_into_batches(
    all_symbols: list, settings: dict
) -> list[list[str]]:
    """Split symbols into focused batches for grounded search.

    Groups: crypto (by sector), US stocks, EU stocks, Asia stocks.
    Each batch gets its own grounded search call for better prompt focus.
    Roughly 4-8 batches → 384-768 calls/day (well within 1,500 free tier).
    """
    from src.analysis.sector_limits import get_symbol_group, _ensure_loaded, _CRYPTO_GROUPS
    _ensure_loaded()

    _EU_SUFFIXES = ('.L', '.DE', '.PA', '.AS', '.SW', '.MC', '.MI',
                    '.CO', '.ST', '.HE')
    _ASIA_SUFFIXES = ('.T', '.HK', '.KS', '.AX', '.NS', '.TW', '.SI')

    crypto_syms = []
    us_stock_syms = []
    eu_stock_syms = []
    asia_stock_syms = []
    other_syms = []

    for sym in all_symbols:
        group = get_symbol_group(sym)
        if group in _CRYPTO_GROUPS:
            crypto_syms.append(sym)
        elif any(sym.endswith(s) for s in _EU_SUFFIXES):
            eu_stock_syms.append(sym)
        elif any(sym.endswith(s) for s in _ASIA_SUFFIXES):
            asia_stock_syms.append(sym)
        elif group is not None:
            us_stock_syms.append(sym)
        else:
            other_syms.append(sym)

    batches = []

    # Max symbols per batch — keeps Gemini responses reliable JSON
    # 25 per batch × ~14 batches × 96 cycles = ~1,344 calls/day (within 1,500 free)
    max_batch = 25

    for sym_list in [crypto_syms, us_stock_syms, eu_stock_syms,
                     asia_stock_syms, other_syms]:
        for i in range(0, len(sym_list), max_batch):
            chunk = sym_list[i:i + max_batch]
            if chunk:
                batches.append(chunk)

    log.info(f"Split {len(all_symbols)} symbols into {len(batches)} "
             f"grounded search batches: {[len(b) for b in batches]}")
    return batches


def _merge_batch_results(batch_results: list) -> dict | None:
    """Merge multiple grounded search results into a single assessment dict."""
    merged_assessments = {}
    market_moods = []
    cross_themes = []

    for result in batch_results:
        if result is None:
            continue
        sa = result.get('symbol_assessments', {})
        merged_assessments.update(sa)
        mood = result.get('market_mood')
        if mood:
            market_moods.append(mood)
        theme = result.get('cross_asset_theme')
        if theme:
            cross_themes.append(theme)

    if not merged_assessments:
        return None

    return {
        'symbol_assessments': merged_assessments,
        'market_mood': '; '.join(market_moods) if market_moods else 'mixed',
        'cross_asset_theme': '; '.join(cross_themes) if cross_themes else None,
    }


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
