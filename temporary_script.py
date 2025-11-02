# temporary_script.py
import os
from sqlalchemy import create_engine, text
from src.logger import log

def get_db_engine():
    """Creates a database engine from environment variables."""
    db_url = os.getenv('DATABASE_URL')
    if not db_url:
        log.error("DATABASE_URL environment variable is not set.")
        return None
    return create_engine(db_url)

def get_distinct_symbols():
    """Connects to the database and fetches distinct symbols."""
    engine = get_db_engine()
    if not engine:
        return

    try:
        with engine.connect() as connection:
            query = text("SELECT DISTINCT symbol FROM market_prices;")
            result = connection.execute(query)
            symbols = [row[0] for row in result]
            print("Distinct symbols in market_prices table:")
            for symbol in symbols:
                print(symbol)
    except Exception as e:
        log.error(f"An error occurred while fetching symbols: {e}")

if __name__ == "__main__":
    # We need to configure the DATABASE_URL from the GitHub secrets.
    # Since this script is run locally, we'll need to provide it.
    # This is a placeholder and will be replaced by the actual secret.
    os.environ['DATABASE_URL'] = os.getenv('DATABASE_URL_SECRET')
    get_distinct_symbols()
