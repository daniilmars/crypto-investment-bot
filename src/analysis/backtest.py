import sqlite3
import pandas as pd
import sys
import os

# Add the project root to the Python path to allow imports from 'src'
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..')))

from src.database import get_db_connection
from src.analysis.signal_engine import generate_comprehensive_signal

# --- Backtesting Configuration ---
INITIAL_CAPITAL = 10000  # Start with $10,000
TRADE_SIZE = 1000      # Each trade will be $1,000

def load_historical_data():
    """Loads all historical data from the database and merges it into a single DataFrame."""
    print("Loading historical data from database...")
    conn = get_db_connection()
    
    # Load data into pandas DataFrames
    fng = pd.read_sql_query("SELECT * FROM fear_and_greed", conn)
    prices = pd.read_sql_query("SELECT * FROM market_prices WHERE symbol = 'BTCUSDT'", conn) # Focus on BTC for now
    whales = pd.read_sql_query("SELECT * FROM whale_transactions", conn)
    
    conn.close()

    # Convert timestamp columns to datetime objects for proper merging
    fng['date'] = pd.to_datetime(fng['timestamp'], unit='s').dt.date
    # Rename price timestamp to avoid conflicts
    prices.rename(columns={'timestamp': 'price_timestamp'}, inplace=True)
    prices['date'] = pd.to_datetime(prices['price_timestamp']).dt.date
    whales['date'] = pd.to_datetime(whales['timestamp'], unit='s').dt.date

    # Aggregate whale data by day
    whale_summary = whales.groupby('date').agg(
        num_transactions=('id', 'count'),
        total_usd=('amount_usd', 'sum')
    ).reset_index()

    # Merge the datasets
    data = pd.merge(prices, fng, on='date', how='left')
    data = pd.merge(data, whale_summary, on='date', how='left')

    # Forward-fill missing F&G values
    data['value'] = data['value'].ffill()
    data['value_classification'] = data['value_classification'].ffill()
    data = data.fillna(0) # Fill any remaining NaNs

    # Calculate a simple moving average (SMA) for the price
    data['price_sma_5'] = data['price'].rolling(window=5).mean()
    # Drop initial rows where SMA is not available
    data = data.dropna().reset_index(drop=True)

    print(f"Loaded and merged {len(data)} data points for backtesting.")
    return data

def run_backtest(data):
    """Runs the backtesting simulation."""
    print("\n--- Starting Backtest Simulation ---")
    capital = INITIAL_CAPITAL
    position = 0  # Current holdings in the asset (e.g., BTC)
    trades = 0

    if data.empty:
        print("No data to backtest.")
        return

    for i, row in data.iterrows():
        # Simulate the data available at that point in time
        fng_data = [{'value': row['value'], 'value_classification': row['value_classification']}]
        whale_data = [{'amount_usd': row['total_usd']}] if row['num_transactions'] > 0 else []
        market_data = {'current_price': row['price'], 'sma_5': row['price_sma_5']}
        
        signal_data = generate_comprehensive_signal(fng_data, whale_data, market_data)
        signal = signal_data.get('signal')
        
        current_price = row['price']

        # --- Trading Logic ---
        if signal == 'BUY' and capital >= TRADE_SIZE:
            # Buy
            position += TRADE_SIZE / current_price
            capital -= TRADE_SIZE
            trades += 1
            print(f"{row['date']}: BUY signal. Bought {TRADE_SIZE / current_price:.6f} BTC at ${current_price:,.2f}")
        
        elif signal == 'SELL' and position > 0:
            # Sell everything
            sell_value = position * current_price
            capital += sell_value
            position = 0
            trades += 1
            print(f"{row['date']}: SELL signal. Sold all BTC for ${sell_value:,.2f}")

    # --- Final Results ---
    final_portfolio_value = capital + (position * data.iloc[-1]['price'])
    profit = final_portfolio_value - INITIAL_CAPITAL
    profit_percent = (profit / INITIAL_CAPITAL) * 100

    print("\n--- Backtest Results ---")
    print(f"Initial Capital: ${INITIAL_CAPITAL:,.2f}")
    print(f"Final Portfolio Value: ${final_portfolio_value:,.2f}")
    print(f"Total Profit/Loss: ${profit:,.2f} ({profit_percent:.2f}%)")
    print(f"Total Trades: {trades}")
    print("------------------------")


if __name__ == '__main__':
    # Ensure pandas is installed
    try:
        import pandas
    except ImportError:
        print("Pandas is not installed. Please run: pip install pandas")
    else:
        historical_data = load_historical_data()
        run_backtest(historical_data)
