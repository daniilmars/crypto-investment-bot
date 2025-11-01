# 🚀 Crypto Investment Alert Bot

A sophisticated, multi-source bot that analyzes on-chain activity and technical indicators to generate automated investment signals for cryptocurrencies.

---

## 📘 Overview

This bot is designed to identify potential crypto investment opportunities by systematically collecting and analyzing data from multiple key sources. It runs in a continuous cycle, fetching the latest data, evaluating it against a configurable strategy, and, in **paper trading mode**, simulates trades and sends real-time alerts via Telegram for significant signals.

All collected data, along with simulated trades and generated signals, is stored in a local **SQLite database**, enabling historical analysis and strategy evaluation with a built-in **backtesting framework**.

---

## 🧠 Core Features

-   📊 **Multi-Source Data Collection:**
    -   **On-Chain Activity:** Whale Alert API for large transaction tracking, including expanded stablecoin inflow analysis.
    -   **Market Data:** Real-time prices from Binance for an expanded `watch_list` of cryptocurrencies.
-   💾 **Data Persistence:** All collected data, generated signals, and simulated trades are automatically saved to a local SQLite database for historical analysis.
-   🧮 **Technical Analysis Signal Engine:** A configurable, rule-based engine that combines on-chain flow with two key technical indicators (SMA and RSI) for robust, symbol-specific signals.
-   🧪 **Paper Trading Framework:** A powerful simulation mode that executes trades against live market data without risking real capital, recording all trades to the database.
-   📲 **Telegram Notifications:** Instant alerts for BUY or SELL signals, plus regular, automated performance reports sent directly to your Telegram.
-   📝 **Structured Logging:** Professional logging for clear, timestamped monitoring of the bot's activity.
-   🤖 **AI-Powered Status Reports:** An interactive `/status` command in Telegram that uses the Gemini API to provide AI-generated summaries of market activity and bot health, including the last generated signal.

---

## 🏗 Architecture

The project is divided into two key architectural components: the application logic and the deployment pipeline.

### Application Architecture

The bot's core logic follows a linear data flow, from collection to notification. The database is designed to be PostgreSQL in production for reliability and SQLite for ease of local development, now also storing generated signals and simulated trades.

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
|  - Trades Table                |
|  - Signals Table               |
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
+-------------------+      +-------------------+      | - binance_trader.py |
                                                       +-------------------+
```

---

## ⚙️ Setup

### Prerequisites

-   Python ≥ 3.9
-   Git
-   API Keys for:
    -   Whale Alert
    -   Telegram (create a bot via @BotFather)
    -   Gemini (for AI summaries)

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
    -   Open `config/settings.yaml` and add your API keys and Telegram details. Also, review and adjust the new settings for:
        -   `watch_list`: Expanded list of cryptocurrencies to monitor.
        -   `stablecoins_to_monitor`: Expanded list of stablecoins for inflow analysis.
        -   `sma_period`, `rsi_overbought_threshold`, `rsi_oversold_threshold`: Configurable technical indicator parameters.
        -   `paper_trading`, `paper_trading_initial_capital`: Enable/disable paper trading and set initial capital.
        -   `trade_risk_percentage`, `stop_loss_percentage`, `take_profit_percentage`, `max_concurrent_positions`: Essential risk management parameters.
        -   `regular_status_update`: Configure automated performance reports to Telegram.

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

### Interacting with the Bot

Once the bot is running, you can interact with it directly in your configured Telegram chat:

-   `/start`: Initializes the bot and confirms it's running. Also provides a brief overview of available commands.
-   `/status`: The bot will perform a health check and use the Gemini API to generate a detailed summary of market activity and bot health, including the last generated signal.
-   `/db_stats`: Provides a quick overview of the number of entries in the `whale_transactions`, `market_prices`, `signals`, and `trades` tables.

Additionally, if configured, the bot will send **regular, automated performance reports** to your Telegram chat at a set interval (e.g., hourly), summarizing paper trading activity and PnL.

### Paper Trading Configuration

To enable or disable paper trading, and to configure its parameters, edit the `settings.yaml` file:

```yaml
settings:
  # ... other settings ...

  paper_trading: true # Set to false to disable paper trading (NOT RECOMMENDED FOR LIVE TRADING YET)
  paper_trading_initial_capital: 10000.0 # Starting capital for simulation

  # Risk Management Settings
  trade_risk_percentage: 0.01 # e.g., 1% of total capital per trade
  stop_loss_percentage: 0.02  # e.g., 2% below entry price
  take_profit_percentage: 0.05 # e.g., 5% above entry price
  max_concurrent_positions: 3 # Limit open trades

  # Regular Status Update Settings
  regular_status_update:
    enabled: true
    interval_hours: 1 # Send report every 1 hour
