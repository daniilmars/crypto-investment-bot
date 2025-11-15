# Concept: Sentiment Analysis for Predictive Modeling

This document outlines the concept and methodology for integrating sentiment analysis into our trading bot's predictive modeling pipeline.

## 1. Objective

The primary goal is to enrich our dataset with off-chain, sentiment-based features. Our previous analysis concluded that our on-chain metrics, while useful, lack the context to be reliably predictive on their own. By quantifying market sentiment (e.g., fear, uncertainty, hype), we aim to provide this missing context and significantly improve the performance of our machine learning model.

We seek to answer questions like:
- Does a sharp drop in market sentiment precede a price decline, even if on-chain flows are neutral?
- Can a surge in news volume and positive sentiment amplify a bullish on-chain signal?
- Can we use sentiment to filter out noisy or misleading on-chain signals?

## 2. Methodology

The process will be broken down into three distinct phases:

### Phase 1: Data Acquisition

We need a reliable and consistent source of text data.

1.  **Data Source:** After a thorough evaluation, we have selected to use a **General News API**. Specifically, we will use **NewsAPI.org** due to its generous free tier, high-quality global news sources, and excellent documentation.
2.  **Collection Script:** A new, standalone Python script, `scripts/collect_news_sentiment.py`, will be created.
3.  **API Key Management:** The API key will be stored securely in the `config/settings.yaml` file and will not be hardcoded in the script.
4.  **Execution:** This script will be designed to be run on a schedule (e.g., hourly) to continuously update our dataset with the latest news.

### Phase 2: Sentiment Quantification & Feature Engineering

Raw text will be converted into numerical features that our model can understand.

1.  **Sentiment Analysis Tool:** We will use the **VADER (Valence Aware Dictionary and sEntiment Reasoner)** library in Python. VADER is chosen because it is:
    -   **Pre-trained:** No need for a complex model training pipeline.
    -   **Fast & Efficient:** Ideal for processing a high volume of headlines.
    -   **Tuned for Social Media & News:** It is specifically designed to understand the sentiment in short-form text, including the use of capitalization and exclamation marks.
2.  **Aggregation:** The sentiment score of each individual headline will be aggregated into the same **1-hour time buckets** used for our price and whale data.
3.  **Feature Creation:** For each 1-hour bucket, we will engineer a set of sentiment features, including:
    -   `avg_sentiment_score`: The average compound sentiment score of all articles in that hour.
    -   `news_volume`: The total count of articles published in that hour.
    -   `sentiment_volatility`: The standard deviation of sentiment scores within the hour. A high value could indicate a controversial or highly debated topic.
    -   `positive_buzz_ratio`: The percentage of articles with a positive sentiment score.
    -   `negative_buzz_ratio`: The percentage of articles with a negative sentiment score.

### Phase 3: Data Storage

The engineered sentiment data will be persisted in our database.

1.  **New Table:** A new table named `news_sentiment` will be created in the `data/crypto_data.db` SQLite database.
2.  **Schema:** The table will include columns for `timestamp`, `avg_sentiment_score`, `news_volume`, etc.

## 3. Integration Strategy

The new sentiment data will be seamlessly integrated into our existing machine learning pipeline.

1.  **Data Loading:** The main analysis script, `scripts/whale_price_correlation.py`, will be modified to load data from the new `news_sentiment` table.
2.  **Unified DataFrame:** The sentiment data will be joined with the existing price and whale transaction data on the hourly `timestamp`. This will create a single, powerful DataFrame containing on-chain, off-chain, and market data.
3.  **Model Training:** This unified dataset will be used as the input for training and tuning our XGBoost "sell-signal" model. The existing `GridSearchCV` process will automatically evaluate the predictive power of the new sentiment features.

## 4. Expected Outcome

We hypothesize that the addition of sentiment features will be the catalyst for a significant improvement in model performance. We expect to see:

-   A non-zero **`f1-score`** for the `DOWN` class in our classification report, indicating that the model can now reliably identify sell signals.
-   Sentiment-related features (e.g., `avg_sentiment_score_lag_1`, `negative_buzz_ratio_lag_2`) appearing in the **Top 10 of the Feature Importance plot**.
-   The creation of a truly multi-modal predictive model that can be confidently integrated into the live trading bot.
