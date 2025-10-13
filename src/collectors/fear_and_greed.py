import requests
import json
from src.database import get_db_connection

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
    print(f"Saved {len(data)} entries to the fear_and_greed table.")

def get_fear_and_greed_index(limit: int = 1):
    """
    Fetches the Fear & Greed Index from the alternative.me API and saves it to the database.
    """
    try:
        response = requests.get(f"{FEAR_AND_GREED_API_URL}&limit={limit}")
        response.raise_for_status()
        
        data = response.json().get('data')
        
        if data:
            print(f"Successfully fetched {len(data)} Fear & Greed Index values.")
            save_fear_and_greed_data(data) # Save the data
            return data
        else:
            print("Warning: No data found in the API response.")
            return None

    except requests.exceptions.RequestException as e:
        print(f"Error fetching Fear & Greed Index: {e}")
        return None
    except json.JSONDecodeError:
        print("Error: Could not decode JSON response from the API.")
        return None

if __name__ == '__main__':
    print("--- Testing Fear & Greed Index Collector (with DB saving) ---")
    
    # Fetch and save the last 10 values to test the database logic.
    get_fear_and_greed_index(limit=10)
            
    print("\n--- Test Complete ---")
