"""Source discovery engine — autonomously finds and evaluates new news sources.

Three discovery methods:
1. RSS feed probing: probe common RSS paths for a given domain
2. Link extraction: track external domains cited in existing articles
3. Gemini-assisted: ask Gemini for candidate sources (low-cost, run weekly)
"""

import feedparser
import requests
from urllib.parse import urlparse

from src.config import app_config
from src.collectors.source_registry import (
    add_source, get_source_by_name,
)
from src.logger import log


RSS_PROBE_PATHS = [
    '/feed', '/rss', '/atom.xml', '/feed.xml', '/rss.xml',
    '/feed/', '/rss/', '/feeds/posts/default',
    '/blog/feed', '/news/feed', '/blog/rss',
]

PROBE_TIMEOUT = 10


def discover_rss_feeds(domains):
    """Probe a list of domains for RSS feeds.

    Args:
        domains: list of domain strings (e.g. ['theblock.co', 'defiant.io'])

    Returns:
        list of dicts: [{'url': str, 'domain': str, 'title': str, 'article_count': int}]
    """
    discovered = []
    for domain in domains:
        feeds = _probe_domain_for_rss(domain)
        discovered.extend(feeds)
    log.info(f"RSS discovery: probed {len(domains)} domains, "
             f"found {len(discovered)} feeds")
    return discovered


def _probe_domain_for_rss(domain):
    """Probe common RSS paths on a domain and return valid feeds."""
    results = []
    base = f"https://{domain}"

    for path in RSS_PROBE_PATHS:
        url = f"{base}{path}"
        try:
            resp = requests.get(url, timeout=PROBE_TIMEOUT,
                                headers={'User-Agent': 'CryptoBot/1.0'},
                                allow_redirects=True)
            if resp.status_code != 200:
                continue

            content_type = resp.headers.get('content-type', '')
            # Quick check: RSS/Atom feeds have xml or rss content type
            is_feed_type = any(t in content_type for t in
                               ['xml', 'rss', 'atom', 'application/json'])

            if not is_feed_type and '<rss' not in resp.text[:500] and '<feed' not in resp.text[:500]:
                continue

            parsed = feedparser.parse(resp.text)
            if not parsed.entries:
                continue

            title = getattr(parsed.feed, 'title', domain)
            results.append({
                'url': url,
                'domain': domain,
                'title': title,
                'article_count': len(parsed.entries),
            })
            log.info(f"Discovered RSS feed: {url} ({len(parsed.entries)} entries)")
            break  # Found a valid feed for this domain, skip other paths
        except Exception:
            continue

    return results


def extract_cited_domains(articles, min_citations=5):
    """Extract frequently cited external domains from articles.

    Args:
        articles: list of article dicts with 'source_url' field.
        min_citations: minimum number of citations to qualify.

    Returns:
        list of (domain, count) tuples, sorted by count descending.
    """
    domain_counts = {}
    known_domains = set()

    # Collect known source domains to exclude
    for article in articles:
        src_url = article.get('source_url', '')
        if src_url:
            try:
                d = urlparse(src_url).hostname
                if d:
                    known_domains.add(d.replace('www.', ''))
            except Exception:
                pass

    # Count external domains from article links/descriptions
    for article in articles:
        desc = article.get('description', '')
        # Simple URL extraction from description text
        for word in desc.split():
            if word.startswith('http'):
                try:
                    d = urlparse(word).hostname
                    if d:
                        d = d.replace('www.', '')
                        if d not in known_domains:
                            domain_counts[d] = domain_counts.get(d, 0) + 1
                except Exception:
                    pass

    candidates = [(d, c) for d, c in domain_counts.items() if c >= min_citations]
    candidates.sort(key=lambda x: x[1], reverse=True)
    return candidates[:20]  # Cap at 20 candidates


