import sys
import os
import pandas as pd
import numpy as np
from sklearn.preprocessing import StandardScaler
from sklearn.model_selection import train_test_split
from tensorflow.keras.models import Sequential
from tensorflow.keras.layers import LSTM, Dense, Dropout
from tensorflow.keras.callbacks import EarlyStopping
import matplotlib.pyplot as plt
import seaborn as sns

# Add the project root to the Python path
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.database import get_db_connection
from src.logger import log
from src.config import app_config

def create_sequences(data, sequence_length):
    """
    Creates sequences of a fixed length from the time-series data.
    """
    sequences = []
    labels = []
    for i in range(len(data) - sequence_length):
        sequences.append(data.iloc[i:i+sequence_length].values)
        labels.append(data.iloc[i+sequence_length]['target'])
    return np.array(sequences), np.array(labels)

def train_lstm_for_symbol(symbol: str, sequence_length: int = 24):
    """
    Trains an LSTM model for a single cryptocurrency symbol using price and whale data.
    """
    log.info(f"--- 🧠 Starting LSTM Model Training for {symbol} ---")
    conn = get_db_connection()

    # --- 1. Data Loading & Feature Engineering ---
    log.info(f"Phase 1: Loading data and engineering features for {symbol}...")
    try:
        prices_df = pd.read_sql(f"SELECT * FROM market_prices WHERE symbol LIKE '{symbol}%'", conn, parse_dates=['timestamp'])
        whales_df = pd.read_sql(f"SELECT * FROM whale_transactions WHERE symbol = '{symbol.lower()}'", conn, parse_dates=['timestamp'])
    except Exception as e:
        log.error(f"Failed to load data for {symbol}: {e}")
        return

    if prices_df.empty:
        log.warning(f"No market price data for {symbol}. Skipping.")
        return

    # Set and standardize timezone to UTC
    for df in [prices_df, whales_df]:
        df.set_index('timestamp', inplace=True)
        if df.index.tz is None:
            df.index = df.index.tz_localize('UTC')
        else:
            df.index = df.index.tz_convert('UTC')

    # Create target variable
    hourly_prices = prices_df['price'].resample('h').last().to_frame()
    hourly_prices['target'] = (hourly_prices['price'].shift(-1) < hourly_prices['price']).astype(int)

    # Engineer rolling window features from whale data
    if not whales_df.empty:
        whales_df['inflow_usd'] = whales_df.apply(lambda row: row['amount_usd'] if 'exchange' in row['to_owner_type'] else 0, axis=1)
        whales_df['outflow_usd'] = whales_df.apply(lambda row: row['amount_usd'] if 'exchange' in row['from_owner_type'] else 0, axis=1)
        
        hourly_whales = pd.DataFrame(index=hourly_prices.index)
        for window in [3, 6, 12, 24]:
            hourly_whales[f'inflow_sum_{window}h'] = whales_df['inflow_usd'].resample('h').sum().rolling(window=window).sum()
            hourly_whales[f'outflow_sum_{window}h'] = whales_df['outflow_usd'].resample('h').sum().rolling(window=window).sum()
            hourly_whales[f'transaction_count_{window}h'] = whales_df['amount_usd'].resample('h').count().rolling(window=window).sum()
    else:
        # Create empty dataframe with same index if no whale data
        hourly_whales = pd.DataFrame(index=hourly_prices.index)
        for window in [3, 6, 12, 24]:
            hourly_whales[f'inflow_sum_{window}h'] = 0
            hourly_whales[f'outflow_sum_{window}h'] = 0
            hourly_whales[f'transaction_count_{window}h'] = 0

    # Merge and create final feature set
    merged_df = hourly_prices.join(hourly_whales).fillna(0)
    merged_df.dropna(inplace=True) # Drop rows with NaN target

    if len(merged_df) < sequence_length * 2:
        log.warning(f"Not enough data to create sequences for {symbol}. Need at least {sequence_length * 2} data points.")
        return

    # --- 2. Data Scaling & Sequencing ---
    log.info(f"Phase 2: Scaling data and creating sequences for {symbol}...")
    scaler = StandardScaler()
    scaled_data = pd.DataFrame(scaler.fit_transform(merged_df), columns=merged_df.columns, index=merged_df.index)
    scaled_data['target'] = merged_df['target'] # Keep target unscaled

    X, y = create_sequences(scaled_data, sequence_length)
    X_train, X_test, y_train, y_test = train_test_split(X, y, test_size=0.2, random_state=42)

    # --- 3. LSTM Model Building & Training ---
    log.info(f"Phase 3: Building and training LSTM model for {symbol}...")
    model = Sequential([
        LSTM(50, return_sequences=True, input_shape=(X_train.shape[1], X_train.shape[2])),
        Dropout(0.2),
        LSTM(50),
        Dropout(0.2),
        Dense(1, activation='sigmoid')
    ])

    model.compile(optimizer='adam', loss='binary_crossentropy', metrics=['accuracy'])
    
    early_stopping = EarlyStopping(monitor='val_loss', patience=10, restore_best_weights=True)
    
    history = model.fit(X_train, y_train, epochs=100, batch_size=32, validation_split=0.2, callbacks=[early_stopping], verbose=1)

    # --- 4. Evaluation & Saving ---
    log.info(f"Phase 4: Evaluating and saving final model for {symbol}...")
    loss, accuracy = model.evaluate(X_test, y_test, verbose=0)
    
    report_path = f"output/lstm_model_performance_{symbol}.txt"
    with open(report_path, 'w') as f:
        f.write(f"LSTM Model Performance Report for {symbol}:\n")
        f.write(f"  - Test Accuracy: {accuracy:.4f}\n")
        f.write(f"  - Test Loss: {loss:.4f}\n")
    log.info(f"Saved performance report for {symbol} to {report_path}")

    model_path = f"output/lstm_model_{symbol}.h5"
    model.save(model_path)
    log.info(f"Saved trained LSTM model for {symbol} to {model_path}")

    log.info(f"--- ✅ LSTM Training for {symbol} Complete ---")

def main():
    """
    Main function to run the LSTM training for all symbols in the watch list.
    """
    log.info("--- 🚀 Starting LSTM Multi-Symbol Model Training Pipeline ---")
    watch_list = app_config.get('settings', {}).get('watch_list', ['BTC'])
    
    for symbol in watch_list:
        train_lstm_for_symbol(symbol)
        
    log.info("--- ✅ All Symbols Processed. LSTM Pipeline Complete. ---")

if __name__ == "__main__":
    main()
