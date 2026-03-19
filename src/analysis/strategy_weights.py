"""Strategy-specific signal strength weighting.

Each trading strategy applies its own weights to the enriched Gemini
assessment fields to produce an effective signal strength. This allows
multiple parallel strategies to make different decisions from the same
Gemini output — zero extra API cost.
"""


def compute_effective_strength(
    base_signal_strength: float,
    gemini_assessment: dict | None,
    weights: dict,
) -> float:
    """Combine base signal strength with enriched Gemini fields and strategy weights.

    Args:
        base_signal_strength: Raw signal strength from signal engine (0.0-1.0).
        gemini_assessment: Per-symbol dict from Gemini grounded search, containing
            optional fields: catalyst_count, hype_vs_fundamental, risk_factors,
            catalyst_freshness, catalyst_type. May be None if no assessment available.
        weights: Strategy-specific weight config dict from settings.yaml.

    Returns:
        Effective signal strength clamped to [0.0, 1.0], or 0.0 if gated out.
    """
    score = base_signal_strength * weights.get('base_confidence', 1.0)

    if not gemini_assessment:
        return max(0.0, min(1.0, score))

    # Catalyst count bonus: reward multiple independent catalysts
    catalyst_count = gemini_assessment.get('catalyst_count', 1)
    if isinstance(catalyst_count, (int, float)):
        bonus = weights.get('catalyst_count_bonus', 0.0)
        score += max(0, catalyst_count - 1) * bonus

    # Min catalyst gate: reject if fewer than required
    min_count = weights.get('min_catalyst_count', 0)
    if isinstance(catalyst_count, (int, float)) and catalyst_count < min_count:
        return 0.0

    # Hype vs fundamental multiplier
    hype_label = gemini_assessment.get('hype_vs_fundamental', 'mixed')
    if hype_label == 'hype':
        score *= weights.get('hype_multiplier', 1.0)
    elif hype_label == 'fundamental':
        score *= weights.get('fundamental_multiplier', 1.0)
    # 'mixed' gets no adjustment

    # Breaking news boost (for momentum strategies)
    freshness = gemini_assessment.get('catalyst_freshness', 'none')
    if freshness == 'breaking':
        score += weights.get('breaking_boost', 0.0)

    # Risk factor penalty
    risk_factors = gemini_assessment.get('risk_factors')
    if isinstance(risk_factors, list):
        penalty = weights.get('risk_factor_penalty', 0.05)
        score -= len(risk_factors) * penalty

    return max(0.0, min(1.0, score))
