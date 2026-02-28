import json
import os
import re
import time

import vertexai
from vertexai.generative_models import GenerativeModel

from src.logger import log

# --- JSON parsing helpers ---

_FENCE_RE = re.compile(r'^```(?:json)?\s*\n?(.*?)```\s*$', re.DOTALL)


def _parse_gemini_json(text: str) -> dict:
    """Strip markdown code-fence variants and parse JSON.

    Handles:
      - Plain JSON (no fences)
      - ```json ... ```
      - ``` ... ```
    Raises json.JSONDecodeError on failure.
    """
    text = text.strip()
    m = _FENCE_RE.match(text)
    if m:
        text = m.group(1).strip()
    return json.loads(text)


def _validate_gemini_response(result: dict, required_keys: list, context: str) -> dict:
    """Warn about missing keys but return result unchanged."""
    missing = [k for k in required_keys if k not in result]
    if missing:
        log.warning(f"Gemini response ({context}) missing keys: {missing}")
    return result

# --- Gemini Response Cache ---
# Key: frozenset of sorted symbols, Value: (timestamp, result)
_gemini_cache: dict[frozenset, tuple[float, dict]] = {}


def clear_gemini_cache():
    """Clears the Gemini response cache. Useful for tests."""
    _gemini_cache.clear()


_gemini_article_cache: dict[str, float] = {}


def clear_gemini_article_cache():
    """Clears the per-article Gemini score cache. Useful for tests."""
    _gemini_article_cache.clear()


def _score_single_batch(model, articles: list) -> list | None:
    """Sends a single batch of articles to Gemini and returns a list of scores.

    Returns None on any failure (parse error, count mismatch, API error).
    """
    numbered_lines = []
    for i, art in enumerate(articles, 1):
        title = art.get('title', '')
        desc = (art.get('description', '') or '')[:200]
        numbered_lines.append(f"{i}. {title} | {desc}")

    articles_text = "\n".join(numbered_lines)

    prompt = (
        "Score each article's financial/crypto market sentiment from -1.0 "
        "(very bearish) to +1.0 (very bullish). 0.0 = neutral or irrelevant.\n\n"
        "Consider:\n"
        "- Positive catalysts (ETF approvals, partnerships, earnings beats) → positive\n"
        "- Negative catalysts (hacks, regulatory bans, earnings misses) → negative\n"
        "- Opinion pieces, vague predictions, irrelevant news → near 0.0\n\n"
        f"Articles:\n{articles_text}\n\n"
        "Respond ONLY with a JSON array of numbers in the same order:\n"
        f"[0.6, -0.3, 0.0, ...]"
    )

    try:
        response = model.generate_content(prompt)
        text = response.text.strip()
        scores = _parse_gemini_json(text)

        if not isinstance(scores, list):
            log.warning(f"Gemini article scoring returned non-list: {type(scores)}")
            return None

        if len(scores) != len(articles):
            log.warning(f"Gemini article scoring count mismatch: "
                        f"expected {len(articles)}, got {len(scores)}")
            return None

        # Validate and clamp each score
        clamped = []
        for s in scores:
            try:
                val = float(s)
                val = max(-1.0, min(1.0, val))
                clamped.append(val)
            except (TypeError, ValueError):
                log.warning(f"Invalid score value from Gemini: {s}")
                return None

        return clamped

    except json.JSONDecodeError as e:
        log.error(f"Failed to parse Gemini article scores as JSON: {e}")
        return None
    except Exception as e:
        log.error(f"Gemini article scoring batch failed: {e}")
        return None


def score_articles_batch(articles: list, batch_size: int = 50) -> dict:
    """Scores a list of articles using Gemini 2.0 Flash in batches.

    Args:
        articles: list of dicts with 'title', 'description', 'title_hash' keys.
        batch_size: number of articles per Gemini API call.

    Returns:
        {title_hash: float} mapping of scores for successfully scored articles.
        Returns empty dict if GCP_PROJECT_ID is not set or on total failure.
    """
    if not articles:
        return {}

    project_id = os.environ.get('GCP_PROJECT_ID')
    if not project_id:
        log.warning("GCP_PROJECT_ID not set — skipping Gemini article scoring.")
        return {}

    location = os.environ.get('GCP_LOCATION', 'us-central1')

    try:
        vertexai.init(project=project_id, location=location)
        model = GenerativeModel('gemini-2.0-flash')
    except Exception as e:
        log.error(f"Failed to initialize Gemini for article scoring: {e}")
        return {}

    all_scores = {}

    for i in range(0, len(articles), batch_size):
        batch = articles[i:i + batch_size]
        scores = _score_single_batch(model, batch)

        if scores is None:
            log.warning(f"Gemini article scoring batch {i // batch_size + 1} failed, skipping.")
            continue

        for art, score in zip(batch, scores):
            title_hash = art.get('title_hash')
            if title_hash:
                all_scores[title_hash] = score

    log.info(f"Gemini article scoring complete: {len(all_scores)}/{len(articles)} articles scored.")
    return all_scores


