"""Tests for src.analysis.headline_validator.

Validator is used to drop Gemini-generated key_headlines that don't relate
to the reasoning or the symbol. Defends against the CCJ/KelpDAO bug:
reasoning about uranium gets paired with a crypto-hack headline.
"""

from src.analysis.headline_validator import (
    is_headline_consistent,
    scrub_unrelated_headlines,
)


# --- is_headline_consistent ---

class TestSymbolMention:
    def test_ticker_in_headline(self):
        assert is_headline_consistent(
            "MRK Acquires Terns Pharmaceuticals for $6.7B",
            "Strategic acquisition strengthens oncology pipeline.",
            "MRK")

    def test_company_name_via_alias(self):
        assert is_headline_consistent(
            "Merck Acquires Terns Pharmaceuticals",
            "Synergistic deal — Keytruda patent cliff defense.",
            "MRK", aliases=["Merck"])

    def test_exchange_suffix_stripped(self):
        # "BP.L" → both "bp.l" and base "BP" considered
        assert is_headline_consistent(
            "BP raises dividend after strong Q1",
            "Buyback program announced.",
            "BP.L")

    def test_word_boundary_short_ticker(self):
        # "BP" must be a word, not a substring of "bplate" or "displaced"
        assert is_headline_consistent(
            "BP unveils new offshore project",
            "Capex update.",
            "BP")
        assert not is_headline_consistent(
            "Workers displaced after corporate reshuffle",
            "Some other topic entirely.",
            "BP")


class TestTokenOverlap:
    def test_meaningful_word_overlap(self):
        assert is_headline_consistent(
            "Novo Nordisk's Wegovy pill launch draws new patients",
            "Wegovy launch expands the weight loss drug market.",
            "NOVO-B.CO")

    def test_only_stopwords_overlap_fails(self):
        # both contain "strong" + "market" but those are stopwords
        assert not is_headline_consistent(
            "Strong market sentiment lifts indices",
            "Strong positive market signals.",
            "XYZ")

    def test_word_form_variants_match(self):
        # acquires vs acquisition should match via 5-char prefix stem
        assert is_headline_consistent(
            "Merck Acquires Terns Pharmaceuticals for $6.7B",
            "Strategic acquisition strengthens oncology pipeline.",
            "MRK")

    def test_no_overlap_no_symbol_fails(self):
        # The CCJ regression: uranium reasoning + crypto-hack headline
        assert not is_headline_consistent(
            "North Korea's Lazarus suspected of stealing US$290M in KelpDAO cyberattack",
            "Approval of a new SMR plant and positive uranium potential outweigh "
            "geopolitical concerns, providing a bullish signal.",
            "CCJ")

    def test_nvda_with_coinbase_headline_fails(self):
        # Real example pulled from quantify_issues.py — NVDA reasoning paired
        # with a Bitcoin/Coinbase headline
        assert not is_headline_consistent(
            "Massive Coinbase News! Bitcoin Rips to $96,750!",
            "Multiple strong announcements regarding NVIDIA's AI advancements, "
            "strategic partnerships, and new tools point to significant fund flows.",
            "NVDA")


class TestEdgeCases:
    def test_empty_headline_passes(self):
        # No headline = nothing to invalidate
        assert is_headline_consistent("", "some reasoning", "AAPL")
        assert is_headline_consistent(None, "some reasoning", "AAPL")

    def test_empty_reasoning_passes(self):
        # Can't judge without reasoning — keep the headline
        assert is_headline_consistent("Apple unveils new iPhone", "", "AAPL")
        assert is_headline_consistent("Apple unveils new iPhone", None, "AAPL")

    def test_short_words_dont_count(self):
        # Words < 6 chars are excluded from overlap counting
        assert not is_headline_consistent(
            "Cat dog run jump up down out",
            "Cat dog run jump in.",
            "XYZ")


# --- scrub_unrelated_headlines ---

class TestScrub:
    def test_clears_only_mismatches(self):
        result = {
            'symbol_assessments': {
                'MRK': {
                    'key_headline': "Merck Acquires Terns Pharmaceuticals",
                    'reasoning': "Strategic acquisition strengthens pipeline.",
                },
                'CCJ': {
                    'key_headline': "Lazarus crypto hack steals $290M",
                    'reasoning': "SMR plant approval and uranium upside.",
                },
            }
        }
        cleared = scrub_unrelated_headlines(result, context='test')
        assert cleared == 1
        assert result['symbol_assessments']['MRK']['key_headline'] == \
            "Merck Acquires Terns Pharmaceuticals"
        assert result['symbol_assessments']['CCJ']['key_headline'] == ''

    def test_no_assessments_returns_zero(self):
        assert scrub_unrelated_headlines({}, 'test') == 0
        assert scrub_unrelated_headlines({'symbol_assessments': None}, 'test') == 0

    def test_handles_non_dict_assessment_entries(self):
        # Defensive: Gemini sometimes returns a list/string for malformed entries
        result = {'symbol_assessments': {'BAD': "not a dict"}}
        assert scrub_unrelated_headlines(result, 'test') == 0

    def test_aliases_keep_headline(self):
        result = {
            'symbol_assessments': {
                'MRK': {
                    'key_headline': "Merck Inc reports record quarter",
                    'reasoning': "Filings released for prior period analysis only.",
                }
            }
        }
        # Without aliases, "Merck" alone doesn't help — but token "merck"
        # is ≥6 chars so it counts. This still passes.
        assert scrub_unrelated_headlines(
            result, 'test', aliases_by_symbol={'MRK': ['Merck']}) == 0

    def test_missing_headline_no_op(self):
        result = {
            'symbol_assessments': {
                'AAPL': {'reasoning': "valid reasoning"},
            }
        }
        assert scrub_unrelated_headlines(result, 'test') == 0
