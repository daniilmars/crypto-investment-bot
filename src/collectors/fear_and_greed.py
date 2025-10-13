import requests
import json

# The API endpoint for the Fear & Greed Index. It returns the last 100 days of data.
FEAR_AND_GREED_API_URL = "https://api.alternative.me/fng/?limit=100"

def get_fear_and_greed_index(limit: int = 1):
    """
    Fetches the Fear & Greed Index from the alternative.me API.

    Args:
        limit (int): The number of results to return. Defaults to 1 (latest value).
                     Set to a higher number to get historical data.

    Returns:
        dict: A dictionary containing the Fear & Greed data if the request is successful,
              otherwise None. The dictionary includes 'value', 'value_classification',
              and 'timestamp'.
    """
    try:
        response = requests.get(f"{FEAR_AND_GREED_API_URL}&limit={limit}")
        # Raise an exception for bad status codes (4xx or 5xx)
        response.raise_for_status()
        
        data = response.json()
        
        if 'data' in data and len(data['data']) > 0:
            print(f"Successfully fetched {len(data['data'])} Fear & Greed Index values.")
            return data['data']
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
    # This block allows you to run the script directly for testing purposes.
    print("--- Testing Fear & Greed Index Collector ---")
    
    # Test case 1: Get the latest value
    print("\nFetching the latest value...")
    latest_value = get_fear_and_greed_index(limit=1)
    if latest_value:
        print(f"Latest Fear & Greed Value: {latest_value[0]['value']} ({latest_value[0]['value_classification']})")

    # Test case 2: Get the last 5 values
    print("\nFetching the last 5 values...")
    last_5_values = get_fear_and_greed_index(limit=5)
    if last_5_values:
        for entry in last_5_values:
            print(f"- Date: {entry['timestamp']}, Value: {entry['value']}, Classification: {entry['value_classification']}")
            
    print("\n--- Test Complete ---")