```

**Important:** Always start with `paper_trading: true` and thoroughly test your strategy before considering live trading.

---

## 🚀 Deployment with Google Cloud Run & GitHub Actions

This project is also configured for automated deployment to Google Cloud Run, a serverless platform that is highly scalable and cost-effective.

### 1. Prerequisites

-   A Google Cloud Platform (GCP) account with billing enabled.
-   The project pushed to a GitHub repository.
-   The Google Cloud CLI (`gcloud`) installed on your local machine.

### 2. One-Time Google Cloud Setup (via gcloud CLI)

This guide provides all the necessary terminal commands to provision your Google Cloud environment correctly.

**Step 1: Authenticate and Select Your Project**

First, log in to the `gcloud` CLI and identify your Project ID.

```bash
# Authenticate with your Google account
gcloud auth login

# List all your available projects to find the correct PROJECT_ID
gcloud projects list

# Set the gcloud CLI to use your chosen project
gcloud config set project [YOUR_PROJECT_ID]
```

**Step 2: Enable Required APIs**

Enable all the necessary services for the deployment.

```bash
gcloud services enable \
  run.googleapis.com \
  artifactregistry.googleapis.com \
  cloudbuild.googleapis.com \
  sqladmin.googleapis.com \
  sql-component.googleapis.com
```

**Step 3: Create Artifact Registry Repository**

Create the repository in Artifact Registry where the bot's Docker images will be stored.

```bash
gcloud artifacts repositories create crypto-bot \
    --repository-format=docker \
    --location=us-central1 \
    --description="Docker repository for crypto bot"
```

**Step 4: Create the Service Account**

Create a dedicated service account that the GitHub Actions workflow will use to deploy the application.

```bash
# Choose a name for your service account
export SERVICE_ACCOUNT_NAME=crypto-bot-deployer

# Create the service account
gcloud iam service-accounts create ${SERVICE_ACCOUNT_NAME} \
    --display-name "Crypto Bot Deployer"
```

**Step 5: Grant Permissions to the Service Account**

Assign the necessary roles to the service account so it has permission to manage Cloud Run, Artifact Registry, Cloud Build, and Cloud SQL.

```bash
# Get your full Project ID
export PROJECT_ID=$(gcloud config get-value project)

# Grant the Cloud Run Admin role
gcloud projects add-iam-policy-binding ${PROJECT_ID} \
    --member="serviceAccount:${SERVICE_ACCOUNT_NAME}@${PROJECT_ID}.iam.gserviceaccount.com" \
    --role="roles/run.admin"

# Grant the Artifact Registry Admin role
gcloud projects add-iam-policy-binding ${PROJECT_ID} \
    --member="serviceAccount:${SERVICE_ACCOUNT_NAME}@${PROJECT_ID}.iam.gserviceaccount.com" \
    --role="roles/artifactregistry.admin"

# Grant the Cloud Build Editor role
gcloud projects add-iam-policy-binding ${PROJECT_ID} \
    --member="serviceAccount:${SERVICE_ACCOUNT_NAME}@${PROJECT_ID}.iam.gserviceaccount.com" \
    --role="roles/cloudbuild.builds.editor"

# Grant the Cloud SQL Client role
gcloud projects add-iam-policy-binding ${PROJECT_ID} \
    --member="serviceAccount:${SERVICE_ACCOUNT_NAME}@${PROJECT_ID}.iam.gserviceaccount.com" \
    --role="roles/cloudsql.client"

# Grant the Storage Admin role (used by Cloud Build)
gcloud projects add-iam-policy-binding ${PROJECT_ID} \
    --member="serviceAccount:${SERVICE_ACCOUNT_NAME}@${PROJECT_ID}.iam.gserviceaccount.com" \
    --role="roles/storage.admin"

# Grant the Service Account User role to allow impersonating the Cloud Build SA
gcloud projects add-iam-policy-binding ${PROJECT_ID} \
    --member="serviceAccount:${SERVICE_ACCOUNT_NAME}@${PROJECT_ID}.iam.gserviceaccount.com" \
    --role="roles/iam.serviceAccountUser"

# Grant the Project Viewer role to allow streaming build logs
gcloud projects add-iam-policy-binding ${PROJECT_ID} \
    --member="serviceAccount:${SERVICE_ACCOUNT_NAME}@${PROJECT_ID}.iam.gserviceaccount.com" \
    --role="roles/viewer"
```

**Step 6: Create and Download the Service Account Key**

Generate a JSON key file that will be used to authenticate from GitHub Actions.

```bash
gcloud iam service-accounts keys create key.json \
    --iam-account="${SERVICE_ACCOUNT_NAME}@${PROJECT_ID}.iam.gserviceaccount.com"
```
**Important:** The `key.json` file will be created in your current directory. You will copy the entire contents of this file for a GitHub secret in the next section.

**Step 7: Create the PostgreSQL Database**

Create a Cloud SQL for PostgreSQL instance and a database for the bot.

```bash
# Choose a name for your database instance
export INSTANCE_NAME=crypto-bot-db

