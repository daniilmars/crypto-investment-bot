import json
import os
import random
import re
import time
import warnings

import vertexai
with warnings.catch_warnings():
    warnings.filterwarnings("ignore", message=".*deprecated.*June 24, 2026.*")
    from vertexai.generative_models import GenerativeModel

from src.config import app_config
from src.logger import log

# TODO(2026-06): Migrate from vertexai SDK to google.genai SDK before June 2026 deadline.
# analyze_news_with_search() already uses google.genai; score_articles_batch() and
# analyze_news_impact() still use the legacy vertexai SDK.

# --- JSON parsing helpers ---

_FENCE_RE = re.compile(r'^```(?:json)?\s*\n?(.*?)```\s*$', re.DOTALL)


def _extract_first_json_object(text: str) -> str:
    """Extract the first complete top-level JSON object from text.

    Uses brace-depth counting (skipping braces inside strings) to handle
    cases where Gemini appends commentary or duplicate objects after the JSON.
    Falls back to returning the full text if no balanced object is found.
    """
    start = text.find('{')
    if start == -1:
        return text
    depth = 0
    in_string = False
    escape = False
    for i in range(start, len(text)):
        ch = text[i]
        if escape:
            escape = False
            continue
        if ch == '\\' and in_string:
            escape = True
            continue
        if ch == '"':
            in_string = not in_string
            continue
        if in_string:
            continue
        if ch == '{':
            depth += 1
        elif ch == '}':
            depth -= 1
            if depth == 0:
                return text[start:i + 1]
    return text


def _parse_gemini_json(text: str) -> dict:
    """Strip markdown code-fence variants and parse JSON.

    Handles:
      - Plain JSON (no fences)
      - ```json ... ```
      - ``` ... ```
      - Extra data after the JSON object (Gemini commentary, duplicate objects)
    Raises json.JSONDecodeError on failure.
    """
    text = text.strip()
    m = _FENCE_RE.match(text)
    if m:
        text = m.group(1).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        # Gemini sometimes appends extra text after the JSON — extract first object
        extracted = _extract_first_json_object(text)
        return json.loads(extracted)


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


_MAX_RETRIES = 3
_RETRY_BASE_DELAY = 2.0


def _is_retryable_error(exc: Exception) -> bool:
    """Check if a Gemini API error is transient and worth retrying."""
    type_name = type(exc).__name__
    if type_name in ('ResourceExhausted', 'InternalServerError',
                     'ServiceUnavailable', 'DeadlineExceeded', 'TooManyRequests'):
        return True
    msg = str(exc).lower()
    return any(s in msg for s in ('429', '500', '503', 'resource exhausted',
                                  'quota', 'rate limit', 'unavailable'))


