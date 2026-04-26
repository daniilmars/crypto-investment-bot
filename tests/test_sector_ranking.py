"""Tests for src.analysis.sector_ranking.apply_rank_caps.

Covers:
  - rank-1 keeps confidence
  - rank-2 capped + tagged
  - rank-3+ capped + tagged
  - counter_impacted flipped to neutral
  - shared-headline safety net (≥5 symbols, no ranks → all capped)
  - shadow mode (enabled=False): tags applied, confidence NOT changed
  - empty/malformed inputs don't crash
"""

from src.analysis.sector_ranking import apply_rank_caps


def _enabled():
    return {'sector_ranking': {'enabled': True}}


def _shadow():
    return {'sector_ranking': {'enabled': False}}


# --- rank-based caps ---

class TestRankCaps:
    def test_rank_1_unchanged(self):
        result = {'symbol_assessments': {
            'XOM': {'direction': 'bullish', 'confidence': 0.85, 'impact_rank': 1}
        }}
        apply_rank_caps(result, _enabled())
        a = result['symbol_assessments']['XOM']
        assert a['confidence'] == 0.85
        assert 'secondary_beneficiary' not in a.get('risk_factors', [])

    def test_rank_2_capped_to_055(self):
        result = {'symbol_assessments': {
            'BP.L': {'direction': 'bullish', 'confidence': 0.75, 'impact_rank': 2}
        }}
        apply_rank_caps(result, _enabled())
        a = result['symbol_assessments']['BP.L']
        assert a['confidence'] == 0.55
        assert 'secondary_beneficiary' in a['risk_factors']

    def test_rank_2_low_confidence_not_raised(self):
        # cap is a ceiling, not a floor
        result = {'symbol_assessments': {
            'X': {'direction': 'bullish', 'confidence': 0.30, 'impact_rank': 2}
        }}
        apply_rank_caps(result, _enabled())
        assert result['symbol_assessments']['X']['confidence'] == 0.30

    def test_rank_3_capped_to_040(self):
        result = {'symbol_assessments': {
            'X': {'direction': 'bullish', 'confidence': 0.70, 'impact_rank': 3}
        }}
        apply_rank_caps(result, _enabled())
        assert result['symbol_assessments']['X']['confidence'] == 0.40

    def test_rank_5_capped_to_040(self):
        result = {'symbol_assessments': {
            'X': {'direction': 'bullish', 'confidence': 0.80, 'impact_rank': 5}
        }}
        apply_rank_caps(result, _enabled())
        assert result['symbol_assessments']['X']['confidence'] == 0.40

    def test_rank_none_unchanged(self):
        # Single-symbol catalysts and "no rank given" should pass through
        result = {'symbol_assessments': {
            'X': {'direction': 'bullish', 'confidence': 0.70}
        }}
        apply_rank_caps(result, _enabled())
        assert result['symbol_assessments']['X']['confidence'] == 0.70


class TestCounterImpacted:
    def test_flipped_to_neutral_when_enabled(self):
        result = {'symbol_assessments': {
            'VLO': {'direction': 'bullish', 'confidence': 0.70,
                    'impact_basis': 'counter-impacted'}
        }}
        apply_rank_caps(result, _enabled())
        a = result['symbol_assessments']['VLO']
        assert a['direction'] == 'neutral'
        assert a['confidence'] == 0.0
        assert 'counter_impacted' in a['risk_factors']

    def test_underscore_form_recognized(self):
        result = {'symbol_assessments': {
            'VLO': {'direction': 'bullish', 'confidence': 0.70,
                    'impact_basis': 'counter_impacted'}
        }}
        apply_rank_caps(result, _enabled())
        assert result['symbol_assessments']['VLO']['direction'] == 'neutral'

    def test_shadow_only_tags_does_not_flip(self):
        result = {'symbol_assessments': {
            'VLO': {'direction': 'bullish', 'confidence': 0.70,
                    'impact_basis': 'counter-impacted'}
        }}
        apply_rank_caps(result, _shadow())
        a = result['symbol_assessments']['VLO']
        assert a['direction'] == 'bullish'
        assert a['confidence'] == 0.70
        assert 'counter_impacted' in a['risk_factors']  # tag still applied


# --- safety net ---

