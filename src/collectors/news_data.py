import requests
import yaml
import os
import sys
from datetime import datetime, timedelta

# Add project root to Python path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..')))

from src.database import get_db_connection
from src.logger import log

# NewsAPI base URL
NEWS_API_URL = "https://newsapi.org/v2/everything"

# --- Simple Sentiment Analysis ---
POSITIVE_KEYWORDS = ['rally', 'surge', 'breakthrough', 'partnership', 'bullish', 'gains', 'optimism', 'upgrade']
NEGATIVE_KEYWORDS = ['crash', 'hack', 'scam', 'ban', 'regulation', 'bearish', 'losses', 'vulnerability', 'fear']

def analyze_sentiment(text: str) -> str:
    """Performs a simple keyword-based sentiment analysis on a text."""
    text = text.lower()
    pos_count = sum(1 for word in POSITIVE_KEYWORDS if word in text)
    neg_count = sum(1 for word in NEGATIVE_KEYWORDS if word in text)

    if pos_count > neg_count:
        return 'Positive'
    elif neg_count > pos_count:
        return 'Negative'
    else:
        return 'Neutral'

# --- Configuration Loading ---
def load_config():
    """Loads the configuration from the settings.yaml file."""
    script_dir = os.path.dirname(os.path.abspath(__file__))
    config_path = os.path.join(script_dir, '..', '..', 'config', 'settings.yaml')
    try:
        with open(config_path, 'r') as f: return yaml.safe_load(f)
    except (FileNotFoundError, yaml.YAMLError) as e:
        log.error(f"Error loading config: {e}")
        return None

# --- Database Logic ---
def save_news_articles(articles: list, symbol: str):
    """Saves news articles to the database."""
    if not articles:
        return

    conn = get_db_connection()
    cursor = conn.cursor()
    
    for article in articles:
        sentiment = analyze_sentiment(article.get('title', ''))
        cursor.execute('''
            INSERT OR IGNORE INTO news_articles (url, symbol, title, description, source, published_at, sentiment)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        ''', (
            article['url'], symbol, article['title'], article.get('description'),
            article['source']['name'], article['publishedAt'], sentiment
        ))
    
    conn.commit()
    conn.close()
    log.info(f"Processed {len(articles)} news articles for {symbol} for the database.")

# --- News Fetching Logic ---
def get_recent_crypto_news(symbol: str):
    """Fetches recent news for a specific crypto symbol from the NewsAPI."""
    config = load_config()
    if not config: return None

    api_key = config.get('api_keys', {}).get('newsapi')
    if not api_key or api_key == "YOUR_NEWSAPI_ORG_KEY":
        log.error("NewsAPI key is not configured.")
        return None

    # Fetch news from the last 24 hours
    from_date = (datetime.utcnow() - timedelta(days=1)).strftime('%Y-%m-%dT%H:%M:%SZ')
    
    params = {
        'q': f'"{symbol}" OR "{symbol.replace("USDT", "")}"', # Search for "BTCUSDT" or "BTC"
        'apiKey': api_key,
        'from': from_date,
        'sortBy': 'publishedAt',
        'language': 'en',
        'pageSize': 20 # Limit to the 20 most recent articles
    }

    try:
        response = requests.get(NEWS_API_URL, params=params)
        response.raise_for_status()
        data = response.json()
        
        if data.get('status') == 'ok':
            articles = data.get('articles', [])
            log.info(f"Successfully fetched {len(articles)} news articles for {symbol}.")
            if articles:
                save_news_articles(articles, symbol)
            return articles
        else:
            log.warning(f"NewsAPI returned an error: {data.get('message')}")
            return None
    except requests.exceptions.RequestException as e:
        log.error(f"Error fetching from NewsAPI: {e}")
        return None

if __name__ == '__main__':
    log.info("--- Testing News Data Collector ---")
    # We need to initialize the DB to ensure the new table is created.
    from src.database import initialize_database
    initialize_database()
    
    get_recent_crypto_news("BTCUSDT")
    log.info("--- Test Complete ---")
