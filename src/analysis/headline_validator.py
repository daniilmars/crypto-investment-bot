"""Validate that a Gemini-generated `key_headline` actually relates to the
reasoning and symbol it claims to support.

The grounded-search Gemini call sometimes echoes a high-volume headline
from the cross-symbol context window into a `key_headline` field that
has nothing to do with the per-symbol `reasoning` it returned. Example:

    symbol:        CCJ (Cameco — uranium)
    reasoning:     "SMR plant approval and uranium potential outweigh ..."
    key_headline:  "North Korea's Lazarus suspected of stealing ..."

Storing the mismatched headline poisons signal_attribution and the Mini
App's "why we bought this" panel. Callers run `is_headline_consistent`
and clear (set to "") any headline that fails the check.
"""
import re

# Words that show up in almost every Gemini reasoning string and don't
# count as meaningful overlap with a real article headline.
_STOPWORDS = frozenset({
    'represents', 'provides', 'includes', 'indicates', 'because', 'strong',
    'signal', 'market', 'recent', 'potential', 'catalyst', 'positive',
    'negative', 'adverse', 'growth', 'related', 'launch', 'launched',
    'expected', 'support', 'concrete', 'driving', 'movement', 'forecast',
    'neutral', 'bullish', 'bearish', 'company', 'companies', 'industry',
    'sector', 'announced', 'announcement', 'reports', 'reported', 'overall',
    'within', 'across', 'against', 'between', 'should', 'remains',
    'higher', 'lower', 'increase', 'decrease', 'continues', 'continued',
    'further', 'earlier', 'recently', 'likely', 'unlikely', 'around',
    'before', 'showing', 'suggest', 'suggests', 'reflects', 'although',
    'however', 'despite',
})

_TOKEN_RE = re.compile(r'\b[a-z]{6,}\b')


def _stems(text: str) -> set[str]:
    """5-character prefixes of all 6+-char non-stopword tokens.

    Using prefixes (a poor person's stemmer) lets reasoning's
    "acquisition" match a headline's "acquires" — both → "acqui". Real
    English news rarely repeats verbatim words between a summary and the
    underlying headline, so exact-token overlap is too strict.
    """
    if not text:
        return set()
    tokens = (w for w in _TOKEN_RE.findall(text.lower()) if w not in _STOPWORDS)
    return {t[:5] for t in tokens}


def is_headline_consistent(
    headline: str | None,
    reasoning: str | None,
    symbol: str | None,
    aliases: list[str] | None = None,
) -> bool:
    """Returns True if `headline` plausibly is the article behind `reasoning`.

    Consistent when EITHER:
      - the headline mentions the symbol or any company alias (word-boundary,
        case-insensitive)
      - the headline shares ≥ 1 meaningful (non-stopword, ≥6-char) token with
        the reasoning

    Edge cases:
      - Empty/None headline → True (nothing to invalidate)
      - Empty reasoning     → True (no basis to judge)
      - Symbol in alias form is matched on word boundaries, so "BP" doesn't
        hit "bplate" and "AI" doesn't hit "available".
    """
    if not headline:
        return True
    if not reasoning:
        return True

    h_lower = headline.lower()

    # 1. Symbol or alias mention?
    candidates: list[str] = []
    if symbol:
        s = symbol.strip()
        if s:
            candidates.append(s.lower())
            base = s.split('.')[0]
            if base and base.lower() != s.lower():
                candidates.append(base.lower())
    if aliases:
        candidates.extend(a.lower() for a in aliases if a)

    for cand in candidates:
        if not cand:
            continue
        if re.search(rf'\b{re.escape(cand)}\b', h_lower):
            return True

    # 2. Stem overlap (5-char prefixes catch acquires/acquisition)?
    if _stems(headline) & _stems(reasoning):
        return True

    return False


def scrub_unrelated_headlines(
    result: dict,
    context: str = '',
    aliases_by_symbol: dict[str, list[str]] | None = None,
) -> int:
    """In-place: blank `key_headline` fields that don't match `reasoning`.

    Walks `result['symbol_assessments']` and clears each unrelated headline.
    Returns the count of cleared headlines (caller may log).

    `aliases_by_symbol` is optional: when provided (e.g. derived from the
    business-descriptions config), each symbol's company aliases also count
    as a valid headline match.
    """
    assessments = result.get('symbol_assessments') if isinstance(result, dict) else None
    if not isinstance(assessments, dict):
        return 0
    cleared = 0
    for symbol, a in assessments.items():
        if not isinstance(a, dict):
            continue
        headline = a.get('key_headline')
        if not headline:
            continue
        aliases = (aliases_by_symbol or {}).get(symbol)
        if not is_headline_consistent(headline, a.get('reasoning', ''), symbol, aliases):
            a['key_headline'] = ''
            cleared += 1
    return cleared
