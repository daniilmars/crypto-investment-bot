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

The project is divided into two key architectural components: the application logic and the deployment pipeline.

### Application Architecture

The bot's core logic follows a linear data flow, from collection to notification. The database is designed to be PostgreSQL in production for reliability and SQLite for ease of local development.

```text
+--------------------------------+
|   Data Collectors (APIs)       |
| - binance_data.py              |
| - whale_alert.py               |
+----------------+---------------+
                 |
                 v
+--------------------------------+
|   Database (database.py)       |
|  - PostgreSQL (Production)     |
|  - SQLite (Local Development)  |
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
| - telegram_bot.py |      | - backtest.py     |      | - main.py (worker)|
+-------------------+      +-------------------+      +-------------------+
```

### Deployment Architecture

The deployment is fully automated via a CI/CD pipeline using GitHub Actions. Every push to the `master` branch triggers a workflow that tests the code and, if successful, deploys the application as a Docker container to Heroku.

```text
+------------------+      +--------------------+      +----------------------+
| Developer        |----->| GitHub Repository  |----->| GitHub Actions       |
| (git push)       |      | (master branch)    |      | (CI/CD Workflow)     |
+------------------+      +--------------------+      +----------+-----------+
                                                                 |
         +-------------------------------------------------------+
         |
         v
+----------+-----------+      +--------------------+      +----------------------+
| Run Tests          |----->| Build Docker Image |----->| Deploy to Heroku     |
| (pytest)           |      |                    |      | (Container Stack)    |
+--------------------+      +--------------------+      +----------------------+
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

## 🚀 Deployment with Heroku & GitHub Actions

This project is configured for automated, professional deployment to Heroku via a GitHub Actions CI/CD pipeline. The workflow automatically tests and deploys the application whenever new code is pushed to the `master` branch.

### 1. Prerequisites

-   A free Heroku account (verified with a payment method).
-   The project pushed to a GitHub repository.
-   The GitHub CLI (`gh`) installed on your local machine.

### 2. One-Time Setup

1.  **Create the Heroku App:**
    From your terminal, create the Heroku application. This also sets the stack to `container`, which is required for Docker-based deployments.
    ```bash
    heroku create your-app-name --stack=container
    ```

2.  **Provision the Postgres Database:**
    Add the free Heroku Postgres add-on. This automatically sets the `DATABASE_URL` config var on your Heroku app.
    ```bash
    heroku addons:create heroku-postgresql:hobby-dev -a your-app-name
    ```

3.  **Configure GitHub Secrets:**
    The CI/CD workflow requires secrets to be set in your GitHub repository. These are used to deploy the app and to configure the app's environment variables on Heroku.
    ```bash
    # The API key for your Heroku account
    gh secret set HEROKU_API_KEY

    # The API key for the Whale Alert service
    gh secret set WHALE_ALERT_API_KEY

    # Your Telegram Bot's token
    gh secret set TELEGRAM_BOT_TOKEN

    # The Chat ID for your Telegram channel or user
    gh secret set TELEGRAM_CHAT_ID
    ```

### 3. Automated Deployment

Once the setup is complete, the process is fully automated:

1.  **Push to GitHub:** Commit and push your changes to the `master` branch.
    ```bash
    git push origin master
    ```
2.  **CI/CD Pipeline:** The push automatically triggers the GitHub Actions workflow defined in `.github/workflows/deploy.yml`.
    -   The workflow installs all dependencies.
    -   It runs the full `pytest` suite to ensure code quality.
    -   If tests pass, it securely sets the API keys as config vars on your Heroku app.
    -   It builds the Docker image and deploys it to Heroku.
    -   Finally, it scales up the `worker` dyno to 1, starting the bot.

### 4. Managing the Bot

-   **To view logs:**
    ```bash
    heroku logs --tail -a your-app-name
    ```
-   **To check if the worker is running:**
    ```bash
    heroku ps -a your-app-name
    ```

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