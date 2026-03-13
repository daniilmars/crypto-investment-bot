import os
import vertexai
from vertexai.generative_models import GenerativeModel
from src.logger import log
import pandas as pd

def generate_market_summary(price_history: list, last_signal: dict,
                            open_positions: list = None) -> str:
    """
    Generates a market summary using Vertex AI Gemini based on the last 24 hours of data,
    including the bot's last generated signal and open positions.

    Args:
        price_history: list of price records from the last 24h
        last_signal: dict with the bot's last signal
        open_positions: optional list of open position dicts with keys:
            symbol, entry_price, current_price, pnl_percentage, quantity
    """
    log.info("Generating market summary with Vertex AI Gemini...")

    project_id = os.environ.get('GCP_PROJECT_ID')
    location = os.environ.get('VERTEX_AI_LOCATION') or os.environ.get('GCP_LOCATION', 'europe-west4')

    if not project_id:
        log.error("GCP_PROJECT_ID is not configured.")
        return "Error: GCP_PROJECT_ID not configured. Please set it in your environment."

    try:
        vertexai.init(project=project_id, location=location)
        model = GenerativeModel('gemini-2.5-flash-lite')

        # --- Data Preparation ---
        price_df = pd.DataFrame(price_history)

        last_signal_str = (
            f"Signal: {last_signal.get('signal', 'N/A')}\n"
            f"Symbol: {last_signal.get('symbol', 'N/A')}\n"
            f"Reason: {last_signal.get('reason', 'N/A')}\n"
            f"Timestamp: {last_signal.get('timestamp', 'N/A')}"
        )

        # Build open positions context
        positions_str = "No open positions."
        if open_positions:
            pos_lines = []
            for p in open_positions:
                sym = p.get('symbol', '?')
                entry = p.get('entry_price', 0)
                current = p.get('current_price', 0)
                pnl = p.get('pnl_percentage', 0)
                pos_lines.append(f"- {sym}: entry ${entry:,.2f}, current ${current:,.2f}, PnL {pnl:+.2f}%")
            positions_str = "\n".join(pos_lines)

        # --- Prompt Engineering ---
        prompt = (
            "You are a trading bot operator's morning briefing assistant. Generate a scannable "
            "market summary in EXACTLY this 5-section format. This will be read on a phone via "
            "Telegram, so keep it concise.\n\n"
            "--- Bot Activity (Last Signal) ---\n"
            f"{last_signal_str}\n\n"
            "--- Open Positions ---\n"
            f"{positions_str}\n\n"
            "--- Price Data (Last 24 Hours) ---\n"
            f"{price_df.to_string()}\n\n"
            "Bot risk params: SL -3.5%, TP +8%, trailing stop activates at +2%.\n\n"
            "--- OUTPUT FORMAT (follow exactly) ---\n\n"
            "Section 1: TOP LINE\n"
            "Start with a single bold sentence: the most important development in the last 24h. "
            "Be opinionated, not neutral. Example: 'BTC broke $70k on ETF inflow momentum.'\n\n"
            "Section 2: MARKET SNAPSHOT\n"
            "One bullet per asset that moved >2%. Skip quiet assets. Format:\n"
            "- SYMBOL: price, change%, one-word direction (rallying/sliding/flat/volatile)\n\n"
            "Section 3: OPEN POSITIONS\n"
            "For each open position: PnL%, how close to SL or TP, one-line assessment. "
            "If no positions, write 'No open positions.'\n\n"
            "Section 4: NEXT 12 HOURS\n"
            "2-3 upcoming catalysts or risk events to watch. If nothing notable, say so.\n\n"
            "Section 5: BOT HEALTH\n"
            "Last signal type + timestamp. Any anomalies (long time since signal, unusual pattern). "
            "One line: 'Running normally' or flag the issue.\n\n"
            "--- FORMATTING RULES ---\n"
            "- Use bold (*text*) for section headers and key data. NO # markdown headers.\n"
            "- Use bullet points (- item) for lists.\n"
            "- Keep total length under 2000 characters.\n"
            "- No emojis except in section headers (one per header max).\n"
            "- Write for a human operator scanning on their phone in 30 seconds."
        )

        response = model.generate_content(prompt)

        log.info("Successfully generated market summary from Vertex AI Gemini.")
        return response.text

    except Exception as e:
        log.error(f"An error occurred while generating the Gemini market summary: {e}")
        return f"Error: Could not generate market summary. Details: {e}"
