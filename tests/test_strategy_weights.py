"""Tests for strategy-specific signal strength weighting."""
import pytest
from src.analysis.strategy_weights import compute_effective_strength


class TestComputeEffectiveStrength:
    """Tests for compute_effective_strength()."""

    def test_no_assessment_returns_base(self):
        """Without Gemini assessment, returns base * base_confidence."""
        result = compute_effective_strength(0.7, None, {'base_confidence': 1.0})
        assert result == pytest.approx(0.7)

    def test_base_confidence_scaling(self):
        """base_confidence scales the raw signal strength."""
        result = compute_effective_strength(0.7, None, {'base_confidence': 0.8})
        assert result == pytest.approx(0.56)

    def test_hype_penalty(self):
        """Hype-driven signals get penalized with low hype_multiplier."""
        assessment = {'hype_vs_fundamental': 'hype'}
        result = compute_effective_strength(
            0.7, assessment, {'base_confidence': 1.0, 'hype_multiplier': 0.6})
        assert result == pytest.approx(0.42)

    def test_fundamental_bonus(self):
        """Fundamental-driven signals get boosted."""
        assessment = {'hype_vs_fundamental': 'fundamental'}
        result = compute_effective_strength(
            0.7, assessment, {'base_confidence': 1.0, 'fundamental_multiplier': 1.3})
        assert result == pytest.approx(0.91)

    def test_mixed_no_adjustment(self):
        """Mixed hype/fundamental gets no multiplier adjustment."""
        assessment = {'hype_vs_fundamental': 'mixed'}
        result = compute_effective_strength(
            0.7, assessment, {'base_confidence': 1.0, 'hype_multiplier': 0.5,
                              'fundamental_multiplier': 1.5})
        assert result == pytest.approx(0.7)

    def test_catalyst_count_bonus(self):
        """Multiple catalysts add bonus."""
        assessment = {'catalyst_count': 3}
        result = compute_effective_strength(
            0.6, assessment, {'base_confidence': 1.0, 'catalyst_count_bonus': 0.10})
        # 0.6 + (3-1)*0.10 = 0.8
        assert result == pytest.approx(0.8)

    def test_single_catalyst_no_bonus(self):
        """Single catalyst gets no bonus (bonus is for extras)."""
        assessment = {'catalyst_count': 1}
        result = compute_effective_strength(
            0.6, assessment, {'base_confidence': 1.0, 'catalyst_count_bonus': 0.10})
        assert result == pytest.approx(0.6)

    def test_min_catalyst_gate_rejects(self):
        """Signal rejected when catalyst_count below min_catalyst_count."""
        assessment = {'catalyst_count': 1}
        result = compute_effective_strength(
            0.9, assessment, {'base_confidence': 1.0, 'min_catalyst_count': 2})
        assert result == 0.0

    def test_min_catalyst_gate_passes(self):
        """Signal passes when catalyst_count meets min_catalyst_count."""
        assessment = {'catalyst_count': 2}
        result = compute_effective_strength(
            0.9, assessment, {'base_confidence': 1.0, 'min_catalyst_count': 2})
        assert result == pytest.approx(0.9)

    def test_risk_factor_penalty(self):
        """Risk factors reduce score."""
        assessment = {'risk_factors': ['priced in', 'low volume']}
        result = compute_effective_strength(
            0.7, assessment, {'base_confidence': 1.0, 'risk_factor_penalty': 0.10})
        # 0.7 - 2*0.10 = 0.5
        assert result == pytest.approx(0.5)

    def test_breaking_boost(self):
        """Breaking freshness adds boost."""
        assessment = {'catalyst_freshness': 'breaking'}
        result = compute_effective_strength(
            0.5, assessment, {'base_confidence': 1.0, 'breaking_boost': 0.15})
        assert result == pytest.approx(0.65)

    def test_non_breaking_no_boost(self):
        """Recent/stale freshness gets no breaking boost."""
        assessment = {'catalyst_freshness': 'recent'}
        result = compute_effective_strength(
            0.5, assessment, {'base_confidence': 1.0, 'breaking_boost': 0.15})
        assert result == pytest.approx(0.5)

    def test_clamp_max(self):
        """Score clamped to 1.0 max."""
        assessment = {'catalyst_count': 5, 'catalyst_freshness': 'breaking'}
        result = compute_effective_strength(
            0.9, assessment,
            {'base_confidence': 1.0, 'catalyst_count_bonus': 0.10,
             'breaking_boost': 0.15})
        assert result == 1.0

    def test_clamp_min(self):
        """Score clamped to 0.0 min."""
        assessment = {'risk_factors': ['a', 'b', 'c', 'd', 'e'],
                      'hype_vs_fundamental': 'hype'}
        result = compute_effective_strength(
            0.3, assessment,
            {'base_confidence': 0.5, 'hype_multiplier': 0.5,
             'risk_factor_penalty': 0.10})
        assert result == 0.0

    def test_combined_conservative_strategy(self):
        """Full conservative strategy: low base, hype penalty, catalyst gate."""
        assessment = {
            'hype_vs_fundamental': 'hype',
            'catalyst_count': 1,
            'risk_factors': ['single source'],
            'catalyst_freshness': 'breaking',
        }
        weights = {
            'base_confidence': 0.8,
            'hype_multiplier': 0.6,
            'fundamental_multiplier': 1.3,
            'catalyst_count_bonus': 0.10,
            'risk_factor_penalty': 0.10,
            'breaking_boost': 0.0,
            'min_catalyst_count': 2,
        }
        result = compute_effective_strength(0.70, assessment, weights)
        # catalyst_count=1 < min_catalyst_count=2 → rejected
        assert result == 0.0

    def test_combined_momentum_strategy(self):
        """Full momentum strategy: embrace hype, breaking boost."""
        assessment = {
            'hype_vs_fundamental': 'hype',
            'catalyst_count': 1,
            'risk_factors': [],
            'catalyst_freshness': 'breaking',
        }
        weights = {
            'base_confidence': 1.0,
            'hype_multiplier': 1.0,
            'fundamental_multiplier': 0.7,
            'catalyst_count_bonus': 0.0,
            'risk_factor_penalty': 0.0,
            'breaking_boost': 0.15,
            'min_catalyst_count': 0,
        }
        result = compute_effective_strength(0.55, assessment, weights)
        # 0.55 * 1.0 * 1.0 (hype) + 0.15 (breaking) = 0.70
        assert result == pytest.approx(0.70)

    def test_empty_weights_uses_defaults(self):
        """Empty weights dict uses safe defaults (no crash)."""
        assessment = {'catalyst_count': 2, 'hype_vs_fundamental': 'fundamental',
                      'risk_factors': ['x'], 'catalyst_freshness': 'breaking'}
        result = compute_effective_strength(0.6, assessment, {})
        # base_confidence=1.0, no multipliers, risk_penalty=0.05 default
        assert result == pytest.approx(0.55)

    def test_missing_assessment_fields_safe(self):
        """Assessment with missing fields doesn't crash."""
        assessment = {}  # all fields missing
        result = compute_effective_strength(0.7, assessment, {'base_confidence': 1.0})
        assert result == pytest.approx(0.7)
