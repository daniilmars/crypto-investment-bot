"""Regex-based IPO event extraction from news article titles and descriptions.

Scans articles for IPO-related language (filings, pricing, listings) and
returns structured events for persistence in the ipo_events table.
"""

import re

from src.logger import log

# Categories to scan for IPO events
DETECTION_CATEGORIES = {'ipo', 'ai', 'press_release'}

# (regex_pattern, event_type) — first capture group is the company name
IPO_PATTERNS = [
    (re.compile(r'\b(\w[\w\s&.]+?)\s+(?:files?|filed)\s+(?:for\s+)?(?:an?\s+)?IPO', re.IGNORECASE), 's1_filed'),
    (re.compile(r'\b(\w[\w\s&.]+?)\s+(?:plans?|set|readies|preparing)\s+(?:for\s+)?(?:an?\s+)?IPO', re.IGNORECASE), 'ipo_announced'),
    (re.compile(r'\b(\w[\w\s&.]+?)\s+(?:IPO|goes public)\s+(?:priced|prices)\s+at', re.IGNORECASE), 'ipo_priced'),
    (re.compile(r'\b(\w[\w\s&.]+?)\s+(?:begins?|starts?)\s+trading', re.IGNORECASE), 'listed'),
    (re.compile(r'\bIPO\s+of\s+(\w[\w\s&.]+?)\s', re.IGNORECASE), 'ipo_announced'),
    (re.compile(r'\b(\w[\w\s&.]+?)\s+(?:debuts?|lists?)\s+on\s+(?:NYSE|Nasdaq|NASDAQ)', re.IGNORECASE), 'listed'),
]

# Words that should never be treated as a company name
_NOISE_NAMES = {'the', 'a', 'an', 'this', 'that', 'its', 'their', 'our', 'his', 'her'}


def _normalize_company_name(name: str) -> str:
    """Strip leading/trailing whitespace and articles."""
    name = name.strip()
    # Remove leading articles
    for prefix in ('The ', 'A ', 'An '):
        if name.startswith(prefix):
            name = name[len(prefix):]
    return name.strip()


def _is_valid_company_name(name: str) -> bool:
    """Check if extracted company name looks plausible."""
    if not name or len(name) < 2 or len(name) > 80:
        return False
    if name.lower() in _NOISE_NAMES:
        return False
    # Must contain at least one letter
    if not any(c.isalpha() for c in name):
        return False
    return True


def detect_ipo_events(articles: list) -> list:
    """Scans articles for IPO-related events.

    Args:
        articles: list of article dicts with 'title', 'description', 'category',
                  'source_url', 'title_hash' (optional) keys.

    Returns:
        list of dicts: [{company_name, event_type, event_detail, source_url,
                         source_article_hash}, ...]
    """
    events = []
    seen = set()  # (normalized_company_name, event_type) for dedup within batch

    for article in articles:
        category = article.get('category', '').lower().strip()
        if category not in DETECTION_CATEGORIES:
            continue

        title = article.get('title', '')
        description = article.get('description', '')
        text = f"{title} {description}"

        for pattern, event_type in IPO_PATTERNS:
            match = pattern.search(text)
            if not match:
                continue

            raw_name = match.group(1)
            company_name = _normalize_company_name(raw_name)
            if not _is_valid_company_name(company_name):
                continue

            dedup_key = (company_name.lower(), event_type)
            if dedup_key in seen:
                continue
            seen.add(dedup_key)

            event = {
                'company_name': company_name,
                'ticker': None,
                'status': event_type,
                'event_type': event_type,
                'event_detail': title[:500],
                'source_url': article.get('source_url', ''),
                'source_article_hash': article.get('title_hash'),
            }
            events.append(event)
            log.info(f"[IPO] Detected {event_type}: {company_name} — {title[:80]}")
            break  # one event per article

    log.info(f"[IPO] Detected {len(events)} IPO events from {len(articles)} articles.")
    return events
