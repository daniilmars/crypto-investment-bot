import argparse
import pandas as pd
from sqlalchemy import create_engine
from datetime import datetime, timedelta

def analyze_performance(database_url: str):
    """
    Connects to the database, fetches trades from the last 7 days,
    and prints a performance summary.
    """
    try:
        engine = create_engine(database_url)
        
        # Calculate the date 7 days ago
        seven_days_ago = datetime.utcnow() - timedelta(days=7)
        
        # Query the trades table using parameterized query
        query = "SELECT * FROM trades WHERE entry_timestamp >= %(cutoff)s"
        trades_df = pd.read_sql(query, engine, params={"cutoff": seven_days_ago})
        
        if trades_df.empty:
            print("No trades found in the last 7 days.")
            return
            
        # --- Performance Calculations ---
        closed_trades = trades_df[trades_df['status'] == 'CLOSED']
        
        if closed_trades.empty:
            print("No closed trades found in the last 7 days.")
            return
            
        total_trades = len(closed_trades)
        winning_trades = closed_trades[closed_trades['pnl'] > 0]
        losing_trades = closed_trades[closed_trades['pnl'] < 0]
        
        win_rate = (len(winning_trades) / total_trades) * 100 if total_trades > 0 else 0
        total_pnl = closed_trades['pnl'].sum()
        average_pnl = closed_trades['pnl'].mean()
        
        # --- Display Summary ---
        print("\n--- 📈 Performance Summary (Last 7 Days) ---")
        print(f"Total Closed Trades: {total_trades}")
        print(f"Winning Trades:      {len(winning_trades)}")
        print(f"Losing Trades:       {len(losing_trades)}")
        print(f"Win Rate:            {win_rate:.2f}%")
        print(f"Total PnL:           ${total_pnl:,.2f}")
        print(f"Average PnL/Trade:   ${average_pnl:,.2f}")
        print("\n--- 📊 All Trades (Last 7 Days) ---")
        print(trades_df.to_string())

    except Exception as e:
        print(f"An error occurred: {e}")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Analyze trading bot performance.")
    parser.add_argument("--db-url", required=True, help="The PostgreSQL database connection URL.")
    args = parser.parse_args()
    analyze_performance(args.db_url)
