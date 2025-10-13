import requests
import json

# Binance API base URL
BINANCE_API_URL = "https://api.binance.com/api/v3"

def get_current_price(symbol: str):
    """
    Fetches the latest price for a specific symbol from the Binance API.

    Args:
        symbol (str): The trading symbol to fetch the price for (e.g., "BTCUSDT", "ETHUSDT").

    Returns:
        dict: A dictionary containing the symbol and its price if the request is successful,
              otherwise None.
    """
    endpoint = f"{BINANCE_API_URL}/ticker/price"
    params = {'symbol': symbol}

    try:
        response = requests.get(endpoint, params=params)
        response.raise_for_status()  # Raise an exception for bad status codes

        price_data = response.json()
        print(f"Successfully fetched price for {symbol}: {price_data.get('price')}")
        return price_data

    except requests.exceptions.HTTPError as http_err:
        # Handle cases where the symbol might not exist
        if response.status_code == 400:
            print(f"Error: Invalid symbol '{symbol}'. The trading pair may not exist on Binance.")
        else:
            print(f"HTTP error occurred: {http_err}")
        return None
    except requests.exceptions.RequestException as e:
        print(f"Error fetching price data from Binance: {e}")
        return None
    except json.JSONDecodeError:
        print("Error: Could not decode JSON response from the Binance API.")
        return None

if __name__ == '__main__':
    # This block allows you to run the script directly for testing purposes.
    print("--- Testing Binance Data Collector ---")

    # Test case 1: Get the price for BTCUSDT
    print("\nFetching price for BTCUSDT...")
    btc_price = get_current_price("BTCUSDT")
    if btc_price:
        print(f"Current BTC Price: ${float(btc_price['price']):,.2f}")

    # Test case 2: Get the price for ETHUSDT
    print("\nFetching price for ETHUSDT...")
    eth_price = get_current_price("ETHUSDT")
    if eth_price:
        print(f"Current ETH Price: ${float(eth_price['price']):,.2f}")
        
    # Test case 3: Get the price for a non-existent symbol
    print("\nFetching price for a non-existent symbol (XYZABC)...")
    invalid_price = get_current_price("XYZABC")
    if not invalid_price:
        print("Correctly handled non-existent symbol.")

    print("\n--- Test Complete ---")
