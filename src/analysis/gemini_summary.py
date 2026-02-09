import os
import vertexai
from vertexai.generative_models import GenerativeModel
from src.config import app_config
from src.logger import log
import pandas as pd

def generate_market_summary(whale_transactions: list, price_history: list, last_signal: dict) -> str:
    """
    Generates a market summary using Vertex AI Gemini based on the last 24 hours of data,
    including the bot's last generated signal.
    """
    log.info("Generating market summary with Vertex AI Gemini...")

    project_id = os.environ.get('GCP_PROJECT_ID')
    location = os.environ.get('GCP_LOCATION', 'us-central1')

    if not project_id:
        log.error("GCP_PROJECT_ID is not configured.")
        return "Error: GCP_PROJECT_ID not configured. Please set it in your environment."

    try:
        vertexai.init(project=project_id, location=location)
        model = GenerativeModel('gemini-2.0-flash')

        # --- Data Preparation ---
        whale_df = pd.DataFrame(whale_transactions)
        price_df = pd.DataFrame(price_history)

        last_signal_str = (
            f"Signal: {last_signal.get('signal', 'N/A')}\n"
            f"Symbol: {last_signal.get('symbol', 'N/A')}\n"
            f"Reason: {last_signal.get('reason', 'N/A')}\n"
            f"Timestamp: {last_signal.get('timestamp', 'N/A')}"
        )

        # --- Prompt Engineering ---
        prompt = (
            "You are an expert crypto market analyst. Your task is to provide a concise, insightful summary "
            "of market activity over the last 24 hours based on the data provided. Focus on the most significant trends, "
            "transactions, and price movements. Also, include a brief overview of the bot's recent activity. "
            "Conclude with a neutral, data-driven outlook.\n\n"
            "--- Bot Activity (Last Signal) ---\n"
            f"{last_signal_str}\n\n"
            "--- Whale Transaction Data (Last 24 Hours) ---\n"
            f"{whale_df.to_string()}\n\n"
            "--- Price Data (Last 24 Hours) ---\n"
            f"{price_df.to_string()}\n\n"
            "--- Analysis Task ---\n"
            "1. **Overall Market Health & Bot Status:** Briefly describe the general health of the market and the bot. Is the bot running smoothly? What was its last significant action?\n"
            "2. **Key Whale Movements:** Identify the 2-3 most significant whale transactions. What cryptocurrencies were moved? "
            "Were they transfers to/from exchanges (potential buy/sell pressure)?\n"
            "3. **Price Action Summary:** Summarize the price trends for the monitored cryptocurrencies. Which coins saw the most "
            "significant gains or losses?\n"
            "4. **Data-Driven Outlook:** Based ONLY on the data provided, what is the neutral outlook for the next few hours? "
            "Mention any potential indicators of volatility or stability.\n\n"
            "Provide the summary in a clear, well-formatted report using Markdown (e.g., bolding, bullet points, etc.)."
        )

        response = model.generate_content(prompt)

        log.info("Successfully generated market summary from Vertex AI Gemini.")
        return response.text

    except Exception as e:
        log.error(f"An error occurred while generating the Gemini market summary: {e}")
        return f"Error: Could not generate market summary. Details: {e}"
