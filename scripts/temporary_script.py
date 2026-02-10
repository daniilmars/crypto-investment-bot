import sys
import os
import psycopg2
import pandas as pd
from contextlib import closing

# Add the project root to the Python path
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.logger import log

def view_whale_transactions():
    """
    Connects to the cloud PostgreSQL database and lists the first 20
    whale transactions.
    """
    log.info("--- Connecting to PostgreSQL to view whale transactions ---")

    db_url = os.environ.get("DATABASE_URL")
    if not db_url:
        log.error("DATABASE_URL environment variable is not set.")
        return

    try:
        with closing(psycopg2.connect(db_url)) as conn:
            # Use pandas to read the data for nice formatting
            df = pd.read_sql("SELECT * FROM whale_transactions ORDER BY timestamp DESC LIMIT 20", conn)
            
            if not df.empty:
                log.info("Displaying the 20 most recent whale transactions:")
                # Set pandas display options to show all columns
                pd.set_option('display.max_rows', None)
                pd.set_option('display.max_columns', None)
                pd.set_option('display.width', 1000)
                pd.set_option('display.colheader_justify', 'center')
                pd.set_option('display.precision', 3)
                print(df)
            else:
                log.info("The 'whale_transactions' table is currently empty.")

    except Exception as e:
        log.error(f"Failed to connect or query PostgreSQL: {e}")

if __name__ == "__main__":
    view_whale_transactions()
