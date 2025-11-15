import sys
import os
import argparse
import pandas as pd

# Add the project root to the Python path
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.execution.binance_trader import get_open_positions
from src.database import get_stop_loss_signals
from src.logger import log

def inspect_data(database_url: str):
    """
    Connects to the database and prints open trades and stop-loss signals.
    """
    log.info("Fetching open positions...")
    # Need to override the db connection logic for get_open_positions
    # For now, let's create a temporary direct connection logic here
    from src.database import get_db_connection
    conn = get_db_connection(database_url)
    open_positions = pd.read_sql_query("SELECT * FROM trades WHERE status = 'OPEN'", conn)
    conn.close()

    log.info("Fetching stop-loss signals...")
    stop_loss_signals = get_stop_loss_signals(db_url=database_url)

    pd.set_option('display.width', 1000)
    pd.set_option('display.max_columns', 12)

    print("\n--- 📈 Open Trades ---")
    if not open_positions.empty:
        print(open_positions)
    else:
        print("No open trades found.")

    print("\n\n--- 🚨 Stop-Loss Signals ---")
    if stop_loss_signals:
        signals_df = pd.DataFrame(stop_loss_signals)
        print(signals_df)
    else:
        print("No stop-loss signals found.")

    print("\n\n--- ACTION REQUIRED ---")
    print("Please review the lists above.")
    print("For each trade in 'Open Trades' that has a corresponding 'Stop-Loss Signal', please provide the following:")
    print("1. The `order_id` of the trade to close.")
    print("2. The `price` from the signal, which will be the exit price.")
    print("3. The `timestamp` from the signal, which will be the exit timestamp.")
    print("\nExample: 'close PAPER_ETH_BUY_12345 at 3376.82 on 2025-11-04 18:02:51'")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Inspect trade data for manual correction.")
    parser.add_argument("--db-url", required=True, help="The PostgreSQL database connection URL.")
    args = parser.parse_args()
    inspect_data(args.db_url)
