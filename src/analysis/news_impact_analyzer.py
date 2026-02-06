import json

from src.config import app_config
from src.logger import log


def _get_client():
    """Returns an Anthropic client or None if not configured."""
    api_key = app_config.get('api_keys', {}).get('anthropic')
    if not api_key or api_key == 'YOUR_ANTHROPIC_API_KEY':
        log.warning("Anthropic API key not configured. Claude analysis unavailable.")
        return None
    try:
        import anthropic
        return anthropic.Anthropic(api_key=api_key)
    except Exception as e:
        log.error(f"Failed to create Anthropic client: {e}")
        return None


def _build_prompt(headlines_by_symbol, current_prices):
    """Builds a structured prompt for Claude to analyze news impact."""
    symbol_sections = []
    for symbol, headlines in headlines_by_symbol.items():
        price = current_prices.get(symbol, 'N/A')
        headline_list = '\n'.join(f'  - {h}' for h in headlines[:10])
        symbol_sections.append(
            f"### {symbol} (Current Price: ${price})\n{headline_list}"
        )

    symbols_text = '\n\n'.join(symbol_sections)

    return f"""You are a financial market analyst. Analyze the following news headlines and assess their likely impact on each asset's price in the next 1-24 hours.

{symbols_text}

Respond ONLY with valid JSON in this exact format, no other text:
{{
    "symbol_assessments": {{
        "SYMBOL": {{
            "direction": "bullish|bearish|neutral",
            "confidence": 0.0-1.0,
            "reasoning": "Brief explanation"
        }}
    }},
    "market_mood": "Brief overall market sentiment summary"
}}"""


def analyze_news_impact(headlines_by_symbol, current_prices):
    """
    Calls Claude API for deep news impact analysis.

    Args:
        headlines_by_symbol: dict mapping symbol -> list of headline strings
        current_prices: dict mapping symbol -> current price float

    Returns:
        dict with 'symbol_assessments' and 'market_mood', or None on failure.
    """
    if not headlines_by_symbol:
        return None

    client = _get_client()
    if client is None:
        return None

    try:
        prompt = _build_prompt(headlines_by_symbol, current_prices)

        message = client.messages.create(
            model="claude-sonnet-4-5-20250929",
            max_tokens=1024,
            messages=[{"role": "user", "content": prompt}],
        )

        response_text = message.content[0].text.strip()

        # Parse JSON from response
        result = json.loads(response_text)

        # Validate expected structure
        if 'symbol_assessments' not in result:
            log.error("Claude response missing 'symbol_assessments' key.")
            return None

        log.info(f"Claude news analysis complete. Market mood: {result.get('market_mood', 'N/A')}")
        return result

    except json.JSONDecodeError as e:
        log.error(f"Failed to parse Claude response as JSON: {e}")
        return None
    except Exception as e:
        log.error(f"Error during Claude news analysis: {e}")
        return None
