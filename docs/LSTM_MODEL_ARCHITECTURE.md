# LSTM Model Architecture for Cryptocurrency Price Prediction

This document outlines the architecture and data processing pipeline for a new, LSTM-based model for predicting cryptocurrency price movements.

## 1. Problem Statement

Our previous, XGBoost-based models have been hampered by extreme data sparsity. This is due to the asynchronous nature of our data sources (prices, whale transactions, and news sentiment).

This new approach will use a Long Short-Term Memory (LSTM) network, a type of Recurrent Neural Network (RNN) that is purpose-built for time-series data. This will allow us to handle the asynchronous data in a more robust and sophisticated way.

## 2. Model Architecture

The model will be built using the TensorFlow/Keras library. The architecture will be as follows:

1.  **Input Layer:** This will accept our "sequences" of time-series data. Each sequence will represent a fixed-length window of time (e.g., 24 hours) and will contain all of our engineered features for that period.

2.  **LSTM Layer:** This is the core of the model. It will process the sequences and learn the temporal patterns in the data.

3.  **Dropout Layer:** This will be used to prevent overfitting, which is a common problem in complex models.

4.  **Dense Output Layer:** A final, fully-connected layer with a `sigmoid` activation function. This will output a single value between 0 and 1, representing the probability of our target event (e.g., "price will go down in the next hour").

## 3. Data Preparation: Sequence Generation

This is the most critical part of the new pipeline. We will transform our sparse, event-based data into a dense, continuous set of sequences.

1.  **Load Data:** For a given currency, we will load the market prices and whale transaction data.

2.  **Rolling Window Aggregations:** For each hourly price point, we will create a rich set of features that summarize the events of the preceding `N` hours (e.g., 24 hours). This will include features like:
    *   `sum_of_whale_inflows_last_6_hours`
    *   `max_whale_outflow_last_3_hours`
    *   `number_of_whale_transactions_last_12_hours`
    *   `price_volatility_last_24_hours`

3.  **Sequence Creation:** This process will result in a dense DataFrame where each row represents an hour and contains a complete set of features. We will then use a sliding window to create our sequences. For example, with a sequence length of 24, the first sequence would be the feature sets for hours 1-24, the second would be for hours 2-25, and so on.

## 4. Training & Evaluation

1.  **Model Compilation:** The model will be compiled with the `adam` optimizer and `binary_crossentropy` loss function.

2.  **Training:** The model will be trained on the generated sequences.

3.  **Evaluation:** We will evaluate the model's performance on a held-out test set and generate a classification report.

4.  **Saving:** The trained model (e.g., `lstm_model_BTC.h5`) and its performance report will be saved to the `output/` directory.

## 5. Implementation Plan

1.  **Create a new script:** `scripts/train_lstm_model.py`.
2.  **Initial Focus:** The initial implementation will focus on just the **price and whale alert data**. This will allow us to build and validate the core pipeline without the added complexity of the news sentiment data.
3.  **Future Expansion:** Once the core pipeline is working, we can easily incorporate the news sentiment data by adding it to the "Rolling Window Aggregations" step.
