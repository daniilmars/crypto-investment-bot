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

        # Get sector conviction scores for context
        sector_context = ""
        try:
            from src.analysis.sector_review import get_all_sector_convictions
            convictions = get_all_sector_convictions()
            if convictions:
                bullish = [(k, v) for k, v in convictions.items()
                           if v.get('score', 0) > 0.3]
                bearish = [(k, v) for k, v in convictions.items()
                           if v.get('score', 0) < -0.3]
                lines = []
                if bullish:
                    lines.append("Bullish sectors (last 24h): " + ", ".join(
                        f"{k} ({v['score']:+.1f}, {v.get('key_catalyst', 'N/A')})"
                        for k, v in sorted(bullish, key=lambda x: -x[1].get('score', 0))))
                if bearish:
                    lines.append("Bearish sectors (last 24h): " + ", ".join(
                        f"{k} ({v['score']:+.1f})"
                        for k, v in sorted(bearish, key=lambda x: x[1].get('score', 0))))
                if lines:
                    sector_context = "\n".join(lines) + "\n"
        except Exception:
            pass

        prompt = (
            "You are a strategic investment analyst building a 5-10 year secular growth portfolio.\n\n"
            f"{regime_context}\n"
            f"{sector_context}\n"
            "TASK: Identify 5-7 sectors with strong secular growth tailwinds for the next decade. "
            "For each sector, name 3-5 pure-play publicly traded stocks.\n\n"
            "IMPORTANT: Include BOTH long-term secular themes AND sectors with strong current "
            "macro tailwinds (e.g. energy during supply shocks, commodities during inflation).\n\n"
            "SECTORS TO CONSIDER (but not limited to):\n"
            "- Quantum computing\n"
            "- AI infrastructure (chips, data centers, cooling)\n"
            "- Clean energy / nuclear renaissance\n"
            "- Biotech / gene editing / longevity\n"
            "- Space / defense technology\n"
            "- Cybersecurity\n"
            "- Robotics / automation\n"
            "- Digital payments / fintech\n"
            "- Energy / oil & gas (when macro tailwinds present)\n"
            "- Mining / commodities / materials\n"
            "- Infrastructure / industrials\n\n"
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


def check_conviction_spike_refresh() -> dict | None:
    """Check if any high-conviction sector is missing from thesis and trigger addendum.

    Called after daily sector review. Returns summary dict if refresh happened.
    """
    from src.config import app_config

    strategies = app_config.get('settings', {}).get('strategies', {})
    cfg = strategies.get('longterm', {}).get('thesis_review', {}).get('midweek_refresh', {})
    if not cfg.get('enabled', False):
        return None

    # Need an existing thesis to merge into
    if not _thesis_cache:
        log.debug("No thesis cache — skipping midweek refresh check.")
        return None

    # Check cooldown
    from src.database import load_bot_state, save_bot_state
    from datetime import datetime, timezone, timedelta

    cooldown_hours = cfg.get('cooldown_hours', 72)
    last_refresh = load_bot_state('last_midweek_thesis_refresh')
    if last_refresh:
        try:
            last_ts = datetime.fromisoformat(last_refresh)
            if datetime.now(timezone.utc) - last_ts < timedelta(hours=cooldown_hours):
                log.debug(f"Midweek thesis refresh on cooldown until "
                          f"{last_ts + timedelta(hours=cooldown_hours)}")
                return None
        except (ValueError, TypeError):
            pass

    # Find unrepresented spike sectors
    spike_sectors = _find_unrepresented_spike_sectors(cfg)
    if not spike_sectors:
        return None

    max_sectors = cfg.get('max_sectors_per_refresh', 1)
    added_sectors = []
    added_stocks = 0

    for group_name, conviction_data in spike_sectors[:max_sectors]:
        log.info(f"Midweek thesis refresh: sector '{group_name}' has conviction "
                 f"{conviction_data.get('score', 0):+.2f} but no thesis coverage.")
        new_sector = generate_sector_addendum(group_name, conviction_data)
        if new_sector:
            _merge_sector_into_thesis(new_sector)
            added_sectors.append(group_name)
            added_stocks += len(new_sector.get('stocks', []))

    if added_sectors:
        save_bot_state('last_midweek_thesis_refresh',
                       datetime.now(timezone.utc).isoformat())
        return {'added_sectors': added_sectors, 'added_stocks': added_stocks}

    return None


def _find_unrepresented_spike_sectors(cfg: dict) -> list[tuple[str, dict]]:
    """Returns (group_name, conviction_data) for sectors with high conviction
    but no thesis coverage, sorted by conviction descending."""
    from src.analysis.sector_review import get_all_sector_convictions
    from src.analysis.sector_limits import get_group_symbols, _CRYPTO_GROUPS

    min_score = cfg.get('min_conviction_score', 0.7)
    require_accel = cfg.get('require_accelerating', True)
    min_conf = cfg.get('min_confidence', 0.6)

    convictions = get_all_sector_convictions()
    if not convictions:
        return []

    thesis_syms = get_thesis_symbols()
    results = []

    for group_name, data in convictions.items():
        # Skip crypto groups — thesis is for stocks
        if group_name in _CRYPTO_GROUPS:
            continue

        score = data.get('score', 0)
        if score < min_score:
            continue
        if require_accel and data.get('momentum') != 'accelerating':
            continue
        if data.get('review_confidence', 0) < min_conf:
            continue

        # Check if any group symbol is already in thesis
        group_syms = set(get_group_symbols(group_name))
        if group_syms & thesis_syms:
            continue  # Already represented

        results.append((group_name, data))

    results.sort(key=lambda x: -x[1].get('score', 0))
    return results


def generate_sector_addendum(sector_group: str, conviction_data: dict) -> dict | None:
    """Generate thesis stocks for a single sector using Gemini 2.5 Pro.

    Returns a sector dict matching the thesis schema, or None on failure.
    """
    try:
        import vertexai
        from vertexai.generative_models import GenerativeModel

        project_id = os.environ.get('GCP_PROJECT_ID')
        location = os.environ.get('VERTEX_AI_LOCATION') or os.environ.get('GCP_LOCATION', 'europe-west4')
        if not project_id:
            return None

        vertexai.init(project=project_id, location=location)
        model = GenerativeModel('gemini-2.5-pro')

        from src.analysis.sector_limits import get_group_symbols
        group_symbols = get_group_symbols(sector_group)
        symbols_str = ", ".join(group_symbols[:30]) if group_symbols else "N/A"

        catalyst = conviction_data.get('key_catalyst', 'strong sector momentum')
        score = conviction_data.get('score', 0)
        rationale = conviction_data.get('rationale', '')

        prompt = (
            f"A sector conviction spike has been detected for '{sector_group}'.\n\n"
            f"Conviction score: {score:+.2f}\n"
            f"Key catalyst: {catalyst}\n"
            f"Rationale: {rationale}\n"
            f"Known tickers in this sector group: {symbols_str}\n\n"
            "TASK: Select 3-5 of the best pure-play stocks for this sector thesis. "
            "You may pick from the known tickers above OR suggest other publicly traded stocks "
            "that are better pure-play representations of this sector.\n\n"
            "RESPOND ONLY with valid JSON:\n"
            "{\n"
            '  "name": "sector name (human readable)",\n'
            '  "thesis": "2-3 sentence thesis why this sector is compelling right now",\n'
            '  "conviction": 0.75,\n'
            '  "stocks": [\n'
            '    {"symbol": "TICKER", "exchange": "NASDAQ/NYSE/LSE/etc", '
            '"reasoning": "why this is a top pick"}\n'
            '  ]\n'
            "}\n\n"
            "CONVICTION SCALE: 0.5=speculative, 0.7=reasonable, 0.85=high, 0.95=very high"
        )

        response = model.generate_content(prompt)
        text = response.text.strip()
        if text.startswith('```'):
            text = text.split('\n', 1)[1].rsplit('```', 1)[0].strip()

        sector = json.loads(text)

        # Validate
        if 'stocks' not in sector or not isinstance(sector['stocks'], list):
            log.error(f"Sector addendum missing 'stocks' key for {sector_group}")
            return None

        log.info(f"Generated sector addendum for '{sector_group}': "
                 f"{len(sector['stocks'])} stocks")
        return sector

    except Exception as e:
        log.error(f"Sector addendum generation failed for {sector_group}: {e}")
        return None


def _merge_sector_into_thesis(new_sector: dict):
    """Merge a new sector into the existing thesis, save to DB, update caches."""
    global _thesis_cache, _thesis_symbols

    if not _thesis_cache:
        return

    import copy
    merged = copy.deepcopy(_thesis_cache)
    merged['sectors'].append(new_sector)

    # Save to DB
    try:
        from src.database import save_longterm_thesis
        summary = _format_thesis_summary(merged)
        save_longterm_thesis(json.dumps(merged), summary, 'gemini-2.5-pro-addendum')
        log.info(f"Merged sector '{new_sector.get('name', '?')}' into thesis. "
                 f"Now {len(merged['sectors'])} sectors, "
                 f"{sum(len(s.get('stocks', [])) for s in merged['sectors'])} stocks.")
    except Exception as e:
        log.warning(f"Failed to save merged thesis to DB: {e}")

    # Update cache
    load_thesis_into_cache(json.dumps(merged))


def _format_thesis_summary(thesis: dict) -> str:
    """Format thesis as human-readable summary."""
    lines = []
    for sector in thesis.get('sectors', []):
        stocks_str = ', '.join(s.get('symbol', '?') for s in sector.get('stocks', []))
        lines.append(f"{sector.get('name', '?')} (conv={sector.get('conviction', '?')}): {stocks_str}")
    return '\n'.join(lines)