def analyze_news_with_search(symbols: list, current_prices: dict,
                             cache_ttl_minutes: int = 30) -> dict | None:
    """
    Uses Gemini with Google Search grounding to find and analyze news for the given symbols.

    Instead of relying on RSS feeds + VADER, this lets Gemini search the web itself
    and return grounded sentiment assessments with citations.

    Args:
        symbols: list of ticker symbols (e.g. ['BTC', 'ETH', 'AAPL'])
        current_prices: {symbol: float} — current prices for context

    Returns:
        dict with 'symbol_assessments' and 'market_mood', or None on failure.
    """
    if not symbols:
        return None

    # --- Cache lookup (before any API/env checks) ---
    cache_key = frozenset(sorted(symbols))
    cached = _gemini_cache.get(cache_key)
    if cached is not None:
        cached_time, cached_result = cached
        age_minutes = (time.time() - cached_time) / 60
        if age_minutes < cache_ttl_minutes:
            log.info(f"Gemini cache hit for {list(symbols)} (age={age_minutes:.1f}m)")
            return cached_result

    project_id = os.environ.get('GCP_PROJECT_ID')
    location = os.environ.get('GCP_LOCATION', 'us-central1')

    if not project_id:
        log.warning("GCP_PROJECT_ID not set — skipping Gemini grounded news analysis.")
        return None

    try:
        from google import genai
        from google.genai.types import GenerateContentConfig, Tool, GoogleSearch

        client = genai.Client(vertexai=True, project=project_id, location=location)

        # Build price context
        price_lines = []
        for sym in symbols:
            price = current_prices.get(sym)
            if price:
                price_lines.append(f"- {sym}: ${price:,.2f}")
            else:
                price_lines.append(f"- {sym}: price unknown")
        price_context = "\n".join(price_lines)

        symbols_str = ", ".join(symbols)
        prompt = (
            f"Search for the latest news (last 24-48 hours) about these assets: {symbols_str}\n\n"
            f"Current prices:\n{price_context}\n\n"
            "For each asset, assess the short-term market sentiment based on the news you find.\n\n"
            "Respond ONLY with valid JSON in this exact format:\n"
            "{\n"
            '  "symbol_assessments": {\n'
            '    "SYMBOL": {"direction": "bullish|bearish|neutral", "confidence": 0.0, "reasoning": "..."}\n'
            "  },\n"
            '  "market_mood": "..."\n'
            "}\n\n"
            "Rules:\n"
            "- direction must be one of: bullish, bearish, neutral\n"
            "- confidence must be a float between 0.0 and 1.0\n"
            "- reasoning should be one sentence summarizing the key news drivers\n"
            "- market_mood should be a brief overall sentiment phrase\n"
            "- Include ALL requested symbols in symbol_assessments\n"
            "- Do not include any text outside the JSON object"
        )

        response = client.models.generate_content(
            model="gemini-2.0-flash",
            contents=prompt,
            config=GenerateContentConfig(
                tools=[Tool(google_search=GoogleSearch())],
                temperature=0.2,
            ),
        )

        text = response.text.strip()
        result = _parse_gemini_json(text)
        _validate_gemini_response(result, ['symbol_assessments', 'market_mood'],
                                  'analyze_news_with_search')
        log.info(f"Gemini grounded news analysis complete: mood={result.get('market_mood')}, "
                 f"symbols={list(result.get('symbol_assessments', {}).keys())}")
        # --- Cache store ---
        _gemini_cache[cache_key] = (time.time(), result)
        return result

    except ImportError:
        log.warning("google-genai SDK not installed — skipping grounded news analysis.")
        return None
    except json.JSONDecodeError as e:
        log.error(f"Failed to parse Gemini grounded news response as JSON: {e}")
        return None
    except Exception as e:
        log.error(f"Gemini grounded news analysis failed: {e}")
        return None


