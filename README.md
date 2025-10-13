# 🚀 Crypto Investment Alert Bot

A sophisticated, multi-source bot that analyzes market sentiment, on-chain activity, and price action to generate automated investment signals for cryptocurrencies.

---

## 📘 Overview

This bot is designed to identify potential crypto investment opportunities by systematically collecting and analyzing data from multiple key sources. It runs in a continuous cycle, fetching the latest data, evaluating it against a configurable strategy, and sending real-time alerts via Telegram for significant signals.

All collected data is stored in a local **SQLite database**, enabling historical analysis and strategy evaluation with a built-in **backtesting framework**.

---

## 🧠 Core Features

-   📊 **Multi-Source Data Collection:**
    -   **Market Sentiment:** Fear & Greed Index.
    -   **On-Chain Activity:** Whale Alert API for large transaction tracking.
    -   **Market Data:** Real-time prices from Binance.
-   💾 **Data Persistence:** All collected data is automatically saved to a local SQLite database for historical analysis.
-   🧮 **Comprehensive Signal Engine:** A multi-layered, rule-based engine that combines sentiment, on-chain flow, and price action (vs. moving average) to generate confirmed BUY/SELL signals.
-   🧪 **Backtesting Framework:** A powerful simulation tool (`backtest.py`) that runs your strategy against historical data to objectively measure its performance (Profit/Loss, number of trades).
-   📲 **Telegram Notifications:** Instant alerts for BUY or SELL signals sent directly to your Telegram.
-   📝 **Structured Logging:** Professional logging for clear, timestamped monitoring of the bot's activity.

---

## 🏗 Architecture

```text
+--------------------------------+
|   Data Collectors (APIs)       |
| - fear_and_greed.py            |
| - binance_data.py              |
| - whale_alert.py               |
+----------------+---------------+
                 |
                 v
+--------------------------------+
|   SQLite Database              |
| - crypto_data.db               |
| - (database.py)                |
+----------------+---------------+
                 |
                 v
+--------------------------------+
|   Analysis & Signal Engine     |
| - signal_engine.py             |
+----------------+---------------+
                 |
+----------------+--------------------------------+
|                |                                |
v                v                                v
+----------------+--+      +----------------+--+      +----------------+--+
| Notification      |      | Backtesting       |      | Live Execution    |
| - telegram_bot.py |      | - backtest.py     |      | - main.py         |
+-------------------+      +-------------------+      +-------------------+
```

---

## ⚙️ Setup

### Prerequisites

-   Python ≥ 3.9
-   Git
-   API Keys for:
    -   Whale Alert
    -   Telegram (create a bot via @BotFather)

### Installation

1.  **Clone the repository:**
    ```bash
    git clone https://github.com/<your-user>/crypto-alert-bot.git
    cd crypto-alert-bot
    ```

2.  **Install dependencies:**
    ```bash
    python3 -m pip install -r requirements.txt
    ```

3.  **Configure the bot:**
    -   Rename `config/settings.yaml.example` to `config/settings.yaml`.
    -   Open `config/settings.yaml` and add your API keys and Telegram details.

---

## 🚀 Usage

### Running the Live Bot

To start the bot in live mode, run `main.py`. It will execute a cycle every 15 minutes (configurable in `settings.yaml`).

```bash
python3 main.py
```

### Running the Backtester

To evaluate your strategy's performance on the data you've collected, run the backtesting script.

```bash
python3 src/analysis/backtest.py
```

The backtester will output the simulated Profit/Loss based on the logic in `signal_engine.py`.

---

## 🧮 Implemented Data Sources

| Category      | Source         | API                                                                |
| ------------- | -------------- | ------------------------------------------------------------------ |
| On-Chain      | Whale Alert    | [https://whale-alert.io](https://whale-alert.io)                   |
| Marktpreise   | Binance        | [https://binance-docs.github.io](https://binance-docs.github.io)   |
| Sentiment     | Alternative.me | [https://alternative.me/crypto/fear-and-greed-index](https://alternative.me/crypto/fear-and-greed-index) |

---

## 🧱 Next Steps & Extensions

-   🤖 Enhance the signal engine with more technical indicators (RSI, MACD).
-   🕸 Build a web dashboard with Streamlit or Flask to visualize data and backtest results.
-   🧩 Implement auto-portfolio management via the Binance API.

---

## ⚠️ Disclaimer

This project is for educational and research purposes only. It is not financial advice. Trading cryptocurrencies involves significant risk.