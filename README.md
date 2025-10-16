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