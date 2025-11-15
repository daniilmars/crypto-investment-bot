import sys
import os
import joblib
import pandas as pd
import matplotlib.pyplot as plt
import seaborn as sns

# Add the project root to the Python path
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.logger import log

def validate_model():
    """
    Loads the trained sell-signal model and analyzes its feature importances to validate
    which data points are most influential.
    """
    log.info("--- 🔎 Starting Model Validation: Feature Importance Analysis ---")

    # --- 1. Load the Model ---
    model_path = 'output/sell_signal_model_advanced.joblib'
    if not os.path.exists(model_path):
        log.error(f"Model file not found at {model_path}. Please run the training script first.")
        return

    log.info(f"Loading model from {model_path}...")
    model = joblib.load(model_path)

    # --- 2. Extract Feature Importances ---
    # The training script needs to be run first to get the feature names
    # For now, we assume the features are available from the training script's context
    # A more robust implementation would save feature names with the model
    try:
        importances = model.feature_importances_
        # We need to get the feature names from the training data
        # This is a bit of a shortcut; ideally, the training script would save the feature list
        from scripts.whale_price_correlation import analyze_whale_data
        # This is a placeholder to get the feature names.
        # In a real-world scenario, you'd save the feature list from the training script.
        # To avoid re-running the whole analysis, we'll just reconstruct the feature names here.
        
        # Reconstruct feature names (this is fragile, but avoids re-running the full training)
        base_cols = ['inflow_volume_usd', 'outflow_volume_usd', 'net_exchange_flow_usd', 'transaction_count', 'num_unique_inflow_sources']
        for i in range(1, 11):
            base_cols.append(f'whale_{i}_outflow')
            
        feature_names = []
        for lag in [1, 2, 3, 6, 12]:
            for col in base_cols:
                feature_names.append(f'{col}_lag_{lag}')

        if len(importances) != len(feature_names):
             log.error("Mismatch between feature importance array and reconstructed feature names.")
             # Fallback for mismatched feature names
             feature_names = [f'feature_{i}' for i in range(len(importances))]

    except Exception as e:
        log.error(f"Could not extract feature importances. Error: {e}")
        return

    feature_importance_df = pd.DataFrame({
        'feature': feature_names,
        'importance': importances
    }).sort_values('importance', ascending=False)

    log.info("Top 10 Most Important Features:")
    print(feature_importance_df.head(10))

    # --- 3. Visualize Feature Importances ---
    log.info("Generating feature importance plot...")
    plt.figure(figsize=(12, 10))
    sns.barplot(x='importance', y='feature', data=feature_importance_df.head(15), palette='viridis')
    plt.title('Top 15 Feature Importances for Sell Signal Model')
    plt.xlabel('Importance Score')
    plt.ylabel('Feature')
    plt.tight_layout()

    output_path = 'output/feature_importance.png'
    plt.savefig(output_path)
    log.info(f"Saved feature importance plot to {output_path}")

    log.info("\n--- ✅ Model Validation Complete ---")

if __name__ == "__main__":
    validate_model()