def discover_via_gemini(category='crypto'):
    """Ask Gemini for candidate news sources with RSS feeds.

    Low-cost: ~$0.01 per call. Run weekly.

    Args:
        category: 'crypto', 'stocks', 'ai', etc.

    Returns:
        list of dicts: [{'name': str, 'url': str, 'feed_url': str}]
    """
    try:
        from vertexai.generative_models import GenerativeModel
    except ImportError:
        log.warning("vertexai not available for Gemini discovery")
        return []

    prompt = (
        f"List 10 reputable {category} news websites that publish RSS feeds. "
        f"For each, provide: the site name, main URL, and RSS feed URL. "
        f"Format each line as: NAME | URL | FEED_URL\n"
        f"Only include sites with active, working RSS feeds. "
        f"Focus on English-language financial and {category} news sources."
    )

    try:
        model = GenerativeModel('gemini-2.5-flash-lite')
        response = model.generate_content(prompt)
        text = response.text.strip()

        candidates = []
        for line in text.split('\n'):
            line = line.strip()
            if '|' not in line:
                continue
            parts = [p.strip() for p in line.split('|')]
            if len(parts) >= 3:
                candidates.append({
                    'name': parts[0],
                    'url': parts[1],
                    'feed_url': parts[2],
                })
        log.info(f"Gemini discovery ({category}): {len(candidates)} candidates")
        return candidates
    except Exception as e:
        log.error(f"Gemini discovery failed: {e}")
        return []


def run_discovery_cycle():
    """Run a full discovery cycle: all methods, evaluate, insert candidates.

    Returns a summary dict.
    """
    from src.collectors.source_evaluator import evaluate_candidate

    config = app_config.get('settings', {}).get('autonomous_bot', {})
    disc_config = config.get('source_discovery', {})

    if not disc_config.get('enabled', False):
        return {'skipped': True, 'reason': 'source_discovery disabled'}

    max_experimental = disc_config.get('max_experimental_sources', 10)

    # Check how many experimental sources we already have
    from src.collectors.source_registry import load_active_sources
    experimental = load_active_sources(source_type=None, tier_max=3)
    tier3_count = sum(1 for s in experimental if s.get('tier') == 3)
    slots_available = max(0, max_experimental - tier3_count)

    if slots_available == 0:
        log.info("Discovery: max experimental sources reached, skipping")
        return {'skipped': True, 'reason': 'max_experimental_sources reached'}

    summary = {'discovered': 0, 'evaluated': 0, 'added': 0, 'methods': {}}

    # Method 1: Gemini-assisted discovery
    for category in ['crypto', 'stocks', 'ai']:
        candidates = discover_via_gemini(category)
        summary['methods'][f'gemini_{category}'] = len(candidates)

        for candidate in candidates:
            if slots_available <= 0:
                break

            feed_url = candidate.get('feed_url', '')
            name = candidate.get('name', '')

            # Skip if already known
            if get_source_by_name(name):
                continue

            summary['evaluated'] += 1
            eval_result = evaluate_candidate(feed_url, name, category)

            if eval_result.get('passed', False):
                added = add_source(
                    source_type='rss',
                    source_name=name,
                    source_url=feed_url,
                    category=category,
                    tier=3,  # experimental
                    added_by='discovery',
                )
                if added:
                    summary['added'] += 1
                    summary['discovered'] += 1
                    slots_available -= 1
                    _log_discovery(name, feed_url, category, eval_result)

    log.info(f"Discovery cycle: {summary['discovered']} new sources, "
             f"{summary['evaluated']} evaluated")
    return summary


def _log_discovery(name, url, category, eval_result):
    """Log a discovery to the experiment log."""
    from src.analysis.feedback_loop import _log_experiment
    _log_experiment(
        'discovery',
        f"Discovered new source '{name}' ({category}) via {eval_result.get('method', 'unknown')}",
        new_value=url,
        reason=f"Score: {eval_result.get('score', 'N/A')}",
        impact_metric='source_count',
    )
