"""
Daily Sector Review — Gemini Pro conviction scores per sector group.

Runs once daily, aggregates 24h article data per sector, calls Gemini 2.5 Pro
for cross-symbol analysis, and produces conviction scores (-1.0 to +1.0).
These scores modulate signal engine thresholds: bullish sectors get easier
BUY triggers, bearish sectors get harder ones.
"""

import json
import os
import statistics
import warnings

from src.config import app_config
from src.logger import log

# Module-level conviction cache: {group: {score, rationale, momentum, ...}}
_conviction_cache: dict[str, dict] = {}


def run_sector_review() -> dict | None:
    """Run daily sector review via Gemini 2.5 Pro.

    Sync function — call via asyncio.to_thread from async code.
    Returns parsed result dict or None on failure.
    """
    project_id = os.environ.get('GCP_PROJECT_ID')
    location = os.environ.get('VERTEX_AI_LOCATION') or os.environ.get('GCP_LOCATION', 'europe-west4')

    if not project_id:
        log.warning("GCP_PROJECT_ID not set — skipping sector review.")
        return None

    try:
        from src.analysis.sector_limits import _CRYPTO_GROUPS
        sector_groups = _load_all_sector_groups()
        if not sector_groups:
            log.warning("No sector groups loaded — skipping sector review.")
            return None

        sector_data = _aggregate_sector_data(sector_groups)
        macro = _get_macro_context()
        prompt = _build_sector_review_prompt(sector_data, macro)

        import vertexai
        with warnings.catch_warnings():
            warnings.filterwarnings("ignore", message=".*deprecated.*June 24, 2026.*")
            from vertexai.generative_models import GenerativeModel

        vertexai.init(project=project_id, location=location)
        model = GenerativeModel('gemini-2.5-pro')
        response = model.generate_content(prompt)

        from src.analysis.gemini_news_analyzer import _parse_gemini_json
        result = _parse_gemini_json(response.text)

        # Update cache and persist
        _update_cache(result, sector_groups)
        _persist_convictions(result)

        log.info(f"Sector review complete: {len(result.get('sectors', {}))} sectors scored.")
        return result

    except Exception as e:
        log.error(f"Sector review failed: {e}", exc_info=True)
        return None


def _load_all_sector_groups() -> dict:
    """Load all sector groups from config, keyed by group name."""
    from src.analysis.sector_limits import _ensure_loaded, _sector_config, _CRYPTO_GROUPS
    _ensure_loaded()
    if not _sector_config:
        return {}
    groups = _sector_config.get('groups', {})
    result = {}
    for group_name, group_data in groups.items():
        asset_class = 'crypto' if group_name in _CRYPTO_GROUPS else 'stock'
        result[group_name] = {
            'symbols': group_data.get('symbols', []),
            'asset_class': asset_class,
        }
    return result


def _aggregate_sector_data(sector_groups: dict) -> dict:
    """Gather 24h article summaries per sector group."""
    from src.database import get_recent_articles

    cfg = app_config.get('settings', {}).get('sector_review', {})
    min_articles = cfg.get('min_articles_for_review', 3)

    sector_data = {}
    for group_name, group_info in sector_groups.items():
        all_articles = []
        for symbol in group_info['symbols']:
            sym_str = str(symbol).upper()
            try:
                articles = get_recent_articles.sync(sym_str, hours=24, limit=50)
                all_articles.extend(articles)
            except Exception as e:
                log.debug(f"Failed to get articles for {sym_str}: {e}")

        if len(all_articles) < min_articles:
            sector_data[group_name] = {
                'asset_class': group_info['asset_class'],
                'article_count': len(all_articles),
                'skipped': True,
            }
            continue

        gemini_scores = [a.get('gemini_score') for a in all_articles
                         if a.get('gemini_score') is not None]
        avg_score = statistics.mean(gemini_scores) if gemini_scores else 0.0
        spread = statistics.stdev(gemini_scores) if len(gemini_scores) > 1 else 0.0

        # Top 3 headlines by absolute score
        scored_articles = [(a, abs(a.get('gemini_score', 0))) for a in all_articles
                           if a.get('gemini_score') is not None]
        scored_articles.sort(key=lambda x: x[1], reverse=True)
        top_headlines = [a[0].get('title', '') for a in scored_articles[:3]]

        sources = set(a.get('source', '') for a in all_articles if a.get('source'))

        sector_data[group_name] = {
            'asset_class': group_info['asset_class'],
            'article_count': len(all_articles),
            'avg_gemini_score': round(avg_score, 3),
            'sentiment_spread': round(spread, 3),
            'top_headlines': top_headlines,
            'source_count': len(sources),
            'skipped': False,
        }

    return sector_data


def _get_macro_context() -> dict:
    """Get current macro regime for prompt context."""
    try:
        from src.analysis.macro_regime import get_macro_regime
        regime = get_macro_regime()
        return {
            'regime': regime.get('regime', 'UNKNOWN'),
            'vix': regime.get('indicators', {}).get('vix', {}).get('current'),
            'sp500_trend': regime.get('signals', {}).get('sp500_trend'),
        }
    except Exception as e:
        log.debug(f"Could not get macro context: {e}")
        return {'regime': 'UNKNOWN'}