def _call_with_retry(fn, *args, **kwargs):
    """Call a Gemini API function with exponential backoff on transient errors.

    Retries up to _MAX_RETRIES times on 429/500/503 errors.
    Raises the original exception if all retries exhausted or error is not retryable.
    """
    last_exc = None
    for attempt in range(_MAX_RETRIES + 1):
        try:
            return fn(*args, **kwargs)
        except Exception as e:
            last_exc = e
            if attempt < _MAX_RETRIES and _is_retryable_error(e):
                delay = _RETRY_BASE_DELAY * (2 ** attempt) + random.uniform(0, 1)
                log.warning(f"Gemini API transient error (attempt {attempt + 1}/{_MAX_RETRIES + 1}), "
                            f"retrying in {delay:.1f}s: {e}")
                time.sleep(delay)
            else:
                raise
    raise last_exc


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
        "You are a senior prop trading desk analyst. Your job is NOT sentiment analysis — "
        "it is to evaluate each article the way a professional trader would: "
        "identify actionable catalysts, assess what is already priced in, "
        "and determine if there is a real trade here.\n\n"
        "For each article, output a score from -1.0 to +1.0 representing TRADE SIGNAL "
        "STRENGTH, not sentiment.\n\n"
        "HOW A PRO TRADER READS NEWS:\n"
        "1. WHAT'S THE CATALYST? — Is this a concrete event (regulatory ruling, hack, "
        "earnings surprise, Fed decision, major deal) or just commentary/opinion? "
        "No catalyst = 0.0, regardless of how 'bullish' the tone is.\n"
        "2. IS IT PRICED IN? — If every outlet has been covering this for days, "
        "the market already moved. Score near 0.0. Only SURPRISE or FRESH information "
        "deserves a high score.\n"
        "3. WHAT'S THE TRADE? — Would you actually put money on this? "
        "A +0.7 score means 'I would enter a position right now based on this.' "
        "Reserve high scores for news where you'd bet your own capital.\n"
        "4. SECOND-ORDER EFFECTS — An AI chip export ban isn't just about NVDA. "
        "Think about who benefits, who gets hurt, supply chain effects.\n"
        "5. CONTRARIAN CHECK — When everyone is bearish on something, that's often "
        "the bottom. Extreme consensus = lower magnitude, not higher.\n\n"
        "SCORE CALIBRATION:\n"
        "- |score| 0.7-1.0: I WOULD TRADE THIS NOW — concrete catalyst, surprise factor, "
        "clear direction. Examples: unexpected ETF approval, exchange hack, "
        "earnings beat/miss >15%, surprise Fed move, major protocol exploit.\n"
        "- |score| 0.4-0.6: WORTH WATCHING — real catalyst but partially priced in, "
        "or catalyst is concrete but expected move is modest. Examples: earnings in-line "
        "with whisper numbers, anticipated partnership announcement, scheduled upgrade.\n"
        "- |score| 0.1-0.3: NOISE WITH A KERNEL — minor news, low surprise value, "
        "or real event but too small to move price >2%.\n"
        "- 0.0: NO TRADE — opinion pieces, price predictions, recycled narratives, "
        "'experts say', vague rumors, corporate PR, articles ABOUT price movement "
        "rather than CAUSING it, duplicate stories. This should be the MOST COMMON score.\n\n"
        "ANTI-PATTERNS (always score 0.0):\n"
        "- 'X could reach $Y' / 'analyst predicts' / 'why I'm bullish on'\n"
        "- Articles that describe price action without explaining what caused it\n"
        "- Rehashed narratives with a new date ('BTC halving will...')\n"
        "- Press releases without market-moving substance\n"
        "- Same story from multiple sources — only the first occurrence matters\n\n"
        f"Articles:\n{articles_text}\n\n"
        "Respond ONLY with a JSON array of numbers in the same order:\n"
        f"[0.7, -0.4, 0.0, ...]"
    )

    try:
        response = _call_with_retry(model.generate_content, prompt)
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
    """Scores a list of articles using Gemini 2.5 Flash in batches.

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

    location = os.environ.get('VERTEX_AI_LOCATION') or os.environ.get('GCP_LOCATION', 'europe-west4')

    try:
        vertexai.init(project=project_id, location=location)
        model = GenerativeModel('gemini-2.5-flash')
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
                             cache_ttl_minutes: int = 30,
                             headlines_by_symbol: dict = None,
                             archived_articles_by_symbol: dict = None,
                             news_stats_by_symbol: dict = None,
                             scored_articles_by_symbol: dict = None) -> dict | None:
    """
    Uses Gemini with Google Search grounding + our RSS/scraper headlines
    to produce a comprehensive news assessment per symbol.

    Combines two data sources:
    - Our curated RSS/scraper headlines (60+ feeds, trusted sources)
    - Gemini's own Google Search results (real-time web search)

    Args:
        symbols: list of ticker symbols (e.g. ['BTC', 'ETH', 'AAPL'])
        current_prices: {symbol: float} — current prices for context
        cache_ttl_minutes: cache TTL for Gemini responses
        headlines_by_symbol: {symbol: [headline, ...]} from RSS/scraping
        archived_articles_by_symbol: {symbol: [{title, source, ...}, ...]} from DB
        news_stats_by_symbol: {symbol: {news_volume, positive_ratio, ...}}

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

    try:
        from google import genai
        from google.genai.types import GenerateContentConfig, Tool, GoogleSearch

        # Prefer consumer API key (free grounding tier, 500 RPD)
        # Falls back to Vertex AI if no key set
        gemini_api_key = os.environ.get('GEMINI_API_KEY')
        if gemini_api_key:
            client = genai.Client(api_key=gemini_api_key, vertexai=False)
        else:
            project_id = os.environ.get('GCP_PROJECT_ID')
            location = os.environ.get('VERTEX_AI_LOCATION') or os.environ.get('GCP_LOCATION', 'europe-west4')
            if not project_id:
                log.warning("Neither GEMINI_API_KEY nor GCP_PROJECT_ID set — "
                            "skipping grounded news analysis.")
                return None
            client = genai.Client(vertexai=True, project=project_id, location=location)

        # Build per-symbol context sections
        symbol_sections = []
        for sym in symbols:
            price = current_prices.get(sym)
            price_str = f"${price:,.2f}" if price else "unknown"
            section = f"**{sym}** (current price: {price_str})"

            # Add our collected headlines
            if headlines_by_symbol and sym in headlines_by_symbol:
                top = headlines_by_symbol[sym][:10]
                if top:
                    section += "\nOur RSS/scraper headlines:\n" + "\n".join(
                        f"- {h}" for h in top)

            # Add archived articles with source attribution
            if archived_articles_by_symbol and sym in archived_articles_by_symbol:
                archived = archived_articles_by_symbol[sym][:5]
                if archived:
                    archive_lines = []
                    for a in archived:
                        source = a.get('source', '?')
                        category = a.get('category', '')
                        cat_tag = f" ({category})" if category else ""
                        line = f"- [{source}{cat_tag}] {a.get('title', '')}"
                        desc = a.get('description', '')
                        if desc and len(desc) > 50:
                            line += f"\n  {desc[:500]}"
                        archive_lines.append(line)
                    section += "\nRecent archived articles:\n" + "\n".join(archive_lines)

            # Add pre-computed news stats
            if news_stats_by_symbol and sym in news_stats_by_symbol:
                stats = news_stats_by_symbol[sym]
                section += (
                    f"\nNews stats: {stats.get('news_volume', 0)} articles, "
                    f"positive/negative: {stats.get('positive_ratio', 0):.0%}/"
                    f"{stats.get('negative_ratio', 0):.0%}")

            # Add per-article Gemini scores (high-impact articles pre-scored)
            if scored_articles_by_symbol and sym in scored_articles_by_symbol:
                scored = scored_articles_by_symbol[sym]
                if scored:
                    score_lines = [
                        f"- [{a['score']:+.2f}] {a['title']}"
                        for a in scored[:8]
                    ]
                    section += (
                        "\nPre-scored articles (individual Gemini scores, "
                        "sorted by impact — use these to weigh catalysts):\n"
                        + "\n".join(score_lines)
                    )

            symbol_sections.append(section)

        collected_context = "\n\n".join(symbol_sections)
        symbols_str = ", ".join(symbols)

        prompt = (
            "You are a senior trading desk analyst. Use BOTH the headlines we collected "
            "from our RSS feeds AND your own Google Search results to assess news impact.\n\n"
            "BOT PARAMETERS:\n"
            "- Stop-loss: -10%, Take-profit: +50%, Trailing stop: +5%/2%\n"
            "- 15-minute decision cycles. Only confidence >= 0.5 triggers trades.\n\n"
            "INSTRUCTIONS:\n"
            "1. Review our collected headlines below for each symbol\n"
            "2. Search the web for any additional breaking news we may have missed\n"
            "3. Cross-reference both sources — corroborated stories get higher confidence\n"
            "4. Only CONCRETE catalysts (regulatory, ETF, hack, earnings surprise, "
            "Fed decision) justify confidence >= 0.6\n"
            "5. Opinion pieces, recycled narratives, price predictions → confidence <= 0.4\n\n"
            f"--- Symbols to analyze: {symbols_str} ---\n\n"
            f"{collected_context}\n\n"
            "Now search the web for any additional news about these assets, then respond "
            "ONLY with valid JSON:\n"
            "{\n"
            '  "symbol_assessments": {\n'
            '    "SYMBOL": {\n'
            '      "direction": "bullish|bearish|neutral",\n'
            '      "confidence": 0.0,\n'
            '      "reasoning": "one sentence: [catalyst] + [expected impact]",\n'
            '      "catalyst_type": "regulatory|etf|hack_exploit|macro|partnership|'
            'protocol_upgrade|fund_flow|earnings|narrative|none",\n'
            '      "catalyst_freshness": "breaking|recent|stale|none",\n'
            '      "catalyst_count": 1,\n'
            '      "hype_vs_fundamental": "hype|fundamental|mixed",\n'
            '      "risk_factors": [],\n'
            '      "sentiment_divergence": false,\n'
            '      "key_headline": "the single most impactful headline"\n'
            '    }\n'
            "  },\n"
            '  "market_mood": "brief overall sentiment phrase",\n'
            '  "cross_asset_theme": "common driver across symbols, or null"\n'
            "}\n\n"
            "FIELD DEFINITIONS:\n"
            "- catalyst_count: number of DISTINCT independent catalysts (not rehashes "
            "of the same story). Max 5.\n"
            "- hype_vs_fundamental: 'hype' = social momentum/narrative/influencer-driven, "
            "'fundamental' = regulatory/earnings/adoption/technology, "
            "'mixed' = elements of both.\n"
            "- risk_factors: up to 3 specific risks, e.g. ['priced in', 'low volume', "
            "'single source', 'contradicted by on-chain data']. Empty array if none.\n\n"
            "CONFIDENCE CALIBRATION:\n"
            "- 0.0-0.3: no catalyst, noise, stale news\n"
            "- 0.3-0.5: weak catalyst or unconfirmed reports\n"
            "- 0.5-0.7: concrete catalyst from reliable source\n"
            "- 0.7-0.9: major catalyst with corroboration\n"
            "- 0.9-1.0: extraordinary event (black swan)\n"
            "- Include ALL requested symbols. When in doubt, score LOWER."
        )

        # Single attempt per batch — no retries to stay within 500 RPD free tier.
        # Empty response = skip this batch (will be reassessed next uncached cycle).
        response = _call_with_retry(
            client.models.generate_content,
            model="gemini-2.5-flash-lite",
            contents=prompt,
            config=GenerateContentConfig(
                tools=[Tool(google_search=GoogleSearch())],
                temperature=0.2,
            ),
        )
        text = response.text.strip() if response.text else ''
        if not text:
            log.error(f"Gemini grounded search returned empty after all "
                      f"retries for {len(symbols)} symbols: {symbols[:5]}...")
            return None
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
                        news_stats_by_symbol: dict = None,
                        scored_articles_by_symbol: dict = None) -> dict | None:
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
        scored_articles_by_symbol: {symbol: [{'title': str, 'score': float}, ...]}
            Optional per-article Gemini scores (|score| > 0.3, sorted by abs score).
            Injected into the prompt so symbol-level assessment can weigh individual catalysts.

    Returns:
        dict with 'symbol_assessments' and 'market_mood', or None on failure.
    """
    project_id = os.environ.get('GCP_PROJECT_ID')
    location = os.environ.get('VERTEX_AI_LOCATION') or os.environ.get('GCP_LOCATION', 'europe-west4')

    if not project_id:
        log.warning("GCP_PROJECT_ID not set — skipping Gemini news analysis.")
        return None

    if not headlines_by_symbol:
        return None

    try:
        vertexai.init(project=project_id, location=location)
        model = GenerativeModel('gemini-2.5-flash-lite')

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

            # Add per-article Gemini scores (high-impact articles pre-scored)
            if scored_articles_by_symbol:
                scored = scored_articles_by_symbol.get(symbol, [])
                if scored:
                    score_lines = [
                        f"- [{a['score']:+.2f}] {a['title']}"
                        for a in scored[:8]
                    ]
                    section += (
                        "\n\nPre-scored articles (individual Gemini scores, "
                        "sorted by impact — use these to weigh catalysts):\n"
                        + "\n".join(score_lines)
                    )

            symbol_sections.append(section)

        headlines_text = "\n\n".join(symbol_sections)

        prompt = (
            "You are a senior trading desk analyst scoring news for an automated trading bot.\n\n"
            "BOT PARAMETERS (critical context — your assessment drives real trades):\n"
            "- Stop-loss: -3.5%, Take-profit: +8%, Trailing stop: +2% activation / 1.5% trail\n"
            "- 15-minute decision cycles. Only signals with confidence ≥ 0.5 trigger trades.\n"
            "- The bot WILL enter positions based on your assessment. False positives cost money.\n\n"
            "ANALYSIS FRAMEWORK — apply ALL steps before scoring:\n\n"
            "STEP 1 — CATALYST SCAN (most important):\n"
            "Ask: 'Is there a SPECIFIC, CONCRETE event that will move the price >3.5% in 24h?'\n"
            "- YES catalysts (can justify confidence ≥ 0.6):\n"
            "  • Regulatory action (SEC ruling, country ban/approval, enforcement action)\n"
            "  • ETF approval/rejection/filing updates\n"
            "  • Exchange hack, exploit, or insolvency\n"
            "  • Major earnings surprise (beat/miss > 10%)\n"
            "  • Central bank decision deviating from consensus (rate surprise)\n"
            "  • Protocol upgrade, hard fork, or major vulnerability\n"
            "  • Large verifiable fund flows (whale alerts, treasury moves)\n"
            "  • Major partnership/acquisition with named counterparty\n"
            "- NO catalysts (confidence MUST stay ≤ 0.4):\n"
            "  • Price predictions ('BTC could reach $X')\n"
            "  • Recycled narratives repackaged with new dates\n"
            "  • Opinion/editorial pieces ('why I'm bullish on...')\n"
            "  • Vague 'sources say', 'insiders hint', 'rumored'\n"
            "  • Press releases without market impact\n"
            "  • Articles that are ABOUT price movement rather than CAUSING it\n"
            "  • General market commentary ('crypto market shows resilience')\n\n"
            "STEP 2 — SOURCE CREDIBILITY:\n"
            "- Tier 1 (trust fully): SEC/CFTC filings, Fed statements, exchange official posts, "
            "Reuters, Bloomberg, AP wire\n"
            "- Tier 2 (weight normally): CoinDesk, CoinTelegraph, Decrypt, WSJ, FT, CNBC\n"
            "- Tier 3 (discount heavily): blogs, crypto Twitter, unknown outlets, "
            "articles without body text (headline-only = low trust)\n"
            "- If ONLY Tier 3 sources report something, cap confidence at 0.4\n\n"
            "STEP 3 — CONSENSUS vs DIVERGENCE:\n"
            "- If pre-scored articles are provided, use their individual scores to gauge consensus.\n"
            "  A single +0.9 article with several -0.3 articles = divergence, not bullish.\n"
            "  Multiple articles above +0.5 = strong consensus.\n"
            "- Headlines all agree → confidence boost\n"
            "- Headlines conflict → cap confidence at 0.5 max, set sentiment_divergence=true\n"
            "- Single source with no corroboration → treat with skepticism\n\n"
            "STEP 4 — FRESHNESS & PRICED-IN CHECK:\n"
            "- Breaking (< 2h old, not yet reflected in price) → full weight\n"
            "- Recent (2-12h) → partial weight, market may have moved already\n"
            "- Stale (>24h) → near-zero weight, already priced in\n"
            "- Scheduled events (CPI, FOMC, earnings dates) that haven't happened yet "
            "→ only the RESULT matters, not pre-event speculation\n\n"
            "STEP 5 — PRICE IMPACT ESTIMATION:\n"
            "Ask: 'Given the bot's 3.5% SL and 8% TP, will this news move the price enough?'\n"
            "- If expected move < 2% → neutral (not worth the trade risk)\n"
            "- If expected move 2-5% → moderate confidence (direction must be clear)\n"
            "- If expected move > 5% → high confidence (if catalyst is concrete)\n"
            "- For correlated assets: if BTC news drives the move, altcoins follow with "
            "higher beta — flag this in cross_asset_theme\n\n"
            f"--- Headlines by Symbol ---\n{headlines_text}\n\n"
            "Respond ONLY with valid JSON:\n"
            "{\n"
            '  "symbol_assessments": {\n'
            '    "SYMBOL": {\n'
            '      "direction": "bullish|bearish|neutral",\n'
            '      "confidence": 0.0,\n'
            '      "reasoning": "one sentence: [catalyst] + [expected impact]",\n'
            '      "catalyst_type": "regulatory|etf|hack_exploit|macro|partnership|'
            'protocol_upgrade|fund_flow|earnings|narrative|none",\n'
            '      "catalyst_freshness": "breaking|recent|stale|none",\n'
            '      "catalyst_count": 1,\n'
            '      "hype_vs_fundamental": "hype|fundamental|mixed",\n'
            '      "risk_factors": [],\n'
            '      "sentiment_divergence": false,\n'
            '      "key_headline": "the single most impactful headline"\n'
            '    }\n'
            "  },\n"
            '  "market_mood": "brief overall sentiment phrase",\n'
            '  "cross_asset_theme": "common driver across symbols, or null"\n'
            "}\n\n"
            "FIELD DEFINITIONS:\n"
            "- catalyst_count: number of DISTINCT independent catalysts (not rehashes "
            "of the same story). Max 5.\n"
            "- hype_vs_fundamental: 'hype' = social momentum/narrative/influencer-driven, "
            "'fundamental' = regulatory/earnings/adoption/technology, "
            "'mixed' = elements of both.\n"
            "- risk_factors: up to 3 specific risks, e.g. ['priced in', 'low volume', "
            "'single source', 'contradicted by on-chain data']. Empty array if none.\n\n"
            "CONFIDENCE CALIBRATION (follow strictly):\n"
            "- 0.0-0.3: no catalyst, noise only, or stale/priced-in news\n"
            "- 0.3-0.5: weak catalyst or unconfirmed reports from lower-tier sources\n"
            "- 0.5-0.7: concrete catalyst from reliable source, clear direction\n"
            "- 0.7-0.9: major catalyst (regulatory, hack, earnings surprise) with corroboration\n"
            "- 0.9-1.0: extraordinary event (exchange insolvency, country ban, black swan)\n"
            "- When in doubt, score LOWER. The bot has stop-losses for protection — "
            "but false entries waste capital and trigger cooldowns."
        )

        response = _call_with_retry(model.generate_content, prompt)
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


def analyze_position_investment(
    position: dict, current_price: float,
    recent_articles: list, technical_data: dict,
    news_velocity: dict,
    hours_held: float = None,
    trailing_stop_info: dict = None,
    position_additions: list = None,
    max_position_multiplier: float = 3.0,
    strategy_type: str = None,
    trade_reason: str = None,
) -> dict | None:
    """Tri-state investment analyst: HOLD / INCREASE / SELL.

    Args:
        position: dict with symbol, entry_price, quantity, order_id, entry_timestamp
        current_price: current market price
        recent_articles: full article dicts from get_recent_articles()
        technical_data: dict with rsi, sma, regime
        news_velocity: dict from compute_news_velocity()
        hours_held: how long position has been open (hours)
        trailing_stop_info: dict with trailing stop state
        position_additions: list of prior additions for this position
        max_position_multiplier: maximum allowed position size as multiple of original

    Returns:
        dict with recommendation, confidence, reasoning, risk_level, etc. or None on failure.
    """
    project_id = os.environ.get('GCP_PROJECT_ID')
    location = os.environ.get('VERTEX_AI_LOCATION') or os.environ.get('GCP_LOCATION', 'europe-west4')

    if not project_id:
        log.warning("GCP_PROJECT_ID not set — skipping position investment analysis.")
        return None

    symbol = position.get('symbol', 'UNKNOWN')
    entry_price = position.get('entry_price', 0)
    quantity = position.get('quantity', 0)
    pnl_pct = ((current_price - entry_price) / entry_price * 100) if entry_price > 0 else 0

    # Calculate current position multiplier from additions
    additions = position_additions or []
    original_value = entry_price * quantity
    # If there are additions, estimate original value by subtracting additions
    total_added_value = sum(a.get('addition_price', 0) * a.get('addition_quantity', 0) for a in additions)
    if total_added_value > 0 and original_value > total_added_value:
        estimated_original_value = original_value - total_added_value
    else:
        estimated_original_value = original_value
    current_multiplier = original_value / estimated_original_value if estimated_original_value > 0 else 1.0
    can_increase = current_multiplier < max_position_multiplier

    try:
        vertexai.init(project=project_id, location=location)
        model = GenerativeModel('gemini-2.5-pro')

        # Format articles for prompt
        if recent_articles:
            article_lines = []
            for i, art in enumerate(recent_articles[:20], 1):
                source = art.get('source', '?')
                score = art.get('gemini_score') or art.get('vader_score')
                score_str = f" [sentiment: {score:+.2f}]" if score is not None else ""
                collected = art.get('collected_at', '')
                desc = (art.get('description', '') or '')[:200]
                line = f"{i}. [{source}]{score_str} {art.get('title', '')}"
                if desc:
                    line += f"\n   {desc}"
                if collected:
                    line += f"\n   ({collected})"
                article_lines.append(line)
            articles_text = "\n".join(article_lines)
        else:
            articles_text = "No recent articles available."

        # Trailing stop context
        trailing_context = ""
        if trailing_stop_info:
            peak = trailing_stop_info.get('peak_price')
            active = trailing_stop_info.get('trailing_active', False)
            activation = trailing_stop_info.get('activation_threshold', 0.02)
            if peak:
                trailing_context = (
                    f"\n**Trailing Stop State:**\n"
                    f"- Peak price seen: ${peak:,.2f}\n"
                    f"- Trailing stop active: {'YES' if active else 'No (not yet at +' + f'{activation * 100:.1f}%)'}\n"
                )

        hours_context = f"- Time held: {hours_held:.1f} hours\n" if hours_held is not None else ""

        # Position additions history
        additions_context = ""
        if additions:
            additions_context = f"\n**Position Addition History ({len(additions)} additions):**\n"
            for a in additions:
                additions_context += f"- Added {a.get('addition_quantity', 0):.6f} at ${a.get('addition_price', 0):,.2f} ({a.get('reason', 'N/A')})\n"
            additions_context += f"- Current position multiplier: {current_multiplier:.1f}x (max: {max_position_multiplier}x)\n"
        else:
            additions_context = f"\n- Position multiplier: 1.0x (max: {max_position_multiplier}x, can increase: {'yes' if can_increase else 'no'})\n"

        # News velocity context
        velocity_context = (
            f"\n**News Velocity:**\n"
            f"- Articles last 1h: {news_velocity.get('articles_last_1h', 0)}, "
            f"4h: {news_velocity.get('articles_last_4h', 0)}, "
            f"24h: {news_velocity.get('articles_last_24h', 0)}\n"
            f"- Avg sentiment 1h: {news_velocity.get('avg_sentiment_1h', 0):+.3f}, "
            f"24h: {news_velocity.get('avg_sentiment_24h', 0):+.3f}\n"
            f"- Sentiment trend: {news_velocity.get('sentiment_trend', 'stable')}\n"
            f"- Velocity: {news_velocity.get('velocity_status', 'normal')}\n"
            f"- Breaking news detected: {'YES' if news_velocity.get('breaking_detected') else 'No'}\n"
        )

        # Strategic investment context
        strategic_context = ""
        if strategy_type and trade_reason:
            categories = app_config.get('settings', {}).get('strategic_categories', {})
            cat_info = categories.get(strategy_type, {})
            cat_label = cat_info.get('label', strategy_type)
            cat_sl = cat_info.get('catastrophic_sl', 0.20)
            strategic_context = (
                f"\n**STRATEGIC INVESTMENT — {cat_label.upper()}:**\n"
                f"- Category: {strategy_type}\n"
                f"- Original thesis: {trade_reason}\n"
                f"- Catastrophic stop-loss: -{cat_sl*100:.0f}% (only hard exit)\n"
                f"- No take-profit or trailing stop — this is a long-term hold\n"
                f"- IMPORTANT: Evaluate whether the original investment thesis "
                f"is still intact. Only recommend SELL if the thesis is broken "
                f"(e.g., fundamental change, regulatory risk, thesis invalidated). "
                f"Short-term price dips within the catastrophic SL range are expected.\n"
            )
        elif trade_reason:
            strategic_context = f"\n- Trade reason: {trade_reason}\n"

        if strategy_type:
            risk_params = (
                f"**Risk Parameters (Strategic — {strategy_type}):**\n"
                f"- Catastrophic stop-loss only (no TP, no trailing stop)\n"
                f"- Exits are thesis-driven, not price-driven\n"
            )
            sell_step = (
                "STEP 3 — SELL EVALUATION (STRATEGIC INVESTMENT):\n"
                "- The PRIMARY question: is the original investment thesis still valid?\n"
                "- Price decline alone is NOT a sell signal for strategic positions\n"
                "- SELL only if: thesis is broken, sector fundamentals deteriorated, "
                "or a concrete adverse catalyst invalidates the investment reason\n"
                "- Temporary market volatility and short-term sentiment dips = HOLD\n"
                "- A single negative headline is NOT enough — look for thesis-breaking corroboration\n\n"
            )
        else:
            risk_params = (
                "**Bot Risk Parameters:**\n"
                "- Catastrophic stop-loss: ~10% (emergency safety net only)\n"
                "- AI exit checks: every 4 hours (evaluates whether to hold or exit)\n"
                "- Trailing stop: activates at +5%, trails 2% from peak\n"
                "- No fixed take-profit — you decide when gains are sufficient\n"
            )
            sell_step = (
                "STEP 3 — SELL EVALUATION:\n"
                "- Requires concrete adverse catalyst with HIGH confidence\n"
                "- OR multiple converging risks: adverse news + technical weakness + breaking negative velocity\n"
                "- A single negative headline is NOT enough — look for corroboration\n"
                "- If trailing stop is active, it will protect gains — bias toward HOLD\n\n"
            )

        prompt = (
            f"You are a senior investment analyst for a trading bot. Evaluate this open position "
            f"and recommend one of: HOLD, INCREASE (add capital), or SELL.\n\n"
            f"{risk_params}"
            f"- The bot checks positions every 15 minutes.\n\n"
            f"**Position Details:**\n"
            f"- Symbol: {symbol}\n"
            f"- Entry price: ${entry_price:,.2f}\n"
            f"- Current price: ${current_price:,.2f}\n"
            f"- Unrealized PnL: {pnl_pct:+.2f}%\n"
            f"- Quantity: {quantity}\n"
            f"{hours_context}"
            f"{trailing_context}"
            f"{additions_context}"
            f"{strategic_context}\n"
            f"**Technical Indicators:**\n"
            f"- RSI: {technical_data.get('rsi', 'N/A')}\n"
            f"- SMA: {technical_data.get('sma', 'N/A')}\n"
            f"- Market regime: {technical_data.get('regime', 'unknown')}\n"
            f"{velocity_context}\n"
            f"**Recent Articles ({len(recent_articles) if recent_articles else 0}):**\n{articles_text}\n\n"
            "**4-STEP ANALYSIS FRAMEWORK:**\n\n"
            "STEP 1 — NEWS MOMENTUM ASSESSMENT:\n"
            "- Is news accelerating? Are articles increasing in frequency?\n"
            "- Is sentiment consistent (all positive/negative) or mixed?\n"
            "- Are there concrete catalysts (regulatory action, partnership, hack, upgrade) or just noise?\n\n"
            "STEP 2 — INCREASE EVALUATION:\n"
            "- ALL of these must be true to recommend INCREASE:\n"
            "  1. Positive sentiment trend with concrete bullish catalyst identified in articles\n"
            "  2. Technical support: RSI < 70 AND price > SMA (if available)\n"
            f"  3. Position not at max size (current: {current_multiplier:.1f}x, max: {max_position_multiplier}x)\n"
            "  4. NOT near take-profit (PnL < +6%)\n"
            "- NEVER recommend INCREASE when trailing stop is active — let it protect gains\n"
            "- NEVER recommend INCREASE based on momentum alone — require a specific catalyst\n\n"
            f"{sell_step}"
            "STEP 4 — DEFAULT HOLD:\n"
            "- No news = HOLD. Mixed signals = HOLD.\n"
            "- Trailing stop active = HOLD (it handles orderly exits).\n"
            "- Uncertain or low-confidence scenarios = HOLD.\n\n"
            "Respond ONLY with valid JSON:\n"
            '{"recommendation": "hold|increase|sell", "confidence": 0.0, '
            '"reasoning": "one concise sentence", '
            '"risk_level": "green|yellow|red", '
            '"primary_driver": "bullish_catalyst|momentum|adverse_news|technical_weakness|sentiment_trend|breaking_news|none", '
            '"news_momentum": "accelerating|stable|decelerating", '
            '"increase_sizing_hint": "small|medium|large", '
            '"key_article": "headline of most important article"}\n\n'
            "Rules:\n"
            "- recommendation must be 'hold', 'increase', or 'sell'\n"
            "- confidence must be a float between 0.0 and 1.0\n"
            "- increase_sizing_hint: only set when recommendation is 'increase' (small=25%, medium=50%, large=75% of original value). Set null otherwise.\n"
            "- key_article: the single most impactful article headline, or null if none\n"
            "- Do not include any text outside the JSON object"
        )

        response = _call_with_retry(model.generate_content, prompt)
        text = response.text.strip()
        result = _parse_gemini_json(text)
        _validate_gemini_response(
            result,
            ['recommendation', 'confidence', 'risk_level', 'primary_driver'],
            'analyze_position_investment',
        )
        log.info(f"Position analyst for {symbol}: {result.get('recommendation')} "
                 f"(confidence={result.get('confidence')})")
        return result

    except json.JSONDecodeError as e:
        log.error(f"Failed to parse Gemini position investment response as JSON: {e}")
        return None
    except Exception as e:
        log.error(f"Gemini position investment analysis failed: {e}")
        return None


def analyze_position_quick(
    position: dict,
    current_price: float,
    recent_articles: list,
    technical_data: dict,
    news_velocity: dict,
    hours_held: float = None,
    trailing_stop_info: dict = None,
    strategy_type: str = None,
    trade_reason: str = None,
) -> dict | None:
    """Fast exit-only check using Flash model — "Is there a reason to EXIT now?"

    Runs every 4 hours between deeper Pro analyst reviews. Binary hold/exit only.

    Returns:
        dict with action ("hold"|"exit"), confidence (0.0-1.0), reason — or None on failure.
    """
    project_id = os.environ.get('GCP_PROJECT_ID')
    location = os.environ.get('VERTEX_AI_LOCATION') or os.environ.get('GCP_LOCATION', 'europe-west4')

    if not project_id:
        log.warning("GCP_PROJECT_ID not set — skipping Flash position check.")
        return None

    symbol = position.get('symbol', 'UNKNOWN')
    entry_price = position.get('entry_price', 0)
    pnl_pct = ((current_price - entry_price) / entry_price * 100) if entry_price > 0 else 0

    try:
        vertexai.init(project=project_id, location=location)
        model = GenerativeModel('gemini-2.5-flash')

        # Format recent articles (last 10, 4h window)
        if recent_articles:
            article_lines = []
            for i, art in enumerate(recent_articles[:10], 1):
                source = art.get('source', '?')
                score = art.get('gemini_score') or art.get('vader_score')
                score_str = f" [{score:+.2f}]" if score is not None else ""
                article_lines.append(f"{i}. [{source}]{score_str} {art.get('title', '')}")
            articles_text = "\n".join(article_lines)
        else:
            articles_text = "No recent articles."

        # Trailing stop context
        trailing_context = ""
        if trailing_stop_info:
            peak = trailing_stop_info.get('peak_price')
            active = trailing_stop_info.get('trailing_active', False)
            if peak:
                trailing_context = (
                    f"- Trailing stop: {'ACTIVE' if active else 'inactive'}, "
                    f"peak ${peak:,.2f}\n"
                )

        hours_context = f"- Held: {hours_held:.1f}h\n" if hours_held is not None else ""

        # Strategic position modifier
        if strategy_type and trade_reason:
            strategy_instruction = (
                f"\nThis is a STRATEGIC {strategy_type.upper()} position.\n"
                f"Original thesis: {trade_reason}\n"
                f"Only recommend EXIT if the thesis is broken by a concrete event. "
                f"Price dips alone are NOT exit signals for strategic positions.\n"
            )
        else:
            strategy_instruction = ""

        # Velocity summary
        velocity_summary = (
            f"- News: {news_velocity.get('articles_last_4h', 0)} articles (4h), "
            f"sentiment trend: {news_velocity.get('sentiment_trend', 'stable')}, "
            f"velocity: {news_velocity.get('velocity_status', 'normal')}"
        )
        if news_velocity.get('breaking_detected'):
            velocity_summary += " [BREAKING NEWS DETECTED]"

        prompt = (
            f"Quick exit check for {symbol}. Is there a concrete reason to EXIT now?\n\n"
            f"- Entry: ${entry_price:,.2f}, Current: ${current_price:,.2f}, PnL: {pnl_pct:+.2f}%\n"
            f"{hours_context}"
            f"{trailing_context}"
            f"- RSI: {technical_data.get('rsi', 'N/A')}\n"
            f"{velocity_summary}\n"
            f"{strategy_instruction}\n"
            f"**Articles (last 4h):**\n{articles_text}\n\n"
            "EXIT only if: concrete adverse catalyst (hack, regulatory ban, thesis-breaking news), "
            "OR multiple converging risks. No news or mixed signals = HOLD.\n\n"
            'Respond ONLY with JSON: {"action": "hold"|"exit", "confidence": 0.0, "reason": "..."}\n'
            "- action: 'hold' or 'exit'\n"
            "- confidence: float 0.0-1.0\n"
            "- reason: one concise sentence"
        )

        response = _call_with_retry(model.generate_content, prompt)
        text = response.text.strip()
        result = _parse_gemini_json(text)
        _validate_gemini_response(result, ['action', 'confidence', 'reason'], 'analyze_position_quick')

        action = result.get('action', 'hold').strip().lower()
        if action not in ('hold', 'exit'):
            log.warning(f"[{symbol}] Flash analyst invalid action: {action!r}, defaulting to hold")
            result['action'] = 'hold'

        log.info(f"Flash analyst for {symbol}: {result.get('action')} "
                 f"(confidence={result.get('confidence')})")
        return result

    except json.JSONDecodeError as e:
        log.error(f"Failed to parse Flash analyst response as JSON: {e}")
        return None
    except Exception as e:
        log.error(f"Flash analyst failed for {symbol}: {e}")
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
    location = os.environ.get('VERTEX_AI_LOCATION') or os.environ.get('GCP_LOCATION', 'europe-west4')

    if not project_id:
        log.warning("GCP_PROJECT_ID not set — skipping position health analysis.")
        return None

    symbol = position.get('symbol', 'UNKNOWN')
    entry_price = position.get('entry_price', 0)
    pnl_pct = ((current_price - entry_price) / entry_price * 100) if entry_price > 0 else 0

    try:
        vertexai.init(project=project_id, location=location)
        model = GenerativeModel('gemini-2.5-flash-lite')

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

        response = _call_with_retry(model.generate_content, prompt)
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
