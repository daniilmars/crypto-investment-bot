import sys
import os
import pandas as pd
import numpy as np
import xgboost as xgb
from sklearn.model_selection import train_test_split, GridSearchCV
from sklearn.metrics import classification_report
from sklearn.preprocessing import StandardScaler
import joblib
import matplotlib.pyplot as plt
import seaborn as sns

# Add the project root to the Python path
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.database import get_db_connection
from src.logger import log
from src.config import app_config

def analyze_whale_data_for_symbol(symbol: str):
    """
    Performs a complete data analysis, feature engineering, model training,
    and evaluation pipeline for a single cryptocurrency symbol.
    """
    log.info(f"--- 📊 Starting Analysis for {symbol} ---")
    conn = get_db_connection()

    # --- 1. Data Loading & Preprocessing ---
    log.info(f"Phase 1: Loading and preparing data for {symbol}...")
    try:
        prices_df = pd.read_sql("SELECT * FROM market_prices WHERE symbol LIKE %(sym)s", conn, params={"sym": f"{symbol}%"}, parse_dates=['timestamp'])
        prices_df.set_index('timestamp', inplace=True)
        if prices_df.index.tz is None:
            prices_df.index = prices_df.index.tz_localize('UTC')
        else:
            prices_df.index = prices_df.index.tz_convert('UTC')

        whales_df = pd.read_sql("SELECT * FROM whale_transactions WHERE symbol = %(sym)s", conn, params={"sym": symbol.lower()}, parse_dates=['timestamp'])
        whales_df.set_index('timestamp', inplace=True)
        if whales_df.index.tz is None:
            whales_df.index = whales_df.index.tz_localize('UTC')
        else:
            whales_df.index = whales_df.index.tz_convert('UTC')

        sentiment_df = pd.read_sql("SELECT * FROM news_sentiment WHERE symbol = %(sym)s", conn, params={"sym": symbol.lower()}, parse_dates=['timestamp'])
        sentiment_df.set_index('timestamp', inplace=True)
        if sentiment_df.index.tz is None:
            sentiment_df.index = sentiment_df.index.tz_localize('UTC')
        else:
            sentiment_df.index = sentiment_df.index.tz_convert('UTC')
    except Exception as e:
        log.error(f"Failed to load data for {symbol}: {e}")
        return

    if prices_df.empty or whales_df.empty:
        log.warning(f"No market price or whale data for {symbol}. Skipping analysis.")
        return

    # --- 2. Feature Engineering ---
    log.info(f"Phase 2: Engineering features for {symbol}...")
    
    # Resample prices to hourly and create target variable
    hourly_prices = prices_df['price'].resample('h').last().to_frame()
    hourly_prices['price_change'] = hourly_prices['price'].diff()
    hourly_prices['target'] = (hourly_prices['price_change'].shift(-1) < 0).astype(int) # Predict if next hour's price will be down

    # Aggregate whale data
    whales_df['net_flow'] = whales_df.apply(lambda row: row['amount_usd'] if 'exchange' in row['to_owner_type'] else -row['amount_usd'], axis=1)
    hourly_whales = whales_df['net_flow'].resample('h').sum().to_frame()
    hourly_whales.rename(columns={'net_flow': 'net_exchange_flow_usd'}, inplace=True)

    # Merge datasets
    merged_df = hourly_prices.join(hourly_whales, how='left').join(sentiment_df, how='left').fillna(0)

    # Drop the symbol column from sentiment_df if it exists before creating lags
    if 'symbol' in merged_df.columns:
        merged_df.drop(columns=['symbol'], inplace=True)

    # Create lagged features
    for lag in range(1, 13):
        merged_df[f'price_lag_{lag}'] = merged_df['price'].shift(lag)
        merged_df[f'whale_flow_lag_{lag}'] = merged_df['net_exchange_flow_usd'].shift(lag)
        if 'avg_sentiment_score' in merged_df.columns:
            merged_df[f'sentiment_lag_{lag}'] = merged_df['avg_sentiment_score'].shift(lag)

    merged_df.dropna(inplace=True)

    if merged_df.empty:
        log.warning(f"No overlapping data after feature engineering for {symbol}. Skipping.")
        return

    # --- 3. Model Training & Tuning ---
    log.info(f"Phase 3: Training and tuning model for {symbol}...")
    X = merged_df.drop(['price', 'price_change', 'target'], axis=1)
    y = merged_df['target']

    if len(X) < 10 or y.nunique() < 2:
        log.warning(f"Not enough data or only one class present for {symbol} to train a model. Need at least 10 samples and 2 classes.")
        return

    X_train, X_test, y_train, y_test = train_test_split(X, y, test_size=0.2, random_state=42, stratify=y)

    scaler = StandardScaler()
    X_train = scaler.fit_transform(X_train)
    X_test = scaler.transform(X_test)

    param_grid = {
        'learning_rate': [0.01, 0.1],
        'max_depth': [3, 5],
        'n_estimators': [100, 200],
    }
    
    xgb_clf = xgb.XGBClassifier(objective='binary:logistic', eval_metric='logloss', use_label_encoder=False)
    grid_search = GridSearchCV(estimator=xgb_clf, param_grid=param_grid, cv=3, scoring='f1_weighted', verbose=1)
    grid_search.fit(X_train, y_train)

    log.info(f"Best parameters for {symbol}: {grid_search.best_params_}")
    best_model = grid_search.best_estimator_

    # --- 4. Evaluation & Saving ---
    log.info(f"Phase 4: Evaluating and saving final model for {symbol}...")
    y_pred = best_model.predict(X_test)
    report = classification_report(y_test, y_pred, target_names=['NOT_DOWN', 'DOWN'])
    
    report_path = f"output/model_performance_{symbol}.txt"
    with open(report_path, 'w') as f:
        f.write(f"Tuned XGBoost Model Performance Report for {symbol}:\n")
        f.write(report)
    log.info(f"Saved performance report for {symbol} to {report_path}")

    model_path = f"output/sell_signal_model_{symbol}.joblib"
    joblib.dump(best_model, model_path)
    log.info(f"Saved trained model for {symbol} to {model_path}")

    # Feature Importance Plot
    feature_importances = pd.DataFrame(best_model.feature_importances_, index=X.columns, columns=['importance'])
    feature_importances.sort_values('importance', ascending=False, inplace=True)
    
    plt.figure(figsize=(12, 8))
    sns.barplot(x=feature_importances.importance, y=feature_importances.index)
    plt.title(f'Feature Importance for {symbol} Sell Signal Model')
    plt.xlabel('Importance')
    plt.ylabel('Feature')
    plt.tight_layout()
    plot_path = f"output/feature_importance_{symbol}.png"
    plt.savefig(plot_path)
    plt.close()
    log.info(f"Saved feature importance plot for {symbol} to {plot_path}")

    log.info(f"--- ✅ Analysis for {symbol} Complete ---")

def main():
    """
    Main function to run the analysis for all symbols in the watch list.
    """
    log.info("--- 🚀 Starting Multi-Symbol Model Training Pipeline ---")
    watch_list = app_config.get('settings', {}).get('watch_list', ['BTC'])
    
    for symbol in watch_list:
        analyze_whale_data_for_symbol(symbol)
        
    log.info("--- ✅ All Symbols Processed. Pipeline Complete. ---")

if __name__ == "__main__":
    main()
