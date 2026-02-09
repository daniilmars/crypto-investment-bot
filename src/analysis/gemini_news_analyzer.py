import json
import os

import vertexai
from vertexai.generative_models import GenerativeModel

from src.logger import log


def analyze_news_with_search(symbols: list, current_prices: dict) -> dict | None:
    """
    Uses Gemini with Google Search grounding to find and analyze news for the given symbols.

    Instead of relying on RSS feeds + VADER, this lets Gemini search the web itself
    and return grounded sentiment assessments with citations.

    Args:
        symbols: list of ticker symbols (e.g. ['BTC', 'ETH', 'AAPL'])
        current_prices: {symbol: float} — current prices for context

    Returns:
        dict with 'symbol_assessments' and 'market_mood', or None on failure.
    """
    project_id = os.environ.get('GCP_PROJECT_ID')
    location = os.environ.get('GCP_LOCATION', 'us-central1')

    if not project_id:
        log.warning("GCP_PROJECT_ID not set — skipping Gemini grounded news analysis.")
        return None

    if not symbols:
        return None

    try:
        from google import genai
        from google.genai.types import GenerateContentConfig, Tool, GoogleSearch

        client = genai.Client(vertexai=True, project=project_id, location=location)

        # Build price context
        price_lines = []
        for sym in symbols:
            price = current_prices.get(sym)
            if price:
                price_lines.append(f"- {sym}: ${price:,.2f}")
            else:
                price_lines.append(f"- {sym}: price unknown")
        price_context = "\n".join(price_lines)

        symbols_str = ", ".join(symbols)
        prompt = (
            f"Search for the latest news (last 24-48 hours) about these assets: {symbols_str}\n\n"
            f"Current prices:\n{price_context}\n\n"
            "For each asset, assess the short-term market sentiment based on the news you find.\n\n"
            "Respond ONLY with valid JSON in this exact format:\n"
            "{\n"
            '  "symbol_assessments": {\n'
            '    "SYMBOL": {"direction": "bullish|bearish|neutral", "confidence": 0.0, "reasoning": "..."}\n'
            "  },\n"
            '  "market_mood": "..."\n'
            "}\n\n"
            "Rules:\n"
            "- direction must be one of: bullish, bearish, neutral\n"
            "- confidence must be a float between 0.0 and 1.0\n"
            "- reasoning should be one sentence summarizing the key news drivers\n"
            "- market_mood should be a brief overall sentiment phrase\n"
            "- Include ALL requested symbols in symbol_assessments\n"
            "- Do not include any text outside the JSON object"
        )

        response = client.models.generate_content(
            model="gemini-2.0-flash",
            contents=prompt,
            config=GenerateContentConfig(
                tools=[Tool(google_search=GoogleSearch())],
                temperature=0.2,
            ),
        )

        text = response.text.strip()

        # Strip markdown code fences if present
        if text.startswith("```"):
            text = text.split("\n", 1)[1] if "\n" in text else text[3:]
            if text.endswith("```"):
                text = text[:-3].strip()

        result = json.loads(text)
        log.info(f"Gemini grounded news analysis complete: mood={result.get('market_mood')}, "
                 f"symbols={list(result.get('symbol_assessments', {}).keys())}")
        return result

    except ImportError:
        log.warning("google-genai SDK not installed — skipping grounded news analysis.")
        return None
    except json.JSONDecodeError as e:
        log.error(f"Failed to parse Gemini grounded news response as JSON: {e}")
        return None
    except Exception as e:
        log.error(f"Gemini grounded news analysis failed: {e}")
        return None


def analyze_news_impact(headlines_by_symbol: dict, current_prices: dict) -> dict | None:
    """
    Uses Vertex AI Gemini to analyze news headlines and assess market impact per symbol.

    Args:
        headlines_by_symbol: {symbol: [headline1, headline2, ...]}
        current_prices: {symbol: float}

    Returns:
        dict with 'symbol_assessments' and 'market_mood', or None on failure.
    """
    project_id = os.environ.get('GCP_PROJECT_ID')
    location = os.environ.get('GCP_LOCATION', 'us-central1')

    if not project_id:
        log.warning("GCP_PROJECT_ID not set — skipping Gemini news analysis.")
        return None

    if not headlines_by_symbol:
        return None

    try:
        vertexai.init(project=project_id, location=location)
        model = GenerativeModel('gemini-2.0-flash')

        # Build the prompt
        symbol_sections = []
        for symbol, headlines in headlines_by_symbol.items():
            price = current_prices.get(symbol, "unknown")
            top_headlines = headlines[:10]
            section = f"**{symbol}** (current price: ${price})\n" + "\n".join(
                f"- {h}" for h in top_headlines
            )
            symbol_sections.append(section)

        headlines_text = "\n\n".join(symbol_sections)

        prompt = (
            "You are a financial news analyst. Analyze these recent headlines and assess "
            "the likely short-term market impact for each asset.\n\n"
            f"{headlines_text}\n\n"
            "For each symbol, provide:\n"
            "- direction: one of 'bullish', 'bearish', or 'neutral'\n"
            "- confidence: a float between 0.0 and 1.0\n"
            "- reasoning: a brief one-sentence explanation\n\n"
            "Also provide an overall 'market_mood' string (e.g. 'cautiously optimistic').\n\n"
            "Respond ONLY with valid JSON in this exact format:\n"
            "{\n"
            '  "symbol_assessments": {\n'
            '    "SYMBOL": {"direction": "bullish|bearish|neutral", "confidence": 0.0, "reasoning": "..."}\n'
            "  },\n"
            '  "market_mood": "..."\n'
            "}"
        )

        response = model.generate_content(prompt)
        text = response.text.strip()

        # Strip markdown code fences if present
        if text.startswith("```"):
            text = text.split("\n", 1)[1] if "\n" in text else text[3:]
            if text.endswith("```"):
                text = text[:-3].strip()

        result = json.loads(text)
        log.info(f"Gemini news analysis complete: mood={result.get('market_mood')}, "
                 f"symbols={list(result.get('symbol_assessments', {}).keys())}")
        return result

    except json.JSONDecodeError as e:
        log.error(f"Failed to parse Gemini news response as JSON: {e}")
        return None
    except Exception as e:
        log.error(f"Gemini news analysis failed: {e}")
        return None
