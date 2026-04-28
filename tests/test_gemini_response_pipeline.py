"""Integration test: parsed Gemini response → headline scrub → rank caps.

Exercises the post-parse pipeline that lives at the bottom of both
`analyze_news_with_search` and `analyze_news_impact`. Catches regressions
where the wiring order changes or one step is accidentally removed.
"""

from src.analysis.headline_validator import scrub_unrelated_headlines
from src.analysis.sector_ranking import apply_rank_caps


def test_full_pipeline_clears_headline_then_caps_rank():
    # Two symbols, one BUY at conf 0.7. The CCJ-like one has a mismatched
    # headline (different topic from reasoning) → should clear.
    # The HII-like one has impact_rank=2 → should cap to 0.55 when enabled.
    raw = {
        'symbol_assessments': {
            'CCJ': {
                'direction': 'bullish',
                'confidence': 0.70,
                'reasoning': "SMR plant approval and uranium upside outweigh concerns.",
                'key_headline': "North Korea Lazarus crypto hack steals $290M",
                'impact_rank': 1,
            },
            'HII': {
                'direction': 'bullish',
                'confidence': 0.70,
                'reasoning': "U.S. Navy action stopping Iranian blockades and "
                             "government-related defense activity supports "
                             "Huntington Ingalls' contract pipeline.",
                'key_headline': "U.S. Navy stops 13 ships from passing Iranian port blockade",
                'impact_rank': 2,
            },
        }
    }
    cleared = scrub_unrelated_headlines(raw)
    apply_rank_caps(raw, settings={'sector_ranking': {'enabled': True}})

    # CCJ's bogus headline cleared; reasoning preserved
    assert raw['symbol_assessments']['CCJ']['key_headline'] == ''
    assert "uranium" in raw['symbol_assessments']['CCJ']['reasoning']
    # Rank=1 → confidence not capped
    assert raw['symbol_assessments']['CCJ']['confidence'] == 0.70

    # HII's headline relates to reasoning ("defense" + "Navy" via stem) → kept
    assert raw['symbol_assessments']['HII']['key_headline'] != ''
    # Rank=2 → capped to 0.55
    assert raw['symbol_assessments']['HII']['confidence'] == 0.55
    assert 'secondary_beneficiary' in raw['symbol_assessments']['HII']['risk_factors']

    assert cleared == 1


def test_rank_cap_then_threshold_blocks_sector_vibe():
    """PR-D combined behavior: rank-2 signal at conf 0.70 should be capped
    to 0.55 by sector_ranking and then fall below conservative's new 0.65
    threshold — sector-vibes basket buys never reach the trade layer.
    """
    from src.analysis.sector_ranking import apply_rank_caps

    raw = {
        'symbol_assessments': {
            # Rank-1 single-name catalyst (NVDA-OpenAI style)
            'NVDA': {
                'direction': 'bullish', 'confidence': 0.75,
                'reasoning': "Direct OpenAI partnership.",
                'key_headline': "OpenAI and NVIDIA announce 10 GW deal",
                'impact_rank': 1,
            },
            # Rank-2 secondary (would be a sector-vibe buy without the cap)
            'AMD': {
                'direction': 'bullish', 'confidence': 0.70,
                'reasoning': "Secondary beneficiary of AI capex cycle.",
                'key_headline': "OpenAI and NVIDIA announce 10 GW deal",
                'impact_rank': 2,
            },
        }
    }
    apply_rank_caps(raw, settings={'sector_ranking': {'enabled': True}})

    nvda = raw['symbol_assessments']['NVDA']
    amd = raw['symbol_assessments']['AMD']
    # Rank 1 keeps full confidence
    assert nvda['confidence'] == 0.75
    # Rank 2 capped to 0.55
    assert amd['confidence'] == 0.55

    # Conservative threshold check (0.65 post-PR-D)
    conservative_threshold = 0.65
    assert nvda['confidence'] >= conservative_threshold, \
        "rank-1 NVDA must still pass conservative gate"
    assert amd['confidence'] < conservative_threshold, \
        "rank-2 AMD sector-vibe must be blocked by combined rank cap + threshold"


def test_pipeline_safety_net_with_cleared_headlines():
    """If a headline is cleared by Step 1, it shouldn't pollute the
    safety-net headline grouping in Step 2."""
    raw = {
        'symbol_assessments': {
            sym: {
                'direction': 'bullish',
                'confidence': 0.70,
                'reasoning': f"{sym}-specific thesis.",
                'key_headline': "Wholly unrelated random headline",
            }
            for sym in ['A', 'B', 'C', 'D', 'E']
        }
    }
    # Bogus headlines cleared → safety net's grouping sees no shared headline
    scrub_unrelated_headlines(raw)
    apply_rank_caps(raw, settings={'sector_ranking': {'enabled': True}})
    for sym in ['A', 'B', 'C', 'D', 'E']:
        # No shared-headline cap because all headlines were cleared
        assert raw['symbol_assessments'][sym]['confidence'] == 0.70
        assert 'shared_thematic_signal' not in \
            raw['symbol_assessments'][sym].get('risk_factors', [])
