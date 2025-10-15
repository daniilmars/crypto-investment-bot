# 🚀 Crypto Investment Alert Bot

A sophisticated, multi-source bot that analyzes on-chain activity and technical indicators to generate automated investment signals for cryptocurrencies.

---

## 📘 Overview

This bot is designed to identify potential crypto investment opportunities by systematically collecting and analyzing data from multiple key sources. It runs in a continuous cycle, fetching the latest data, evaluating it against a configurable strategy, and sending real-time alerts via Telegram for significant signals.

All collected data is stored in a local **SQLite database**, enabling historical analysis and strategy evaluation with a built-in **backtesting framework**.

---

## 🧠 Core Features

-   📊 **Multi-Source Data Collection:**
    -   **On-Chain Activity:** Whale Alert API for large transaction tracking.
    -   **Market Data:** Real-time prices from Binance.
-   💾 **Data Persistence:** All collected data is automatically saved to a local SQLite database for historical analysis.
-   🧮 **Technical Analysis Signal Engine:** A rule-based engine that combines on-chain flow with two key technical indicators for robust signals:
    -   **Simple Moving Average (SMA):** To identify the primary market trend.
    -   **Relative Strength Index (RSI):** To measure momentum and identify overbought/oversold conditions.
-   🧪 **Backtesting Framework:** A powerful simulation tool (`backtest.py`) that runs your strategy against historical data to objectively measure its performance (Profit/Loss, number of trades).
-   📲 **Telegram Notifications:** Instant alerts for BUY or SELL signals sent directly to your Telegram.
-   📝 **Structured Logging:** Professional logging for clear, timestamped monitoring of the bot's activity.

---

## 🏗 Architecture

```text
+--------------------------------+
|   Data Collectors (APIs)       |
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
| - technical_indicators.py      |
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

## 🚀 Deployment with Heroku

The recommended way to run this bot in production is by using Heroku, which can automate the deployment and run the bot on a 24/7 server.

### 1. Prerequisites

-   A free Heroku account.
-   The Heroku CLI installed on your local machine.
-   The project pushed to a GitHub repository.

### 2. Setup and Deployment Steps

1.  **Create a New Heroku App:**
    From your terminal, logged into the Heroku CLI, create a new application:
    ```bash
    heroku create your-bot-name
    ```

2.  **Provision the Postgres Database:**
    Add the Heroku Postgres add-on to your app. The free `hobby-dev` tier is sufficient to get started. This will automatically create the database and set the required `DATABASE_URL` environment variable for your application.
    ```bash
    heroku addons:create heroku-postgresql:hobby-dev -a your-bot-name
    ```

3.  **Configure Environment Variables:**
    Set the required API keys as environment variables in Heroku. This is the secure way to manage your secrets.
    ```bash
    heroku config:set WHALE_ALERT_API_KEY="YOUR_WHALE_ALERT_KEY" -a your-bot-name
    heroku config:set TELEGRAM_BOT_TOKEN="YOUR_TELEGRAM_TOKEN" -a your-bot-name
    heroku config:set TELEGRAM_CHAT_ID="YOUR_TELEGRAM_CHAT_ID" -a your-bot-name
    ```

4.  **Connect to GitHub and Deploy:**
    -   In the Heroku Dashboard for your app, go to the "Deploy" tab.
    -   Connect your GitHub account and select the repository for this project.
    -   Choose to "Enable Automatic Deploys" from your `main` branch.
    -   Manually trigger the first deploy by clicking "Deploy Branch".

    Heroku will now automatically build the `Dockerfile`, provision the `worker` process as defined in `heroku.yml`, and start the bot.

### 3. Managing the Bot

-   **To view logs:** `heroku logs --tail -a your-bot-name`
-   **To check if the worker is running:** `heroku ps -a your-bot-name`
-   The bot will automatically restart if it crashes.

---

## 🧮 Implemented Data Sources

| Category      | Source         | API                                                                |
| ------------- | -------------- | ------------------------------------------------------------------ |
| On-Chain      | Whale Alert    | [https://whale-alert.io](https://whale-alert.io)                   |
| Marktpreise   | Binance        | [https://binance-docs.github.io](https://binance-docs.github.io)   |

---

## 🧱 Next Steps & Extensions

-   🤖 Enhance the signal engine with more technical indicators (e.g., MACD, Bollinger Bands).
-   🕸 Build a web dashboard with Streamlit or Flask to visualize data and backtest results.
-   🧩 Implement auto-portfolio management via the Binance API.

---

## ⚠️ Disclaimer

This project is for educational and research purposes only. It is not financial advice. Trading cryptocurrencies involves significant risk.