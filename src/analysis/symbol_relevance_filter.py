"""Semantic relevance filter for the (article, symbol) routing layer.

The keyword router in `news_data._match_article_to_symbols` is broad on purpose —
it generates candidates cheaply via regex matches. That cheapness leaks two
ways:

  1. **Word collisions.** "Coca Cola Zone" is a mining-trench name in a
     gold-discovery article; the regex matches "Coca-Cola" and routes the
     article to ticker KO with conf 0.90.

  2. **Sector over-routing.** A single OPEC headline routes to 8+ oil
     names — XOM, BP.L, COP, VLO, MPC, NESTE.HE, etc. — even though most
     of them aren't materially mentioned in the article body. Each then
     consumes a Gemini scoring call.

This module gates each (article, candidates) pair through Gemini flash-lite,
asking which candidates the article is *materially* about. Output is
"material" / "tangential" / "unrelated"; "unrelated" gets dropped.

Cached by (title_hash, candidates_hash) so the same article with the same
candidate set isn't re-judged on every cycle. Falls open on any error —
returns the original candidate list unchanged when Gemini is unavailable.

Disabled by default via config (`news_analysis.symbol_relevance_filter
.enabled`). Even when enabled, a `shadow_log_only` flag lets us log what
would be dropped without changing behavior — that's the validation phase.
"""
import hashlib
import json
import time
from typing import Iterable

from src.logger import log

# In-process cache: maps (title_hash, candidates_hash) -> set[str] of relevant
# symbols. Cleared per-process; the DB-backed cache survives restarts.
_RELEVANCE_CACHE: dict[tuple[str, str], set[str]] = {}
_CACHE_TTL_SECONDS = 7 * 24 * 3600  # 7 days
_CACHE_TS: dict[tuple[str, str], float] = {}


def clear_relevance_cache():
    """Test hook to clear the in-process cache."""
    _RELEVANCE_CACHE.clear()
    _CACHE_TS.clear()


def _candidates_hash(candidates: Iterable[str]) -> str:
    s = ",".join(sorted(c for c in candidates if c))
    return hashlib.sha256(s.encode()).hexdigest()[:16]


def _cache_get(key: tuple[str, str]) -> set[str] | None:
    ts = _CACHE_TS.get(key)
    if ts is None:
        return None
    if time.time() - ts > _CACHE_TTL_SECONDS:
        # expired
        _RELEVANCE_CACHE.pop(key, None)
        _CACHE_TS.pop(key, None)
        return None
    return _RELEVANCE_CACHE.get(key)


def _cache_set(key: tuple[str, str], relevant: set[str]):
    _RELEVANCE_CACHE[key] = relevant
    _CACHE_TS[key] = time.time()


def _build_prompt(article: dict, candidates: list[str],
                  business_descriptions: dict[str, str] | None) -> str:
    title = article.get('title', '') or ''
    desc = article.get('description', '') or ''
    body = f"TITLE: {title}\n"
    if desc and len(desc) > 30:
        body += f"BODY: {desc[:1500]}\n"

    desc_lines = []
    for sym in candidates:
        d = (business_descriptions or {}).get(sym)
        if d:
            desc_lines.append(f"- {sym}: {d}")
        else:
            desc_lines.append(f"- {sym}")
    desc_block = "\n".join(desc_lines)

    return (
        f"{body}\n"
        f"CANDIDATE SYMBOLS (judge each):\n{desc_block}\n\n"
        "For each candidate, output one of:\n"
        "  material    — the company is the direct subject of the article (named "
        "in headline, M&A target, named contract winner, regulator's actual subject, "
        "specific earnings/product/event for that company)\n"
        "  tangential  — same sector or theme, but the article is NOT specifically "
        "about this company (e.g. \"oil prices rise\" article + an oil ticker that "
        "isn't named in the article)\n"
        "  unrelated   — the symbol matched on a text collision (e.g. ticker letters "
        "appearing in another context) but the article isn't about the company at all\n\n"
        "Output ONLY a single JSON object mapping each ticker to its label, e.g.:\n"
        '{"XOM": "material", "BP.L": "tangential", "VLO": "unrelated"}\n'
    )


def _parse_response(text: str, candidates: list[str]) -> dict[str, str]:
    """Best-effort parse. Falls back to {} on any error so caller can fail-open."""
    if not text:
        return {}
    try:
        # Strip code fences
        t = text.strip()
        if t.startswith("```"):
            t = t.strip("`").strip()
            if t.startswith("json"):
                t = t[4:].strip()
        # Extract first {...} block
        start = t.find("{")
        end = t.rfind("}")
        if start == -1 or end == -1:
            return {}
        obj = json.loads(t[start:end + 1])
        if not isinstance(obj, dict):
            return {}
        # Keep only known candidates; normalise labels
        out = {}
        for sym in candidates:
            v = obj.get(sym)
            if isinstance(v, str):
                v_low = v.strip().lower()
                if v_low in ("material", "tangential", "unrelated"):
                    out[sym] = v_low
        return out
    except (json.JSONDecodeError, ValueError, TypeError):
        return {}


def filter_by_relevance(
    article: dict,
    candidate_symbols: list[str],
    business_descriptions: dict[str, str] | None = None,
    *,
    model: str = "gemini-2.5-flash-lite",
    drop_tangential: bool = False,
) -> tuple[list[str], dict[str, str]]:
    """Returns (kept, verdicts).

    kept: subset of candidate_symbols that the article is materially about.
      By default, "material" + "tangential" are kept; only "unrelated" is dropped.
      Pass drop_tangential=True for stricter filtering.
    verdicts: full {symbol: label} map (for logging / caching).

    Skips the Gemini call entirely when len(candidate_symbols) <= 1.
    Falls open on any error: returns (candidate_symbols, {}).
    """
    if not candidate_symbols:
        return [], {}
    if len(candidate_symbols) <= 1:
        return list(candidate_symbols), {}

    title_hash = article.get('title_hash') or ''
    if not title_hash:
        # Without a title hash we can't cache; still run but log
        log.debug("symbol_relevance_filter: article has no title_hash; "
                  "running uncached")

    cache_key = (title_hash, _candidates_hash(candidate_symbols))
    cached = _cache_get(cache_key)
    if cached is not None:
        kept = [s for s in candidate_symbols if s in cached]
        return kept, {s: ("material" if s in cached else "unrelated")
                      for s in candidate_symbols}

    try:
        from src.analysis.gemini_news_analyzer import _make_genai_client
        client = _make_genai_client()
        if client is None:
            return list(candidate_symbols), {}

        prompt = _build_prompt(article, candidate_symbols, business_descriptions)
        response = client.models.generate_content(
            model=model,
            contents=prompt,
        )
        text = (response.text or "").strip() if response else ""
        verdicts = _parse_response(text, candidate_symbols)
        if not verdicts:
            # Parse failed → fail-open
            return list(candidate_symbols), {}

        if drop_tangential:
            kept_set = {s for s, v in verdicts.items() if v == "material"}
        else:
            kept_set = {s for s, v in verdicts.items() if v in ("material", "tangential")}

        # Symbols missing from verdicts (Gemini didn't judge them) → keep, fail-open
        for sym in candidate_symbols:
            if sym not in verdicts:
                kept_set.add(sym)

        _cache_set(cache_key, kept_set)
        return [s for s in candidate_symbols if s in kept_set], verdicts

    except Exception as e:
        log.debug(f"symbol_relevance_filter: Gemini call failed ({e}); fail-open")
        return list(candidate_symbols), {}
