"""Longterm investment thesis generator — autonomous sector + stock selection.

Uses Gemini 2.5 Pro weekly to identify 5-7 sectors with secular growth
tailwinds and 3-5 pure-play stocks per sector. The thesis drives the
longterm strategy's BUY decisions.
"""

import json
import os

from src.logger import log

# Module-level thesis cache
_thesis_cache: dict | None = None
_thesis_symbols: set[str] = set()


def generate_investment_thesis() -> dict | None:
    """Generate a new investment thesis using Gemini 2.5 Pro.

    Returns structured dict with sectors, stocks, and reasoning.
    Saves to DB and updates module cache.
    """
    try:
        import vertexai
        from vertexai.generative_models import GenerativeModel

        project_id = os.environ.get('GCP_PROJECT_ID')
        location = os.environ.get('VERTEX_AI_LOCATION') or os.environ.get('GCP_LOCATION', 'europe-west4')
        if not project_id:
            log.warning("GCP_PROJECT_ID not set — skipping thesis generation.")
            return None

        vertexai.init(project=project_id, location=location)
        model = GenerativeModel('gemini-2.5-pro')

        # Get current regime for context
        try:
            from src.analysis.macro_regime import get_macro_regime
            regime = get_macro_regime()
            regime_context = (
                f"Current market regime: {regime['regime']} (score={regime['score']}). "
                f"VIX: {regime.get('indicators', {}).get('vix', {}).get('current', '?')}. "
            )
        except Exception:
            regime_context = ""

        prompt = (
            "You are a strategic investment analyst building a 5-10 year secular growth portfolio.\n\n"
            f"{regime_context}\n"
            "TASK: Identify 5-7 sectors with strong secular growth tailwinds for the next decade. "
            "For each sector, name 3-5 pure-play publicly traded stocks.\n\n"
            "SECTORS TO CONSIDER (but not limited to):\n"
            "- Quantum computing\n"
            "- AI infrastructure (chips, data centers, cooling)\n"
            "- Clean energy / nuclear renaissance\n"
            "- Biotech / gene editing / longevity\n"
            "- Space / defense technology\n"
            "- Cybersecurity\n"
            "- Robotics / automation\n"
            "- Digital payments / fintech\n\n"
            "CRITERIA FOR STOCKS:\n"
            "- Must be publicly traded (US, EU, or Asia exchanges)\n"
            "- Prefer pure-play exposure to the thesis (not conglomerates)\n"
            "- Consider market cap, competitive moat, and growth runway\n"
            "- Include both established leaders AND high-potential challengers\n\n"
            "RESPOND ONLY with valid JSON:\n"
            "{\n"
            '  "sectors": [\n'
            '    {\n'
            '      "name": "sector name",\n'
            '      "thesis": "2-3 sentence thesis why this sector grows for 10 years",\n'
            '      "conviction": 0.85,\n'
            '      "stocks": [\n'
            '        {"symbol": "TICKER", "exchange": "NASDAQ/NYSE/LSE/TSE/etc", '
            '"reasoning": "why this is a top pick"}\n'
            '      ]\n'
            '    }\n'
            '  ],\n'
            '  "macro_view": "1-2 sentence overall market view"\n'
            "}\n\n"
            "CONVICTION SCALE: 0.5=speculative, 0.7=reasonable, 0.85=high, 0.95=very high\n"
            "Include 20-35 total stocks across all sectors."
        )

        response = model.generate_content(prompt)
        text = response.text.strip()

        # Parse JSON (handle markdown fences)
        if text.startswith('```'):
            text = text.split('\n', 1)[1].rsplit('```', 1)[0].strip()

        thesis = json.loads(text)

        # Validate structure
        if 'sectors' not in thesis or not isinstance(thesis['sectors'], list):
            log.error("Thesis missing 'sectors' key")
            return None

        # Save to DB
        try:
            from src.database import save_longterm_thesis
            summary = _format_thesis_summary(thesis)
            save_longterm_thesis(json.dumps(thesis), summary, 'gemini-2.5-pro')
            log.info(f"Generated investment thesis: {len(thesis['sectors'])} sectors, "
                     f"{sum(len(s.get('stocks', [])) for s in thesis['sectors'])} stocks")
        except Exception as e:
            log.warning(f"Failed to save thesis to DB: {e}")

        # Update cache
        load_thesis_into_cache(json.dumps(thesis))

        return thesis

    except Exception as e:
        log.error(f"Thesis generation failed: {e}")
        return None


def get_thesis_symbols() -> set[str]:
    """Get the set of stock symbols from the current thesis.

    Returns cached set, or loads from DB if cache is empty.
    """
    if _thesis_symbols:
        return _thesis_symbols

    # Try loading from DB
    try:
        from src.database import get_active_thesis
        thesis_row = get_active_thesis()
        if thesis_row and thesis_row.get('thesis_json'):
            load_thesis_into_cache(thesis_row['thesis_json'])
    except Exception as e:
        log.debug(f"Could not load thesis from DB: {e}")

    return _thesis_symbols


def get_thesis_context_for_symbol(symbol: str) -> str | None:
    """Get thesis context for a specific symbol (for position analyst).

    Returns a string describing the sector thesis and stock rationale,
    or None if the symbol isn't in the thesis.
    """
    if not _thesis_cache or symbol not in _thesis_symbols:
        return None

    for sector in _thesis_cache.get('sectors', []):
        for stock in sector.get('stocks', []):
            if stock.get('symbol') == symbol:
                return (
                    f"Sector: {sector.get('name', '?')}\n"
                    f"Thesis: {sector.get('thesis', '?')}\n"
                    f"Stock rationale: {stock.get('reasoning', '?')}\n"
                    f"Conviction: {sector.get('conviction', '?')}"
                )
    return None


def load_thesis_into_cache(thesis_json: str):
    """Parse thesis JSON and populate module-level cache."""
    global _thesis_cache, _thesis_symbols
    try:
        thesis = json.loads(thesis_json) if isinstance(thesis_json, str) else thesis_json
        _thesis_cache = thesis
        _thesis_symbols = set()
        for sector in thesis.get('sectors', []):
            for stock in sector.get('stocks', []):
                sym = stock.get('symbol')
                if sym:
                    _thesis_symbols.add(sym)
        log.info(f"Thesis cache loaded: {len(_thesis_symbols)} symbols "
                 f"across {len(thesis.get('sectors', []))} sectors")
    except Exception as e:
        log.warning(f"Failed to parse thesis JSON: {e}")
        _thesis_cache = None
        _thesis_symbols = set()


def _format_thesis_summary(thesis: dict) -> str:
    """Format thesis as human-readable summary."""
    lines = []
    for sector in thesis.get('sectors', []):
        stocks_str = ', '.join(s.get('symbol', '?') for s in sector.get('stocks', []))
        lines.append(f"{sector.get('name', '?')} (conv={sector.get('conviction', '?')}): {stocks_str}")
    return '\n'.join(lines)
