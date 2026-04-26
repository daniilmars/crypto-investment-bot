"""Sector-aware confidence caps for Gemini per-symbol assessments.

Solves the "1 headline → 14 oil stocks all bullish at 0.7" problem:

  Without ranking, the grounded-search Gemini call returns a per-symbol
  bullish call for every ticker that's loosely related to a sector-level
  story. Real example: 1 energy headline drove 1,588 high-confidence
  bullish assessments across XOM/BP/SLB/VLO/HAL/EOG/... in 14 days.
  HII + LHX both bought on a single Iran/Navy headline → both stopped −10%.

The fix has two layers:

  1. Prompt asks Gemini to rank symbols within shared catalysts and tag
     counter-impacted names (refiners on oil spikes etc.).
  2. This module enforces caps on the returned ranks AND has a safety
     net that catches the case where Gemini ignores the ranking rule
     and just gives every name in a basket the same confidence.

Behavior is gated by `settings.sector_ranking.enabled`. Default is False
(shadow mode): the function still runs and tags assessments, but does
not lower confidence. Flip the flag once the shadow logs confirm the
caps don't suppress real signal.
"""
from collections import defaultdict
from typing import Any

from src.logger import log


# --- Defaults (overridable via settings.sector_ranking.*) ---

DEFAULT_CAP_RANK_2 = 0.55
DEFAULT_CAP_RANK_3PLUS = 0.40
DEFAULT_SAFETY_NET_MIN_SYMBOLS = 5
DEFAULT_SAFETY_NET_CAP = 0.45


def _cfg(settings: dict | None, key: str, default: Any) -> Any:
    if not settings:
        return default
    return (settings.get('sector_ranking') or {}).get(key, default)


def apply_rank_caps(result: dict, settings: dict | None = None) -> dict:
    """In-place: enforce confidence caps based on Gemini's impact_rank,
    plus a safety net for shared headlines.

    Mutations applied to each assessment dict:
      - confidence: capped per rank rules (only when enabled=True)
      - risk_factors: tagged with 'secondary_beneficiary',
        'counter_impacted', or 'shared_thematic_signal'
      - direction: flipped to 'neutral' for counter_impacted entries

    Always-on stats (regardless of enabled flag) are returned via a new
    `result['_sector_ranking_stats']` key so shadow-mode runs can be
    audited from logs.

    Args:
      result: full Gemini result dict (with 'symbol_assessments').
      settings: app settings; reads `sector_ranking.{enabled, cap_rank_2,
        cap_rank_3plus, safety_net_min_symbols, safety_net_cap}`.
    """
    if not isinstance(result, dict):
        return result
    assessments = result.get('symbol_assessments')
    if not isinstance(assessments, dict):
        return result

    enabled = bool(_cfg(settings, 'enabled', False))
    cap2 = float(_cfg(settings, 'cap_rank_2', DEFAULT_CAP_RANK_2))
    cap3 = float(_cfg(settings, 'cap_rank_3plus', DEFAULT_CAP_RANK_3PLUS))
    safety_n = int(_cfg(settings, 'safety_net_min_symbols', DEFAULT_SAFETY_NET_MIN_SYMBOLS))
    safety_cap = float(_cfg(settings, 'safety_net_cap', DEFAULT_SAFETY_NET_CAP))

    stats = {
        'enabled': enabled,
        'rank_2_caps': 0,
        'rank_3plus_caps': 0,
        'counter_impacted': 0,
        'shared_headline_caps': 0,
        'symbols_seen': 0,
    }

    # Pass 1 — per-symbol rank caps + counter-impacted handling
    for symbol, a in assessments.items():
        if not isinstance(a, dict):
            continue
        stats['symbols_seen'] += 1
        a.setdefault('risk_factors', [])
        if not isinstance(a['risk_factors'], list):
            a['risk_factors'] = [str(a['risk_factors'])]

        impact_basis = (a.get('impact_basis') or '').strip().lower()
        rank = a.get('impact_rank')
        try:
            rank = int(rank) if rank is not None else None
        except (TypeError, ValueError):
            rank = None

        if impact_basis == 'counter-impacted' or impact_basis == 'counter_impacted':
            stats['counter_impacted'] += 1
            if 'counter_impacted' not in a['risk_factors']:
                a['risk_factors'].append('counter_impacted')
            if enabled:
                # Don't BUY a counter-impacted name on sector vibes.
                a['direction'] = 'neutral'
                a['confidence'] = 0.0
            continue

        if rank == 2:
            stats['rank_2_caps'] += 1
            if 'secondary_beneficiary' not in a['risk_factors']:
                a['risk_factors'].append('secondary_beneficiary')
            if enabled:
                cur = float(a.get('confidence') or 0.0)
                if cur > cap2:
                    a['confidence'] = cap2
        elif rank is not None and rank >= 3:
            stats['rank_3plus_caps'] += 1
            if 'secondary_beneficiary' not in a['risk_factors']:
                a['risk_factors'].append('secondary_beneficiary')
            if enabled:
                cur = float(a.get('confidence') or 0.0)
                if cur > cap3:
                    a['confidence'] = cap3
        # rank 1 or None → no cap

    # Pass 2 — safety net: ≥N symbols share a key_headline AND no ranks
    # given → cap them all (catches Gemini ignoring the ranking rule).
    grouped: dict[str, list[str]] = defaultdict(list)
    for symbol, a in assessments.items():
        if not isinstance(a, dict):
            continue
        h = (a.get('key_headline') or '').strip()
        if not h:
            continue
        # Only group when no useful rank info was given for this symbol
        if a.get('impact_rank') is not None:
            continue
        # Only consider currently-bullish high-confidence calls
        if (a.get('direction') or '').lower() != 'bullish':
            continue
        if float(a.get('confidence') or 0.0) < 0.5:
            continue
        grouped[h.lower()].append(symbol)

    for headline_key, symbols in grouped.items():
        if len(symbols) < safety_n:
            continue
        for sym in symbols:
            a = assessments.get(sym)
            if not isinstance(a, dict):
                continue
            stats['shared_headline_caps'] += 1
            if 'shared_thematic_signal' not in (a.get('risk_factors') or []):
                a['risk_factors'].append('shared_thematic_signal')
            if enabled:
                cur = float(a.get('confidence') or 0.0)
                if cur > safety_cap:
                    a['confidence'] = safety_cap

    result['_sector_ranking_stats'] = stats
    if (stats['rank_2_caps'] + stats['rank_3plus_caps']
            + stats['counter_impacted'] + stats['shared_headline_caps']) > 0:
        log.info(
            f"sector_ranking[{'on' if enabled else 'shadow'}] "
            f"symbols={stats['symbols_seen']} "
            f"rank2={stats['rank_2_caps']} rank3+={stats['rank_3plus_caps']} "
            f"counter={stats['counter_impacted']} "
            f"shared_headline={stats['shared_headline_caps']}")
    return result
