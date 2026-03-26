"""News pipeline — collects news, runs Gemini analysis, and triggers market alerts."""

import asyncio

from src.analysis.gemini_news_analyzer import analyze_news_impact, analyze_news_with_search
from src.analysis.news_velocity import compute_news_velocity
from src.analysis.market_alerts import run_market_alerts
from src.collectors.news_data import collect_news_sentiment
from src.database import get_recent_articles
from src.logger import log
from src.notify.telegram_bot import send_news_alert, send_market_event_alert


def build_trade_feedback_context(days=14, limit=20) -> str:
    """Build trade outcome feedback string for Gemini prompt injection."""
    try:
        from src.analysis.signal_attribution import get_recent_trade_outcomes
        outcomes = get_recent_trade_outcomes(days=days, limit=limit)
        if not outcomes:
            return ''

        lines = []
        for t in outcomes:
            sym = t.get('symbol', '?')
            conf = t.get('gemini_confidence')
            cat = t.get('catalyst_type', '?')
            exit_r = t.get('exit_reason', '?')
            pnl_pct = t.get('trade_pnl_pct')
            pnl_str = f"{pnl_pct:+.1f}%" if pnl_pct is not None else "?"

            # Derive lesson from outcome
            if exit_r and 'stop_loss' in exit_r:
                lesson = "catalyst did not sustain price movement"
            elif exit_r and 'take_profit' in exit_r:
                lesson = "catalyst confirmed, strong follow-through"
            elif exit_r and 'trailing' in exit_r:
                lesson = "partial move captured before reversal"
            elif exit_r and 'analyst' in exit_r:
                lesson = "position analyst recommended exit"
            else:
                lesson = f"{exit_r or 'closed'}"

            conf_str = f"conf={conf:.2f}" if conf else "conf=?"
            lines.append(f"- {sym}: BUY ({conf_str}, {cat}) -> {exit_r} {pnl_str}. {lesson}")

        return '\n'.join(lines)
    except Exception as e:
        log.debug(f"Trade feedback context unavailable: {e}")
        return ''


def build_regime_context(macro_regime_result: dict = None) -> str:
    """Build regime trajectory context string for Gemini prompt injection."""
    try:
        from src.analysis.macro_regime import get_regime_trajectory
        trajectory = get_regime_trajectory()
        summary = trajectory.get('summary', '')
        if not summary:
            return ''

        regime = trajectory.get('current_regime', '?')
        direction = trajectory.get('regime_direction', '?')
        days = trajectory.get('days_in_regime', 0)

        lines = [
            f"MARKET REGIME: {regime} for {days}d ({direction})",
            f"  {summary}",
        ]
        if regime == 'RISK_OFF':
            lines.append("  Apply extra scrutiny to BUY signals. Require stronger catalysts.")
        elif regime == 'RISK_ON':
            lines.append("  Risk appetite is strong. Standard confidence thresholds apply.")

        return '\n'.join(lines)
    except Exception as e:
        log.debug(f"Regime context unavailable: {e}")
        return ''


def build_source_reliability_context(days=30, min_signals=3) -> str:
    """Build source reliability context string for Gemini prompt injection."""
    try:
        from src.analysis.signal_attribution import get_source_performance
        sources = get_source_performance(days=days)
        if not sources:
            return ''

        # Filter to sources with enough trades
        qualified = [s for s in sources if s.get('total_signals', 0) >= min_signals]
        if len(qualified) < 2:
            return ''

        qualified.sort(key=lambda s: s.get('win_rate', 0), reverse=True)
        top = qualified[:5]
        bottom = [s for s in qualified if s.get('win_rate', 0) < 0.40][:5]

        lines = ["SOURCE RELIABILITY (from our trade outcomes):"]
        if top:
            top_str = ', '.join(
                f"{s['source_name']} ({s['win_rate']:.0%} WR, {s['total_signals']} trades)"
                for s in top)
            lines.append(f"  Top performers: {top_str}")
        if bottom:
            bot_str = ', '.join(
                f"{s['source_name']} ({s['win_rate']:.0%} WR, {s['total_signals']} trades)"
                for s in bottom)
            lines.append(f"  Underperformers: {bot_str}")
        lines.append("  Weight corroborated stories from top sources higher.")

        return '\n'.join(lines)
    except Exception as e:
        log.debug(f"Source reliability context unavailable: {e}")
        return ''


def build_symbol_memory_context(days=30, min_trades=3) -> str:
    """Build symbol win rate memory context string for Gemini prompt injection."""
    try:
        from src.analysis.signal_attribution import get_symbol_win_rates
        win_rates = get_symbol_win_rates(days=days, min_trades=min_trades)
        if not win_rates:
            return ''

        caution = {s: d for s, d in win_rates.items() if d['win_rate'] < 0.30}
        strong = {s: d for s, d in win_rates.items() if d['win_rate'] > 0.60}

        if not caution and not strong:
            return ''

        lines = ["SYMBOL TRACK RECORD (from our recent trades):"]
        if caution:
            caution_str = ', '.join(
                f"{s} ({d['wins']}W/{d['losses']}L={d['win_rate']:.0%})"
                for s, d in sorted(caution.items(), key=lambda x: x[1]['win_rate']))
            lines.append(f"  CAUTION — poor history: {caution_str}")
            lines.append("  Require stronger catalysts for CAUTION symbols.")
        if strong:
            strong_str = ', '.join(
                f"{s} ({d['wins']}W/{d['losses']}L={d['win_rate']:.0%})"
                for s, d in sorted(strong.items(), key=lambda x: -x[1]['win_rate']))
            lines.append(f"  Strong performers: {strong_str}")

        return '\n'.join(lines)
    except Exception as e:
        log.debug(f"Symbol memory context unavailable: {e}")
        return ''


async def collect_and_analyze_news(
    all_symbols: list,
    current_prices_dict: dict,
    settings: dict,
    macro_regime_result: dict = None,
    ytd_changes: dict = None,
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
    # Batch by asset class for focused prompts (500 RPD free budget).
    if use_grounded_search:
        cache_ttl = news_config.get('cache_ttl_minutes', 15)
        batches = _split_symbols_into_batches(all_symbols, settings)

        # Build feedback context once (shared across all batches)
        trade_feedback = await asyncio.to_thread(build_trade_feedback_context)
        regime_context = build_regime_context(macro_regime_result)
        source_reliability = await asyncio.to_thread(build_source_reliability_context)
        symbol_memory = await asyncio.to_thread(build_symbol_memory_context)

        # Sequential batch calls with small delay to avoid overwhelming flash-lite.
        batch_results = []
        for i, batch in enumerate(batches):
            result = await asyncio.to_thread(
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
                trade_feedback_context=trade_feedback,
                regime_context=regime_context,
                source_reliability_context=source_reliability,
                symbol_memory_context=symbol_memory,
                ytd_changes=ytd_changes,
            )
            batch_results.append(result)
            if i < len(batches) - 1:
                await asyncio.sleep(1)  # 1s delay between batches

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

    # Max symbols per batch — balance between API call count and grounding reliability.
    # 25 per batch × ~16 batches, with 90-min cache → ~128 calls/day (within 500 free RPD)
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
