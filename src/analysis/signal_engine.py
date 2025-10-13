# This module will contain the logic for analyzing data and generating trading signals.

def generate_signal_from_fear_and_greed(fear_and_greed_data: list):
    """
    Analyzes the Fear & Greed Index data to generate a simple trading signal.

    This is a basic rule-based example:
    - If the latest F&G value is "Extreme Fear" (e.g., < 25), it's a potential BUY signal.
    - If the latest F&G value is "Extreme Greed" (e.g., > 75), it's a potential SELL signal.
    - Otherwise, it's a HOLD signal.

    Args:
        fear_and_greed_data (list): A list of dictionaries, where each dictionary
                                    is a data point from the Fear & Greed API.
                                    It's assumed the list is sorted with the
                                    latest data point at index 0.

    Returns:
        dict: A dictionary containing the signal ('BUY', 'SELL', 'HOLD'),
              the reason, and the data used for the decision.
              Returns None if the input data is invalid.
    """
    if not fear_and_greed_data or not isinstance(fear_and_greed_data, list) or len(fear_and_greed_data) == 0:
        print("Error: Invalid or empty data provided to signal engine.")
        return None

    latest_entry = fear_and_greed_data[0]
    value = int(latest_entry.get('value', 50)) # Default to a neutral 50 if 'value' is missing
    classification = latest_entry.get('value_classification', 'Neutral')

    signal = {
        "signal": "HOLD",
        "reason": f"Fear & Greed Index is {value} ({classification}), which is neutral.",
        "source": "Fear & Greed Index",
        "details": latest_entry
    }

    if classification == "Extreme Fear":
        signal["signal"] = "BUY"
        signal["reason"] = f"Fear & Greed Index is at {value} (Extreme Fear), indicating a potential buying opportunity."
    elif classification == "Extreme Greed":
        signal["signal"] = "SELL"
        signal["reason"] = f"Fear & Greed Index is at {value} (Extreme Greed), indicating a potential selling opportunity."

    return signal

if __name__ == '__main__':
    # This block allows you to run the script directly for testing purposes.
    print("--- Testing Signal Engine ---")

    # Test case 1: Extreme Fear (should be a BUY signal)
    print("\nTesting with 'Extreme Fear' data...")
    test_data_buy = [{'value': '15', 'value_classification': 'Extreme Fear', 'timestamp': 'N/A'}]
    signal_buy = generate_signal_from_fear_and_greed(test_data_buy)
    print(f"Generated Signal: {signal_buy['signal']}")
    print(f"Reason: {signal_buy['reason']}")

    # Test case 2: Fear (should be a HOLD signal)
    print("\nTesting with 'Fear' data...")
    test_data_hold_fear = [{'value': '35', 'value_classification': 'Fear', 'timestamp': 'N/A'}]
    signal_hold_fear = generate_signal_from_fear_and_greed(test_data_hold_fear)
    print(f"Generated Signal: {signal_hold_fear['signal']}")
    print(f"Reason: {signal_hold_fear['reason']}")

    # Test case 3: Extreme Greed (should be a SELL signal)
    print("\nTesting with 'Extreme Greed' data...")
    test_data_sell = [{'value': '85', 'value_classification': 'Extreme Greed', 'timestamp': 'N/A'}]
    signal_sell = generate_signal_from_fear_and_greed(test_data_sell)
    print(f"Generated Signal: {signal_sell['signal']}")
    print(f"Reason: {signal_sell['reason']}")
    
    # Test case 4: Empty data
    print("\nTesting with empty data...")
    signal_empty = generate_signal_from_fear_and_greed([])
    print(f"Generated Signal: {signal_empty}")

    print("\n--- Test Complete ---")