class TestSharedHeadlineSafetyNet:
    def test_5_plus_symbols_same_headline_capped(self):
        head = "Iran ratchets tensions in Strait of Hormuz"
        result = {'symbol_assessments': {
            sym: {'direction': 'bullish', 'confidence': 0.70,
                  'key_headline': head}
            for sym in ['XOM', 'BP.L', 'CVX', 'SHEL.L', 'EOG']
        }}
        apply_rank_caps(result, _enabled())
        for sym in ['XOM', 'BP.L', 'CVX', 'SHEL.L', 'EOG']:
            a = result['symbol_assessments'][sym]
            assert a['confidence'] == 0.45
            assert 'shared_thematic_signal' in a['risk_factors']

    def test_4_symbols_below_threshold(self):
        head = "Iran ratchets tensions in Strait of Hormuz"
        result = {'symbol_assessments': {
            sym: {'direction': 'bullish', 'confidence': 0.70,
                  'key_headline': head}
            for sym in ['XOM', 'BP.L', 'CVX', 'SHEL.L']
        }}
        apply_rank_caps(result, _enabled())
        for sym in ['XOM', 'BP.L', 'CVX', 'SHEL.L']:
            assert result['symbol_assessments'][sym]['confidence'] == 0.70

    def test_safety_net_skipped_when_ranks_present(self):
        # If Gemini DID give ranks, we trust the ranking; safety net is
        # only for the "everyone gets the same confidence" failure mode.
        head = "Defense bill passes"
        result = {'symbol_assessments': {
            'LMT': {'direction': 'bullish', 'confidence': 0.70,
                    'key_headline': head, 'impact_rank': 1},
            'GD':  {'direction': 'bullish', 'confidence': 0.55,
                    'key_headline': head, 'impact_rank': 2},
            'NOC': {'direction': 'bullish', 'confidence': 0.40,
                    'key_headline': head, 'impact_rank': 3},
            'HII': {'direction': 'bullish', 'confidence': 0.40,
                    'key_headline': head, 'impact_rank': 3},
            'LHX': {'direction': 'bullish', 'confidence': 0.40,
                    'key_headline': head, 'impact_rank': 3},
        }}
        apply_rank_caps(result, _enabled())
        # LMT is unchanged (rank=1)
        assert result['symbol_assessments']['LMT']['confidence'] == 0.70
        # No 'shared_thematic_signal' tags applied — ranks were given
        for sym in ['LMT', 'GD', 'NOC', 'HII', 'LHX']:
            tags = result['symbol_assessments'][sym].get('risk_factors', [])
            assert 'shared_thematic_signal' not in tags

    def test_safety_net_skips_low_conf(self):
        head = "Macro headline"
        result = {'symbol_assessments': {
            sym: {'direction': 'bullish', 'confidence': 0.30,
                  'key_headline': head}
            for sym in ['A', 'B', 'C', 'D', 'E']
        }}
        apply_rank_caps(result, _enabled())
        for sym in ['A', 'B', 'C', 'D', 'E']:
            assert result['symbol_assessments'][sym]['confidence'] == 0.30


# --- edge cases / shape robustness ---

class TestRobustness:
    def test_missing_assessments_no_op(self):
        result = {}
        apply_rank_caps(result, _enabled())
        assert result == {} or '_sector_ranking_stats' not in result

    def test_non_dict_assessment_skipped(self):
        result = {'symbol_assessments': {'BAD': "not a dict"}}
        apply_rank_caps(result, _enabled())
        assert result['symbol_assessments']['BAD'] == "not a dict"

    def test_stats_recorded(self):
        result = {'symbol_assessments': {
            'A': {'direction': 'bullish', 'confidence': 0.7, 'impact_rank': 2},
            'B': {'direction': 'bullish', 'confidence': 0.7, 'impact_rank': 3},
            'C': {'direction': 'bullish', 'confidence': 0.7,
                  'impact_basis': 'counter-impacted'},
        }}
        apply_rank_caps(result, _enabled())
        s = result['_sector_ranking_stats']
        assert s['enabled'] is True
        assert s['rank_2_caps'] == 1
        assert s['rank_3plus_caps'] == 1
        assert s['counter_impacted'] == 1

    def test_no_settings_uses_defaults(self):
        # No settings → defaults (rank2 → 0.55) should apply
        result = {'symbol_assessments': {
            'X': {'direction': 'bullish', 'confidence': 0.70, 'impact_rank': 2}
        }}
        apply_rank_caps(result, settings=None)
        # enabled defaults to False → confidence NOT capped
        assert result['symbol_assessments']['X']['confidence'] == 0.70
        # but tag IS applied
        assert 'secondary_beneficiary' in \
            result['symbol_assessments']['X']['risk_factors']

    def test_existing_risk_factors_preserved(self):
        result = {'symbol_assessments': {
            'X': {'direction': 'bullish', 'confidence': 0.70,
                  'impact_rank': 2, 'risk_factors': ['priced in']}
        }}
        apply_rank_caps(result, _enabled())
        assert 'priced in' in result['symbol_assessments']['X']['risk_factors']
        assert 'secondary_beneficiary' in result['symbol_assessments']['X']['risk_factors']

    def test_idempotent_does_not_double_tag(self):
        result = {'symbol_assessments': {
            'X': {'direction': 'bullish', 'confidence': 0.70, 'impact_rank': 2}
        }}
        apply_rank_caps(result, _enabled())
        apply_rank_caps(result, _enabled())
        tags = result['symbol_assessments']['X']['risk_factors']
        assert tags.count('secondary_beneficiary') == 1
