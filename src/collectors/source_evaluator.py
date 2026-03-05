"""Source evaluator — evaluates candidate news sources before adding to registry.

Evaluation pipeline:
1. Availability: fetch feed, verify parseable, check article frequency
2. Relevance: score articles for relevance to watched symbols
3. Uniqueness: compare titles against existing articles (dedup ratio)
4. Quality: article length, English language, financial content presence
"""

import feedparser
import requests
from datetime import datetime, timezone

from src.config import app_config
from src.database import compute_title_hash
from src.logger import log


FETCH_TIMEOUT = 15


def evaluate_candidate(feed_url, source_name, category=None):
    """Evaluate a candidate feed source.

    Returns:
        dict with evaluation results:
        {
            'passed': bool,
            'score': float (0-1),
            'availability': float,
            'relevance': float,
            'uniqueness': float,
            'quality': float,
            'article_count': int,
            'reason': str (if failed),
            'method': str,
        }
    """
    result = {
        'passed': False,
        'score': 0.0,
        'availability': 0.0,
        'relevance': 0.0,
        'uniqueness': 0.5,
        'quality': 0.0,
        'article_count': 0,
        'method': 'evaluation',
    }

    # Step 1: Availability check
    articles = _check_availability(feed_url)
    if articles is None:
        result['reason'] = 'feed_unavailable'
        return result

    result['article_count'] = len(articles)
    if len(articles) < 3:
        result['reason'] = f'too_few_articles ({len(articles)})'
        result['availability'] = 0.2
        return result

    result['availability'] = 1.0

    # Step 2: Quality check
    quality_score = _check_quality(articles)
    result['quality'] = quality_score

    if quality_score < 0.3:
        result['reason'] = f'low_quality ({quality_score:.2f})'
        return result

    # Step 3: Relevance check
    relevance_score = _check_relevance(articles, category)
    result['relevance'] = relevance_score

    # Step 4: Uniqueness check
    uniqueness_score = _check_uniqueness(articles)
    result['uniqueness'] = uniqueness_score

    if uniqueness_score < 0.2:
        result['reason'] = f'too_much_overlap ({uniqueness_score:.2f})'
        return result

    # Calculate overall score
    score = (0.3 * result['availability'] +
             0.2 * result['relevance'] +
             0.2 * result['uniqueness'] +
             0.3 * result['quality'])
    result['score'] = round(score, 3)

    # Pass threshold
    min_score = app_config.get('settings', {}).get('autonomous_bot', {}).get(
        'source_discovery', {}).get('promotion_threshold', 0.6)

    # Lower bar for initial addition (tier 3 trial)
    trial_threshold = min_score * 0.7  # ~0.42
    result['passed'] = score >= trial_threshold

    if not result['passed']:
        result['reason'] = f'below_threshold ({score:.2f} < {trial_threshold:.2f})'

    return result


def _check_availability(feed_url):
    """Fetch and parse a feed. Returns list of articles or None."""
    try:
        resp = requests.get(feed_url, timeout=FETCH_TIMEOUT,
                            headers={'User-Agent': 'CryptoBot/1.0'})
        if resp.status_code != 200:
            return None

        parsed = feedparser.parse(resp.text)
        if not parsed.entries:
            return None

        articles = []
        for entry in parsed.entries:
            articles.append({
                'title': getattr(entry, 'title', ''),
                'description': getattr(entry, 'summary', ''),
                'link': getattr(entry, 'link', ''),
                'published': getattr(entry, 'published', ''),
            })
        return articles
    except Exception as e:
        log.debug(f"Availability check failed for {feed_url}: {e}")
        return None


def _check_quality(articles):
    """Score article quality: language, length, content presence.

    Returns float 0-1.
    """
    if not articles:
        return 0.0

    good = 0
    for article in articles[:20]:  # Check up to 20 articles
        title = article.get('title', '')
        desc = article.get('description', '')

        # Must have a title
        if not title or len(title) < 10:
            continue

        # English language check (simple heuristic)
        text = title + ' ' + desc
        non_ascii = sum(1 for c in text if ord(c) > 127)
        if len(text) > 0 and non_ascii / len(text) > 0.15:
            continue

        # Minimum content length
        if len(desc) < 20 and len(title) < 20:
            continue

        good += 1

    return good / min(len(articles), 20)


def _check_relevance(articles, category=None):
    """Score how relevant articles are to the bot's watched symbols.

    Returns float 0-1.
    """
    from src.collectors.news_data import _KEYWORD_PATTERNS

    if not articles:
        return 0.0

    settings = app_config.get('settings', {})
    symbols = settings.get('watch_list', [])
    stock_symbols = settings.get('stock_trading', {}).get('watch_list', [])
    all_symbols = symbols + stock_symbols

    if not all_symbols:
        return 0.5  # Can't measure relevance without symbols

    matched = 0
    checked = min(len(articles), 20)

    for article in articles[:checked]:
        title = article.get('title', '')
        desc = article.get('description', '')
        text = f"{title} {desc}"

        for sym in all_symbols:
            patterns = _KEYWORD_PATTERNS.get(sym, [])
            if any(p.search(text) for p in patterns):
                matched += 1
                break

    return matched / checked if checked > 0 else 0.0


def _check_uniqueness(articles):
    """Check how unique the articles are compared to existing scraped_articles.

    Returns float 0-1 (1 = all unique, 0 = all duplicates).
    """
    from src.database import get_db_connection, release_db_connection, _cursor
    import psycopg2

    if not articles:
        return 0.5

    # Compute title hashes for candidate articles
    candidate_hashes = set()
    for article in articles[:20]:
        title = article.get('title', '').strip()
        if title:
            candidate_hashes.add(compute_title_hash(title))

    if not candidate_hashes:
        return 0.5

    # Check how many exist in our DB
    conn = None
    try:
        conn = get_db_connection()
        is_pg = isinstance(conn, psycopg2.extensions.connection)
        ph = "%s" if is_pg else "?"

        placeholders = ", ".join([ph] * len(candidate_hashes))
        with _cursor(conn) as cur:
            cur.execute(
                f"SELECT COUNT(*) FROM scraped_articles "
                f"WHERE title_hash IN ({placeholders})",
                list(candidate_hashes))
            row = cur.fetchone()
            existing = list(row.values())[0] if is_pg else row[0]

        uniqueness = 1.0 - (existing / len(candidate_hashes))
        return max(0.0, uniqueness)
    except Exception as e:
        log.debug(f"Uniqueness check failed: {e}")
        return 0.5
    finally:
        if conn:
            release_db_connection(conn)
