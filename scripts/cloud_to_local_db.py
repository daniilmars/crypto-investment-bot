import sys
import os
import pandas as pd
import sqlite3
import psycopg2

# Add the project root to the Python path
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.database import get_db_connection
from src.logger import log

def transfer_whale_data():
    """
    Transfers the whale_transactions table from the local SQLite database
    to the cloud PostgreSQL database.
    """
    log.info("--- 🚚 Starting Data Transfer: SQLite to PostgreSQL ---")

    # --- 1. Connect to SQLite and Read Data ---
    log.info("Connecting to local SQLite database to read whale transactions...")
    sqlite_conn = None
    try:
        sqlite_conn = get_db_connection()
        whales_df = pd.read_sql("SELECT * FROM whale_transactions", sqlite_conn)
        log.info(f"Successfully read {len(whales_df)} records from SQLite.")
    except Exception as e:
        log.error(f"Failed to read data from SQLite: {e}")
        return
    finally:
        if sqlite_conn:
            sqlite_conn.close()

    # --- 2. Connect to PostgreSQL and Write Data ---
    log.info("Connecting to PostgreSQL to write data...")
    pg_conn = None
    try:
        # Use the environment variable for the DB URL
        db_url = os.environ.get("DATABASE_URL")
        if not db_url:
            log.error("DATABASE_URL environment variable is not set. Aborting.")
            return
            
        pg_conn = psycopg2.connect(db_url)
        
        # Clear the existing table
        log.info("Clearing existing whale_transactions data from PostgreSQL...")
        cursor = pg_conn.cursor()
        cursor.execute("DELETE FROM whale_transactions")
        pg_conn.commit()
        log.info("Successfully cleared remote table.")

        # Write the new data
        whales_df.to_sql('whale_transactions', pg_conn, if_exists='append', index=False, method='multi')
        log.info(f"Successfully wrote {len(whales_df)} records to PostgreSQL.")
    except Exception as e:
        log.error(f"Failed to write data to PostgreSQL: {e}")
    finally:
        if pg_conn:
            pg_conn.close()
    
    log.info("--- ✅ Data Transfer Complete ---")

if __name__ == "__main__":
    transfer_whale_data()