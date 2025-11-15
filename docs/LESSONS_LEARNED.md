# Lessons Learned: ML Model Iteration

This document summarizes the key insights gained from the iterative development and tuning of the whale transaction predictive model.

## 1. Initial Model: The "Flat" Bias

Our first attempt involved a multi-class XGBoost classifier aimed at predicting `UP`, `DOWN`, and `FLAT` price movements.

-   **Observation:** The model achieved a respectable accuracy of ~43% but had a recall of `1.00` for the `FLAT` class and `0.00` for both `UP` and `DOWN`.
-   **Lesson:** Standard machine learning models are not plug-and-play for financial time-series data. The natural class imbalance (where significant price moves are much rarer than small, insignificant ones) will cause a default model to simply learn to predict the most common outcome. This is statistically safe but provides no trading value.

## 2. Weighted Model: Acknowledging Rarity

To address the class imbalance, we introduced class weights to penalize the model more heavily for misclassifying the rare `UP` and `DOWN` events.

-   **Observation:** The model's behavior completely inverted. It began to predict the `DOWN` class with a recall of `1.00`, while ignoring the `UP` and `FLAT` classes. The precision for the `DOWN` class was ~32%.
-   **Lesson:** Class weighting is a highly effective technique for forcing a model to learn the patterns of minority classes. The result also gave us our first major insight into the data itself: the features we were using (aggregate exchange flows, transaction counts) appear to have a strong **directional bias**. They are much more indicative of bearish (selling) pressure than bullish (buying) pressure.

## 3. Tuned Binary Model: Hitting the Feature Wall

Based on the previous insight, we specialized the model into a binary classifier focused solely on predicting `DOWN` vs. `NOT_DOWN`. We then used `GridSearchCV` to find the absolute best hyperparameters for this task.

-   **Observation:** The hyperparameter tuning process converged on a model that, once again, had a recall of `0.00` for the `DOWN` class. It learned that the most accurate strategy was to always predict `NOT_DOWN`.
-   **Lesson:** This was the most crucial lesson. The `GridSearchCV` process effectively proved that we have exhausted the predictive power of our current, simple feature set. The model, even with optimal tuning, determined that the signals from our aggregate features were not strong enough to confidently predict a downward move. The problem is not the model's configuration; it is the quality and depth of the input data.

## Conclusion & Mandate for the Next Step

This iterative process has been highly successful. We have:
1.  Validated our end-to-end data pipeline and modeling framework.
2.  Gained critical insights into the nature of our dataset.
3.  Received a clear, data-driven mandate to proceed with more advanced feature engineering.

To improve the model's performance, we must move beyond simple, aggregate metrics and create a richer, more sophisticated set of features that can capture the complex dynamics of the on-chain environment. This includes graph-based features, individual whale tracking, and "smart money" analysis.