def _build_sector_review_prompt(sector_data: dict, macro: dict) -> str:
    """Build the Gemini Pro prompt for sector review."""
    # Separate sectors with data from skipped ones
    active_sectors = {k: v for k, v in sector_data.items() if not v.get('skipped')}
    skipped_sectors = [k for k, v in sector_data.items() if v.get('skipped')]

    sectors_text = ""
    for group, data in active_sectors.items():
        sectors_text += f"\n## {group} ({data['asset_class']})\n"
        sectors_text += f"- Articles (24h): {data['article_count']}\n"
        sectors_text += f"- Avg sentiment score: {data.get('avg_gemini_score', 0):.3f}\n"
        sectors_text += f"- Sentiment spread (stdev): {data.get('sentiment_spread', 0):.3f}\n"
        sectors_text += f"- Sources: {data.get('source_count', 0)}\n"
        headlines = data.get('top_headlines', [])
        if headlines:
            sectors_text += "- Top headlines:\n"
            for h in headlines:
                sectors_text += f"  - {h}\n"

    macro_text = f"Regime: {macro.get('regime', 'UNKNOWN')}"
    if macro.get('vix') is not None:
        macro_text += f", VIX: {macro['vix']}"
    if macro.get('sp500_trend'):
        macro_text += f", S&P trend: {macro['sp500_trend']}"

    return f"""You are a senior portfolio strategist reviewing daily sector sentiment data.

# Macro Context
{macro_text}

# Sector Data (last 24 hours)
{sectors_text}

{f"Sectors skipped (insufficient data): {', '.join(skipped_sectors)}" if skipped_sectors else ""}

# Task
Analyze cross-symbol patterns within each sector and produce conviction scores.

# Scoring Guidelines
- Score range: -1.0 (strong bearish) to +1.0 (strong bullish)
- Factor weighting: news catalysts 40%, sentiment trend 30%, macro alignment 20%, cross-sector flows 10%
- Be conservative: most sectors should score between -0.3 and +0.3
- Only use extreme scores (|score| > 0.7) for clear, catalyst-driven moves
- momentum: "accelerating", "stable", or "decelerating"

# Response Format
Return ONLY valid JSON (no markdown fences):
{{
  "cross_sector_theme": "brief theme description",
  "sectors": {{
    "sector_name": {{
      "score": 0.0,
      "rationale": "brief rationale",
      "key_catalyst": "main driver",
      "momentum": "stable",
      "confidence": 0.0
    }}
  }}
}}

Include ALL sectors from the data above. For skipped sectors, assign score 0.0 with rationale "Insufficient data"."""


def _update_cache(result: dict, sector_groups: dict):
    """Update module-level conviction cache from parsed result."""
    import time
    sectors = result.get('sectors', {})
    theme = result.get('cross_sector_theme', '')

    for group_name in sector_groups:
        if group_name in sectors:
            entry = sectors[group_name]
            _conviction_cache[group_name] = {
                'score': float(entry.get('score', 0.0)),
                'rationale': entry.get('rationale', ''),
                'key_catalyst': entry.get('key_catalyst', ''),
                'momentum': entry.get('momentum', 'stable'),
                'confidence': float(entry.get('confidence', 0.0)),
                'cross_sector_theme': theme,
                'updated_at': time.time(),
            }
        else:
            _conviction_cache[group_name] = {
                'score': 0.0,
                'rationale': 'Not reviewed',
                'momentum': 'stable',
                'updated_at': time.time(),
            }


def _persist_convictions(result: dict):
    """Save conviction scores to DB."""
    from src.database import save_sector_convictions
    from src.analysis.sector_limits import _CRYPTO_GROUPS

    sectors = result.get('sectors', {})
    theme = result.get('cross_sector_theme', '')
    rows = []
    for group_name, entry in sectors.items():
        asset_class = 'crypto' if group_name in _CRYPTO_GROUPS else 'stock'
        rows.append({
            'sector_group': group_name,
            'asset_class': asset_class,
            'score': float(entry.get('score', 0.0)),
            'rationale': entry.get('rationale'),
            'key_catalyst': entry.get('key_catalyst'),
            'momentum': entry.get('momentum'),
            'review_confidence': float(entry.get('confidence', 0.0)),
            'cross_sector_theme': theme,
        })
    if rows:
        save_sector_convictions(rows)


def get_sector_conviction(sector_group: str) -> float:
    """Returns cached conviction score for a group, default 0.0 if missing."""
    entry = _conviction_cache.get(sector_group)
    if entry:
        return entry.get('score', 0.0)
    return 0.0


def get_all_sector_convictions() -> dict:
    """Returns full conviction cache dict (for Telegram digest)."""
    return dict(_conviction_cache)


def load_convictions_into_cache(rows: list[dict]):
    """Populate cache from DB rows (used at startup)."""
    import time
    for row in rows:
        group = row.get('sector_group')
        if group:
            _conviction_cache[group] = {
                'score': float(row.get('score', 0.0)),
                'rationale': row.get('rationale', ''),
                'key_catalyst': row.get('key_catalyst', ''),
                'momentum': row.get('momentum', 'stable'),
                'confidence': float(row.get('review_confidence', 0.0)),
                'cross_sector_theme': row.get('cross_sector_theme', ''),
                'updated_at': time.time(),
            }
    if rows:
        log.info(f"Loaded {len(rows)} sector convictions into cache from DB.")


def clear_sector_conviction_cache():
    """Clear cache — for tests."""
    _conviction_cache.clear()
