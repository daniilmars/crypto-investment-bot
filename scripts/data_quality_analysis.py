import sys
import os
import pandas as pd
import matplotlib.pyplot as plt
import seaborn as sns

# Add the project root to the Python path
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.database import get_db_connection
from src.logger import log
from src.config import app_config

def analyze_data_quality():
    """
    Performs a comprehensive data quality analysis on the collected crypto data.
    1.  Checks for data completeness and gaps.
    2.  Analyzes the statistical distribution of key features.
    3.  Calculates cross-correlations between features.
    4.  Generates a consolidated report and visualizations.
    """
    log.info("--- 🔬 Starting Data Quality Analysis ---")
    conn = get_db_connection()
    watch_list = app_config.get('settings', {}).get('watch_list', ['BTC'])
    report_path = "output/data_quality_report.txt"

    with open(report_path, 'w') as report_file:
        report_file.write("--- Crypto Bot: Data Quality Analysis Report ---\\n\\n")

        for symbol in watch_list:
            log.info(f"--- Analyzing data for {symbol} ---")
            report_file.write(f"--- Analysis for: {symbol} ---\\n")

            # --- 1. Data Loading ---
            try:
                prices_df = pd.read_sql(f"SELECT * FROM market_prices WHERE symbol LIKE '{symbol}%'", conn, parse_dates=['timestamp'])
                whales_df = pd.read_sql(f"SELECT * FROM whale_transactions WHERE symbol = '{symbol.lower()}'", conn, parse_dates=['timestamp'])
                sentiment_df = pd.read_sql(f"SELECT * FROM news_sentiment WHERE symbol = '{symbol.lower()}'", conn, parse_dates=['timestamp'])
            except Exception as e:
                log.error(f"Failed to load data for {symbol}: {e}")
                report_file.write(f"Error loading data: {e}\\n\\n")
                continue

            if prices_df.empty:
                log.warning(f"No market price data for {symbol}. Skipping.")
                report_file.write("No market price data found.\\n\\n")
                continue

            # --- 2. Data Completeness & Sparsity ---
            report_file.write("\\n1. Data Completeness & Sparsity:\\n")
            
            # Create a complete hourly index for the last 30 days
            end_date = prices_df['timestamp'].max()
            start_date = end_date - pd.Timedelta(days=30)
            complete_hourly_index = pd.date_range(start=start_date, end=end_date, freq='h', tz='UTC')

            # Ensure all dataframes are timezone-aware (UTC) before resampling
            prices_df.set_index('timestamp', inplace=True)
            if prices_df.index.tz is None:
                prices_df.index = prices_df.index.tz_localize('UTC')
            else:
                prices_df.index = prices_df.index.tz_convert('UTC')

            whales_df.set_index('timestamp', inplace=True)
            if whales_df.index.tz is None:
                whales_df.index = whales_df.index.tz_localize('UTC')
            else:
                whales_df.index = whales_df.index.tz_convert('UTC')

            sentiment_df.set_index('timestamp', inplace=True)
            if sentiment_df.index.tz is None:
                sentiment_df.index = sentiment_df.index.tz_localize('UTC')
            else:
                sentiment_df.index = sentiment_df.index.tz_convert('UTC')

            # Resample each data source to this common index
            prices_resampled = prices_df.resample('h').last()
            
            # Select only numeric columns for aggregation
            numeric_whales_df = whales_df.select_dtypes(include=np.number)
            whales_resampled = numeric_whales_df.resample('h').sum()
            
            # Drop the non-numeric 'symbol' column before resampling
            sentiment_numeric = sentiment_df.drop(columns=['symbol'])
            sentiment_resampled = sentiment_numeric.resample('h').mean()

            # Calculate completeness
            price_completeness = (prices_resampled['price'].notna().sum() / len(complete_hourly_index)) * 100
            whale_completeness = (whales_resampled['amount_usd'].notna().sum() / len(complete_hourly_index)) * 100
            sentiment_completeness = (sentiment_resampled['avg_sentiment_score'].notna().sum() / len(complete_hourly_index)) * 100

            report_file.write(f"  - Price Data Completeness (hourly): {price_completeness:.2f}%\\n")
            report_file.write(f"  - Whale Transaction Data Completeness (hourly): {whale_completeness:.2f}%\\n")
            report_file.write(f"  - News Sentiment Data Completeness (hourly): {sentiment_completeness:.2f}%\\n")

            # --- 3. Statistical Distribution ---
            report_file.write("\\n2. Statistical Distribution of Key Features:\\n")
            
            # Price distribution
            report_file.write("\\n  - Market Prices:\\n")
            report_file.write(prices_df['price'].describe().to_string())
            report_file.write("\\n")

            # Whale transaction distribution
            if not whales_df.empty:
                report_file.write("\\n  - Whale Transaction Sizes (USD):\\n")
                report_file.write(whales_df['amount_usd'].describe().to_string())
                report_file.write("\\n")

            # Sentiment score distribution
            if not sentiment_df.empty:
                report_file.write("\\n  - Average Sentiment Scores:\\n")
                report_file.write(sentiment_df['avg_sentiment_score'].describe().to_string())
                report_file.write("\\n")

            # --- 4. Correlation Analysis ---
            log.info(f"Generating correlation heatmap for {symbol}...")
            
            # Create a merged dataframe for correlation
            merged_df = prices_resampled[['price']].join(whales_resampled[['amount_usd']], how='inner')
            if not sentiment_resampled.empty:
                 merged_df = merged_df.join(sentiment_resampled[['avg_sentiment_score']], how='inner')

            if len(merged_df) > 1:
                correlation_matrix = merged_df.corr()
                plt.figure(figsize=(10, 8))
                sns.heatmap(correlation_matrix, annot=True, cmap='coolwarm', fmt=".2f")
                plt.title(f'Feature Correlation Matrix for {symbol}')
                plot_path = f"output/correlation_heatmap_{symbol}.png"
                plt.savefig(plot_path)
                plt.close()
                log.info(f"Saved correlation heatmap for {symbol} to {plot_path}")
                report_file.write(f"\\n3. Correlation matrix saved to: {plot_path}\\n")
            else:
                report_file.write("\\n3. Correlation matrix could not be generated (insufficient overlapping data).\\n")

            report_file.write("\\n" + "="*40 + "\\n\\n")

    log.info(f"--- ✅ Data Quality Analysis Complete. Report saved to {report_path} ---")

if __name__ == "__main__":
    analyze_data_quality()