def analyze_news_impact(headlines_by_symbol: dict, current_prices: dict,
                        archived_articles_by_symbol: dict = None,
                        news_stats_by_symbol: dict = None) -> dict | None:
    """
    Uses Vertex AI Gemini to analyze news headlines and assess market impact per symbol.

    Args:
        headlines_by_symbol: {symbol: [headline1, headline2, ...]}
        current_prices: {symbol: float}
        archived_articles_by_symbol: {symbol: [{'title': ..., 'source': ..., 'vader_score': ...}, ...]}
            Optional recent archived articles to enrich the prompt.
        news_stats_by_symbol: {symbol: {'sentiment_volatility': float, 'positive_ratio': float,
            'negative_ratio': float, 'news_volume': int}}
            Optional pre-computed news statistics per symbol.

    Returns:
        dict with 'symbol_assessments' and 'market_mood', or None on failure.
    """
    project_id = os.environ.get('GCP_PROJECT_ID')
    location = os.environ.get('GCP_LOCATION', 'us-central1')

    if not project_id:
        log.warning("GCP_PROJECT_ID not set — skipping Gemini news analysis.")
        return None

    if not headlines_by_symbol:
        return None

    try:
        vertexai.init(project=project_id, location=location)
        model = GenerativeModel('gemini-2.0-flash')

        # Build the prompt
        symbol_sections = []
        for symbol, headlines in headlines_by_symbol.items():
            price = current_prices.get(symbol, "unknown")
            top_headlines = headlines[:10]
            section = f"**{symbol}** (current price: ${price})\n" + "\n".join(
                f"- {h}" for h in top_headlines
            )

            # Enrich with archived articles (recent headlines from DB)
            if archived_articles_by_symbol:
                archived = archived_articles_by_symbol.get(symbol, [])
                if archived:
                    archive_lines = []
                    for a in archived[:5]:
                        source = a.get('source', '?')
                        category = a.get('category', '')
                        cat_tag = f" ({category})" if category else ""
                        line = f"- [{source}{cat_tag}] {a.get('title', '')}"
                        desc = a.get('description', '')
                        if desc and len(desc) > 50:
                            line += f"\n  Body excerpt: {desc[:500]}"
                        archive_lines.append(line)
                    section += "\n\nRecent archived headlines:\n" + "\n".join(archive_lines)

            # Add pre-computed news stats if available
            if news_stats_by_symbol:
                stats = news_stats_by_symbol.get(symbol)
                if stats:
                    section += (
                        f"\n\nNews stats (pre-computed):"
                        f"\n- Volume: {stats.get('news_volume', 0)} articles"
                        f"\n- Positive/Negative ratio: {stats.get('positive_ratio', 0):.0%} / {stats.get('negative_ratio', 0):.0%}"
                        f"\n- Sentiment volatility: {stats.get('sentiment_volatility', 0):.3f}"
                    )

            symbol_sections.append(section)

        headlines_text = "\n\n".join(symbol_sections)

        prompt = (
            "You are a crypto/financial news analyst for a trading bot that runs 15-minute cycles "
            "with 3.5% stop-loss and 8% take-profit. Your job is to assess short-term (<24h) "
            "market impact from news.\n\n"
            "Analyze these recent headlines using the following 4-step framework:\n\n"
            "STEP 1 — CATALYST SCAN: Identify concrete events vs noise.\n"
            "- Catalysts: regulatory action, ETF approval/rejection, exchange hack/exploit, "
            "major partnership, macro policy (Fed, CPI), protocol upgrade, large fund flow.\n"
            "- Noise: opinion pieces, price predictions, recycled narratives, vague 'experts say' articles.\n"
            "- Only catalysts should drive confidence above 0.6.\n\n"
            "STEP 2 — SOURCE WEIGHT: Not all sources are equal.\n"
            "- Tier 1 (high weight): regulatory filings, wire services (Reuters, AP), exchange announcements.\n"
            "- Tier 2 (medium): CoinDesk, CoinTelegraph, Bloomberg, established financial media.\n"
            "- Tier 3 (low): blogs, KOLs, unknown sources, articles without body text.\n"
            "- Articles with body excerpts are more reliable than headline-only.\n\n"
            "STEP 3 — CONSENSUS vs DIVERGENCE: Do headlines agree?\n"
            "- If most headlines point the same direction → higher confidence.\n"
            "- If headlines conflict (some bullish, some bearish) → flag divergence, lower confidence.\n"
            "- Note the sentiment_divergence in your response.\n\n"
            "STEP 4 — TIME HORIZON: Only short-term catalysts matter.\n"
            "- Events happening now or within 24h → can drive high confidence.\n"
            "- Speculative future events (months away) → low confidence regardless of importance.\n"
            "- Stale news (>48h old, already priced in) → neutral/low confidence.\n\n"
            f"--- Headlines by Symbol ---\n{headlines_text}\n\n"
            "For each symbol, provide:\n"
            "- direction: one of 'bullish', 'bearish', or 'neutral'\n"
            "- confidence: a float between 0.0 and 1.0\n"
            "- reasoning: a brief one-sentence explanation\n"
            "- catalyst_type: one of 'regulatory', 'etf', 'hack_exploit', 'macro', 'partnership', "
            "'protocol_upgrade', 'fund_flow', 'earnings', 'none' (use 'none' if no real catalyst)\n"
            "- catalyst_freshness: one of 'breaking', 'recent', 'stale', 'none'\n"
            "- sentiment_divergence: true if headlines conflict, false if consensus\n"
            "- key_headline: the single most impactful headline driving your assessment\n\n"
            "Also provide:\n"
            "- 'market_mood': a brief overall sentiment phrase\n"
            "- 'cross_asset_theme': a one-sentence theme if multiple assets share a common driver "
            "(e.g. 'broad risk-off on Fed hawkishness'), or null if none\n\n"
            "Respond ONLY with valid JSON in this exact format:\n"
            "{\n"
            '  "symbol_assessments": {\n'
            '    "SYMBOL": {"direction": "bullish|bearish|neutral", "confidence": 0.0, '
            '"reasoning": "...", "catalyst_type": "...", "catalyst_freshness": "...", '
            '"sentiment_divergence": false, "key_headline": "..."}\n'
            "  },\n"
            '  "market_mood": "...",\n'
            '  "cross_asset_theme": "..." or null\n'
            "}"
        )

        response = model.generate_content(prompt)
        text = response.text.strip()
        result = _parse_gemini_json(text)
        _validate_gemini_response(result, ['symbol_assessments', 'market_mood'],
                                  'analyze_news_impact')
        log.info(f"Gemini news analysis complete: mood={result.get('market_mood')}, "
                 f"symbols={list(result.get('symbol_assessments', {}).keys())}")
        return result

    except json.JSONDecodeError as e:
        log.error(f"Failed to parse Gemini news response as JSON: {e}")
        return None
    except Exception as e:
        log.error(f"Gemini news analysis failed: {e}")
        return None


