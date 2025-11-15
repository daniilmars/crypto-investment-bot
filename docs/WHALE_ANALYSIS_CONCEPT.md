# Concept: Whale Transaction & Price Correlation Analysis

This document outlines the concept and methodology for a detailed analysis of whale transaction data to identify patterns that correlate with cryptocurrency price movements.

## 1. Objective
The primary goal of this analysis is to move beyond simple, rule-based signal generation and to statistically validate the relationship between whale activity and price changes.

We aim to answer questions like:
- Do large inflows to exchanges consistently precede a price drop?
- Does a high volume of wallet-to-wallet transfers signal an upcoming increase in volatility?
- What is the time lag between a significant on-chain event and its impact on the market price?
- Do the actions of specific, individual whales have a more predictive power than the aggregate market flow?
- Can we identify "smart money" or coordinated activity through network analysis?

The insights from this analysis will be used to refine and improve the logic in `src/analysis/signal_engine.py`.

## 2. Methodology

The analysis will be conducted in a new, dedicated Python script and will be broken down into five phases:

### Phase 1: Data Preparation & Advanced Feature Engineering

This is the most critical phase. We will create a unified, hourly dataset that combines both price and whale data, and enrich it with advanced, modern features.

1.  **Load Data:** Load the `market_prices` and `whale_transactions` tables from the SQLite database into pandas DataFrames.
2.  **Resample Price Data:** Resample the price data into fixed **1-hour** intervals, calculating the open, high, low, and close (OHLC) for each hour, as well as the percentage change in price (`price_change`).
3.  **Aggregate Whale Data:** Group the raw whale transactions into the same **1-hour** buckets.
4.  **Engineer Aggregate Whale Features:** For each 1-hour bucket, we will calculate a rich set of aggregate features, including:
    -   `net_exchange_flow_usd`: The net amount (in USD) of all monitored cryptocurrencies moving to/from exchanges. (Inflow - Outflow)
    -   `inflow_volume_usd`: Total USD value of transactions moving *to* exchanges.
    -   `outflow_volume_usd`: Total USD value of transactions moving *from* exchanges.
    -   `wallet_to_wallet_volume_usd`: Total USD value of transactions between unknown wallets.
    -   `transaction_count`: The total number of whale transactions.
    -   `avg_transaction_size_usd`: The average USD value of a single transaction.
5.  **Engineer Individual & Graph-Based Features:**
    -   **Top Whale Identification:** We will identify the top 20 most active whale wallets (by transaction volume) in the dataset.
    -   **Individual Whale Features:** For each of these top whales, we will create specific features, such as `whale_1_net_flow`, `whale_2_inflow_volume`, etc.
    -   **Graph-Based Features:** We will model the transactions as a network to create features like `num_unique_source_wallets` for exchange inflows, to differentiate between a single large transfer and a distributed inflow.
    -   **"Smart Money" Flow:** We will create a specific feature to track the net flow of a pre-identified list of highly profitable or influential wallets.
    -   **Transaction Categorization:** We will add features to categorize transactions (e.g., `cex_inflow`, `defi_interaction`) to better understand the *intent* behind the money flow.

### Phase 2: Correlation Analysis (Statistical)

Once we have the unified hourly dataset, we will perform a statistical analysis to quantify the relationships between all our engineered features and price changes.

1.  **Calculate Pearson Correlation:** We will calculate the standard Pearson correlation coefficient between each whale feature and the `price_change` in the same hour.
2.  **Calculate Lagged Correlation:** We will shift the `price_change` data backward in time to see if whale activity in one hour can predict the price change in the *next* hour. We will test multiple lags (e.g., 1 hour, 2 hours, 4 hours, 8 hours).

### Phase 3: Visualization

To make the results easy to interpret, we will generate a series of visualizations.

1.  **Time Series Plots:** We will create plots that overlay the cryptocurrency price with key whale activity metrics to visually inspect the relationships.
2.  **Correlation Heatmap:** We will generate a heatmap of the correlation matrix to provide a clear, at-a-glance view of which features are the strongest predictors.
3.  **Scatter Plots:** For the most highly correlated features, we will create scatter plots to visualize the strength and direction of the relationship.

### Phase 4: Predictive Modeling (Machine Learning)

After identifying the most promising features, we will build and train a machine learning model to predict future price movements.

1.  **Model Selection:** We will start with a robust, tree-based model like **XGBoost**.
2.  **Feature Set:** We will use the full set of engineered features, including the aggregate, individual, and graph-based metrics.
3.  **Target Variable:** We will frame the problem as a ternary classification task (`UP`, `DOWN`, or `FLAT`).
4.  **Training & Evaluation:** We will split the data into a training and testing set to rigorously evaluate the model's performance.

### Phase 5: Cross-Modal Sentiment Analysis (Future Scope)

As a future enhancement, we will integrate off-chain data to build a more context-aware model.

1.  **New Data Source:** We will integrate a new data source for social media sentiment (e.g., a News API or a dedicated sentiment analysis service).
2.  **Combined Features:** We will create features that combine on-chain and off-chain metrics (e.g., `net_exchange_flow * positive_sentiment_score`).
3.  **Model Refinement:** We will retrain the XGBoost model with these new, cross-modal features to see if it improves predictive accuracy.

## 3. Tools & Implementation

-   **Script:** A new script, `scripts/whale_price_correlation.py`, will be created.
-   **Libraries:**
    -   `pandas`, `scipy`, `matplotlib`, `seaborn`, `scikit-learn`, `xgboost`
    -   `networkx` (for graph-based feature engineering).
-   **Output:** The script will save all plots, reports, and trained models to a new `output/` directory.

## 4. Expected Outcome

The final output will be a comprehensive, data-driven report and a trained machine learning model that can be integrated into the live bot to generate more accurate, probabilistic trading signals.