# Choose a strong password and save it securely for the next steps
export ROOT_PASSWORD="[CHOOSE_A_STRONG_PASSWORD]"

# Create the Cloud SQL instance (this can take several minutes)
gcloud sql instances create ${INSTANCE_NAME} \
    --database-version=POSTGRES_14 \
    --tier=db-g1-small \
    --region=us-central1 \
    --root-password="${ROOT_PASSWORD}"

# Create the database within the instance
gcloud sql databases create crypto_data --instance=${INSTANCE_NAME}
```

**Step 8: Get Database Connection Info**

Retrieve the connection details for your new database. You will need the **Public IP Address** for local testing and the **Instance Connection Name** for the production deployment on Cloud Run.

```bash
# Get the public IP address for local/external connections
export DB_IP=$(gcloud sql instances describe ${INSTANCE_NAME} --format="value(ipAddresses.ipAddress)")

# Get the instance connection name for the Cloud Run service
export INSTANCE_CONNECTION_NAME=$(gcloud sql instances describe ${INSTANCE_NAME} --format="value(connectionName)")

# Display the values to use in your GitHub secrets
echo "Your DATABASE_URL is: postgresql://postgres:${ROOT_PASSWORD}@${DB_IP}/crypto_data"
echo "Your INSTANCE_CONNECTION_NAME is: ${INSTANCE_CONNECTION_NAME}"
```

**Step 9: Configure Cloud NAT for Internet Access**

By default, Cloud Run services do not have outbound internet access. To allow the bot to connect to external APIs like Telegram and Whale Alert, you must set up a Cloud NAT gateway.

```bash
# Create a Cloud Router
gcloud compute routers create crypto-bot-router \
    --network default \
    --region=us-central1

# Create the NAT gateway
gcloud compute routers nats create crypto-bot-nat \
    --router=crypto-bot-router \
    --region=us-central1 \
    --auto-allocate-nat-external-ips \
    --nat-all-subnet-ip-ranges
```

### 3. GitHub Repository Setup

1.  **Add Secrets to GitHub:**
    Go to your GitHub repository's "Settings" > "Secrets and variables" > "Actions" and add the following secrets:
    -   `GCP_PROJECT_ID`: Your Google Cloud project ID.
    -   `GCP_SA_KEY`: The content of the JSON key file you downloaded.
    -   `DB_INSTANCE_CONNECTION_NAME`: The full instance connection name from the previous step.
    -   `DATABASE_URL`: The connection string with the public IP. **Note:** This is primarily for local testing or external connections, not for the production Cloud Run service.
    -   `WHALE_ALERT_API_KEY`: Your Whale Alert API key.
    -   `TELEGRAM_BOT_TOKEN`: Your Telegram bot token.
    -   `TELEGRAM_CHAT_ID`: Your Telegram chat ID.

### 4. Automated Deployment

Once the setup is complete, the process is fully automated:

1.  **Push to GitHub:**
    Commit and push your changes to the `main` branch.
    ```bash
    git push origin main
    ```
2.  **CI/CD Pipeline:**
    The push automatically triggers the GitHub Actions workflow defined in `.github/workflows/google-cloud-run.yml`.
    -   The workflow authenticates with Google Cloud.
    -   It builds the Docker image using Cloud Build and pushes it to Google Artifact Registry.
    -   It then deploys the new image to Cloud Run, securely setting the environment variables from GitHub Secrets.

### 5. Managing the Bot on Cloud Run

-   **To view logs:**
    Go to the Cloud Run section of the Google Cloud Console, select your service, and go to the "Logs" tab.
-   **To check if the service is running:**
    In the Cloud Run section, you can see the status of your service, including the number of running instances.

---

## 🧮 Implemented Data Sources

| Category      | Source         | API                                                                |
| ------------- | -------------- | ------------------------------------------------------------------ |
| On-Chain      | Whale Alert    | [https://whale-alert.io](https://whale-alert.io) (Monitors BTC, ETH, SOL, XRP, ADA, AVAX, DOGE, MATIC, BNB, TRX, USDT, USDC, BUSD, DAI, TUSD, FDUSD, PYUSD and more) |
| Marktpreise   | Binance        | [https://binance-docs.github.io](https://binance-docs.github.io)   |

---

## 🧱 Next Steps & Extensions

Refer to `docs/LIVE_TRADING_ROADMAP.md` for the detailed strategic plan to evolve this bot into a fully automated trader. Our immediate focus is on:

-   **Phase 1: Enhancing Current Intelligence and Usability** (Signal Engine improvements, richer Telegram reports).
-   **Phase 2: Building Core Trading Infrastructure (Paper Trading First)** (Trade execution, position management, essential risk parameters, and robust paper trading mode).

Future considerations include advanced trading strategies (arbitrage, market making, AI/ML), sophisticated risk management, and a professional-grade system architecture.
---

## ⚠️ Disclaimer

This project is for educational and research purposes only. It is not financial advice. Trading cryptocurrencies involves significant risk.