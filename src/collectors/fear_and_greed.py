import requests
import json
from src.database import get_db_connection
from src.logger import log

# The API endpoint for the Fear & Greed Index.
FEAR_AND_GREED_API_URL = "https://api.alternative.me/fng/?limit=100"

def save_fear_and_greed_data(data: list):
    """Saves Fear & Greed data to the database."""
    if not data:
        return

    conn = get_db_connection()
    cursor = conn.cursor()
    
    for entry in data:
        # Use INSERT OR IGNORE to avoid errors if the timestamp already exists.
        cursor.execute('''
            INSERT OR IGNORE INTO fear_and_greed (timestamp, value, value_classification)
            VALUES (?, ?, ?)
        ''', (entry['timestamp'], entry['value'], entry['value_classification']))
    
    conn.commit()
    conn.close()
    log.info(f"Saved {len(data)} entries to the fear_and_greed table.")

def get_fear_and_greed_index(limit: int = 1):
    """
    Fetches the Fear & Greed Index from the alternative.me API and saves it to the database.
    """
    try:
        response = requests.get(f"{FEAR_AND_GREED_API_URL}&limit={limit}")
        response.raise_for_status()
        
        data = response.json().get('data')
        
        if data:
            log.info(f"Successfully fetched {len(data)} Fear & Greed Index values.")
            save_fear_and_greed_data(data) # Save the data
            return data
        else:
            log.warning("No data found in the F&G API response.")
            return None

    except requests.exceptions.RequestException as e:
        log.error(f"Error fetching Fear & Greed Index: {e}")
        return None
    except json.JSONDecodeError:
        log.error("Could not decode JSON response from the F&G API.")
        return None

if __name__ == '__main__':
    log.info("--- Testing Fear & Greed Index Collector (with DB saving) ---")
    get_fear_and_greed_index(limit=10)
    log.info("--- Test Complete ---")
