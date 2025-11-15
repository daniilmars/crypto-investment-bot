import sys
import os
import pandas as pd
import argparse
from datetime import datetime, timedelta
from vaderSentiment.vaderSentiment import SentimentIntensityAnalyzer
from newsapi import NewsApiClient

# Add the project root to the Python path
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.database import get_db_connection
from src.config import app_config
from src.logger import log

def collect_and_analyze_sentiment(start_date_str=None, end_date_str=None):
    """
    Collects news articles for all symbols in the watch_list, analyzes their sentiment,
    and stores the aggregated hourly data in the database.
    """
    log.info("---📰 Starting News Sentiment Collection for All Watchlist Symbols ---")

    # --- 1. Load Settings & Initialize APIs ---
    api_keys = app_config.get('api_keys', {})
    if not api_keys or not api_keys.get('newsapi'):
        log.error("NewsAPI key not found in settings.yaml. Aborting.")
        return

    newsapi = NewsApiClient(api_key=api_keys['newsapi'])
    analyzer = SentimentIntensityAnalyzer()
    watch_list = app_config.get('settings', {}).get('watch_list', ['BTC'])

    # --- 2. Determine Date Range ---
    if start_date_str and end_date_str:
        log.info(f"Running in BACKFILL mode from {start_date_str} to {end_date_str}")
        from_date = start_date_str
        to_date = end_date_str
    else:
        log.info("Running in LIVE mode for the last 24 hours.")
        from_date = (datetime.utcnow() - timedelta(days=1)).strftime('%Y-%m-%d')
        to_date = datetime.utcnow().strftime('%Y-%m-%d')

    # --- 3. Loop Through Watchlist and Collect Data ---
    all_sentiment_data = []
    for symbol in watch_list:
        log.info(f"--- Fetching news for {symbol} ---")
        try:
            all_articles = newsapi.get_everything(q=symbol,
                                                  language='en',
                                                  sort_by='publishedAt',
                                                  from_param=from_date,
                                                  to=to_date)
        except Exception as e:
            log.error(f"Failed to fetch articles for {symbol} from NewsAPI: {e}")
            continue

        if not all_articles or all_articles['status'] != 'ok' or not all_articles['articles']:
            log.warning(f"No articles found for {symbol}. Skipping.")
            continue

        log.info(f"Successfully fetched {len(all_articles['articles'])} articles for {symbol}.")

        for article in all_articles['articles']:
            title = article.get('title', '')
            if not title:
                continue
            
            sentiment = analyzer.polarity_scores(title)
            all_sentiment_data.append({
                'timestamp': pd.to_datetime(article['publishedAt']),
                'symbol': symbol.lower(), # Store symbol in lowercase to match whale data
                'headline': title,
                'sentiment_score': sentiment['compound']
            })

    if not all_sentiment_data:
        log.warning("No valid headlines found across all symbols. Exiting.")
        return

    df = pd.DataFrame(all_sentiment_data).set_index('timestamp')

    # --- 4. Aggregate Data into Hourly Buckets (per symbol) ---
    log.info("Aggregating sentiment data into hourly features for each symbol...")
    
    # Group by symbol and then resample
    grouped = df.groupby('symbol')
    hourly_dfs = []
    for name, group in grouped:
        hourly_sentiment = pd.DataFrame(index=group.index.floor('h').unique())
        hourly_sentiment['avg_sentiment_score'] = group['sentiment_score'].resample('h').mean()
        hourly_sentiment['news_volume'] = group['headline'].resample('h').count()
        hourly_sentiment['sentiment_volatility'] = group['sentiment_score'].resample('h').std()
        
        group['is_positive'] = group['sentiment_score'] > 0.05
        group['is_negative'] = group['sentiment_score'] < -0.05
        hourly_sentiment['positive_buzz_ratio'] = group['is_positive'].resample('h').sum() / hourly_sentiment['news_volume']
        hourly_sentiment['negative_buzz_ratio'] = group['is_negative'].resample('h').sum() / hourly_sentiment['news_volume']
        
        hourly_sentiment['symbol'] = name
        hourly_dfs.append(hourly_sentiment)

    final_df = pd.concat(hourly_dfs).fillna(0).reset_index().rename(columns={'index': 'timestamp'})

    # --- 5. Store Data in Database ---
    log.info(f"Storing {len(final_df)} rows of hourly sentiment data...")
    conn = get_db_connection()
    try:
        # Clear the table first to avoid duplicate data
        log.info("Clearing old sentiment data...")
        conn.execute("DELETE FROM news_sentiment")
        final_df.to_sql('news_sentiment', conn, if_exists='append', index=False)
        log.info("Successfully stored new sentiment data.")
    except Exception as e:
        log.error(f"Database error: {e}")
    finally:
        conn.close()

    log.info("--- ✅ News Sentiment Collection Complete ---")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Collect news sentiment data for all watchlist symbols.")
    parser.add_argument('--backfill', nargs=2, metavar=('START_DATE', 'END_DATE'),
                        help='Run in backfill mode. Provide start and end dates in YYYY-MM-DD format.')
    args = parser.parse_args()

    if args.backfill:
        collect_and_analyze_sentiment(start_date_str=args.backfill[0], end_date_str=args.backfill[1])
    else:
        collect_and_analyze_sentiment()
