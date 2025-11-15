import sqlite3
import pandas as pd
import sys
import os

# Add the project root to the Python path
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.logger import log

def view_optimization_results(db_path="data/crypto_data.db"):
    """
    Connects to the SQLite database and prints the optimization results.
    """
    try:
        log.info(f"Connecting to SQLite database at {db_path}...")
        conn = sqlite3.connect(db_path)
        
        query = "SELECT * FROM optimization_results ORDER BY pnl DESC"
        df = pd.read_sql_query(query, conn)
        
        conn.close()
        
        if df.empty:
            log.info("No optimization results found in the database.")
            return
            
        log.info("\n--- 📈 Optimization Results ---")
        # Print the DataFrame in a readable format
        print(df.to_string())

    except sqlite3.OperationalError as e:
        log.error(f"Database error: {e}. It's possible the table doesn't exist yet.")
    except Exception as e:
        log.error(f"An error occurred: {e}", exc_info=True)

if __name__ == "__main__":
    view_optimization_results()