def analyze_position_health(position: dict, current_price: float,
                            recent_headlines: list, technical_data: dict,
                            hours_held: float = None,
                            trailing_stop_info: dict = None) -> dict | None:
    """
    Asks Gemini whether an open position should be held or exited.

    Args:
        position: dict with keys: symbol, entry_price, quantity, entry_timestamp
        current_price: current market price
        recent_headlines: list of headline strings (last 10)
        technical_data: dict with keys: rsi, sma, regime
        hours_held: how long the position has been open (in hours), optional
        trailing_stop_info: dict with trailing stop state, optional
            keys: peak_price, trailing_active, pnl_percentage, activation_threshold

    Returns:
        dict with keys: recommendation ("hold"|"exit"), confidence (0.0-1.0), reasoning,
        risk_level ("green"|"yellow"|"red"), primary_risk
        or None on failure.
    """
    project_id = os.environ.get('GCP_PROJECT_ID')
    location = os.environ.get('GCP_LOCATION', 'us-central1')

    if not project_id:
        log.warning("GCP_PROJECT_ID not set — skipping position health analysis.")
        return None

    symbol = position.get('symbol', 'UNKNOWN')
    entry_price = position.get('entry_price', 0)
    pnl_pct = ((current_price - entry_price) / entry_price * 100) if entry_price > 0 else 0

    try:
        vertexai.init(project=project_id, location=location)
        model = GenerativeModel('gemini-2.0-flash')

        headlines_text = "\n".join(f"- {h}" for h in recent_headlines[:10]) if recent_headlines else "No recent headlines."

        # Build trailing stop context
        trailing_context = ""
        if trailing_stop_info:
            peak = trailing_stop_info.get('peak_price')
            active = trailing_stop_info.get('trailing_active', False)
            activation = trailing_stop_info.get('activation_threshold', 0.02)
            if peak:
                trailing_context = (
                    f"\n**Trailing Stop State:**\n"
                    f"- Peak price seen: ${peak:,.2f}\n"
                    f"- Trailing stop active: {'YES' if active else 'No (not yet at +' + f'{activation*100:.1f}%)'}\n"
                )

        hours_context = ""
        if hours_held is not None:
            hours_context = f"- Time held: {hours_held:.1f} hours\n"

        prompt = (
            f"You are a position risk analyst for a trading bot. Evaluate this open position "
            f"using the TRAFFIC LIGHT framework.\n\n"
            f"**Bot Risk Parameters (hardcoded):**\n"
            f"- Stop-loss: -3.5% (auto-closes position)\n"
            f"- Take-profit: +8% (auto-closes position)\n"
            f"- Trailing stop: activates at +2%, trails 1.5% from peak\n"
            f"- The bot checks positions every 15 minutes.\n\n"
            f"**Position Details:**\n"
            f"- Symbol: {symbol}\n"
            f"- Entry price: ${entry_price:,.2f}\n"
            f"- Current price: ${current_price:,.2f}\n"
            f"- Unrealized PnL: {pnl_pct:+.2f}%\n"
            f"{hours_context}"
            f"{trailing_context}\n"
            f"**Technical Indicators:**\n"
            f"- RSI: {technical_data.get('rsi', 'N/A')}\n"
            f"- SMA: {technical_data.get('sma', 'N/A')}\n"
            f"- Market regime: {technical_data.get('regime', 'unknown')}\n\n"
            f"**Recent Headlines:**\n{headlines_text}\n\n"
            "**TRAFFIC LIGHT FRAMEWORK:**\n"
            "- GREEN: position healthy — trend supports hold, no adverse catalysts, momentum intact.\n"
            "- YELLOW: one risk factor present — fading momentum, mixed headlines, RSI extreme, "
            "or approaching SL but no clear exit catalyst.\n"
            "- RED: multiple risk factors converge, OR a severe catalyst (exploit, regulatory ban, "
            "exchange insolvency). Recommends exit.\n\n"
            "**DECISION RULES (follow these):**\n"
            "- If PnL is near take-profit (>+6%) → lean GREEN/HOLD, let the TP mechanism close it.\n"
            "- If no concrete adverse catalyst → lean HOLD. Don't exit on vibes alone.\n"
            "- A high-confidence EXIT requires a concrete catalyst (specific bad news, not just 'uncertain market').\n"
            "- RSI overbought alone is YELLOW, not RED (the trailing stop handles orderly pullbacks).\n"
            "- If trailing stop is already active, it will protect gains — bias toward HOLD.\n\n"
            "Respond ONLY with valid JSON:\n"
            '{"recommendation": "hold|exit", "confidence": 0.0, "reasoning": "...", '
            '"risk_level": "green|yellow|red", '
            '"primary_risk": "none|momentum_fading|adverse_news|rsi_extreme|approaching_sl|multiple_factors"}\n\n'
            "Rules:\n"
            "- recommendation must be 'hold' or 'exit'\n"
            "- confidence must be a float between 0.0 and 1.0\n"
            "- reasoning should be one concise sentence\n"
            "- risk_level must be 'green', 'yellow', or 'red'\n"
            "- primary_risk describes the main concern (use 'none' if green)\n"
            "- Do not include any text outside the JSON object"
        )

        response = model.generate_content(prompt)
        text = response.text.strip()
        result = _parse_gemini_json(text)
        _validate_gemini_response(result,
                                  ['recommendation', 'confidence', 'risk_level', 'primary_risk'],
                                  'analyze_position_health')
        log.info(f"Position health for {symbol}: {result.get('recommendation')} "
                 f"(confidence={result.get('confidence')})")
        return result

    except json.JSONDecodeError as e:
        log.error(f"Failed to parse Gemini position health response as JSON: {e}")
        return None
    except Exception as e:
        log.error(f"Gemini position health analysis failed: {e}")
        return None
