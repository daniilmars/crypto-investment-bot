# Architectural Decision Records (ADR)

This document logs key architectural and design decisions made throughout the project.

---

### ADR-001: Project Initialization and Structure

**Date:** 2025-10-13

**Decision:**
- Initialize the project with a standard Git repository.
- Adopt a modular directory structure (`src/collectors`, `src/analysis`, `src/notify`) to separate concerns.
- Use a `config/settings.yaml` for configuration and API keys, with a `settings.yaml.example` as a template. The actual `settings.yaml` is excluded from version control via `.gitignore`.
- Manage dependencies using a `requirements.txt` file.

**Reasoning:**
This setup follows standard best practices for Python applications. It promotes clean code, maintainability, and security by preventing secrets from being committed to the repository. It also ensures that the development environment is reproducible.

---

### ADR-002: Implementation of Core Bot Platform

**Date:** 2025-10-13

**Decision:**
- **Implemented Data Persistence:** A SQLite database (`data/crypto_data.db`) was created to store all collected data. This provides a historical record for analysis and prevents data loss between bot runs.
- **Built a Backtesting Framework:** Created `src/analysis/backtest.py` to simulate the trading strategy on historical data. This allows for objective performance measurement and data-driven strategy refinement.
- **Enhanced the Signal Engine:** Upgraded the signal logic to be multi-layered, combining market sentiment (Fear & Greed), on-chain activity (Whale Alert), and price action (Simple Moving Average) for more robust signals.
- **Integrated News Sentiment Analysis:** Added a new data collector for news headlines via NewsAPI, including a simple sentiment analysis model. This adds a narrative/social data layer to the bot's analysis capabilities.
- **Refactored to Professional Logging:** Replaced all `print()` statements with a centralized logging system (`src/logger.py`) for structured, timestamped, and level-based application monitoring.
- **Updated Documentation:** The `README.md` was significantly overhauled to reflect the project's current architecture, features, and usage instructions.

**Reasoning:**
These decisions transition the project from a simple script to a sophisticated, data-driven platform. The focus was on building a robust foundation for continuous improvement, enabling the strategy to be tested, measured, and enhanced in a systematic way.

---

### ADR-003: Integration of a Formal Testing Framework

**Date:** 2025-10-14

**Decision:**
- **Integrated Pytest:** The `pytest` framework was added to the project and included in `requirements.txt` to establish a formal, automated testing process.
- **Created Initial Test Suites:** A `tests/` directory was created, and initial test suites were developed for two core components:
    - `tests/test_signal_engine.py`: Migrated and expanded the existing ad-hoc tests into a structured pytest format.
    - `tests/test_database.py`: Developed a new test suite to verify the database initialization and connection logic.
- **Refactored for Testability:** The `src/database.py` module was refactored to support testing with an in-memory database, preventing tests from interfering with production data. All `print()` statements in the module were replaced with the centralized logger for consistent application monitoring.

**Reasoning:**
This decision addresses the most significant weakness identified in the initial quality assessment: the lack of automated tests. Integrating a formal testing framework is a critical step in maturing the codebase. It improves code quality, significantly reduces the risk of regressions when adding new features or refactoring, and provides verifiable documentation for how individual components are expected to behave. This foundation of tests makes future development safer, faster, and more maintainable.

---

### ADR-004: Enhancing Signal Engine Intelligence and Test Coverage

**Date:** 2025-10-14

**Decision:**
- **Expanded Test Coverage to All Collectors:** Following the establishment of the testing framework, comprehensive test suites were created for all data collectors (`binance_data`, `fear_and_greed`, `whale_alert`, `news_data`). This ensures each data source is robustly tested for success and failure cases.
- **Enabled SMA in Live Mode:** The main application loop (`main.py`) was refactored to fetch historical prices from the local database. This allows the live bot to calculate a Simple Moving Average (SMA) and use the same price action analysis that was previously only available in the backtester.
- **Integrated News Sentiment into Signal Engine:** The signal generation logic was upgraded to be a four-factor model. It now uses recent news sentiment as a final confirmation layer, downgrading BUY/SELL signals if they are contradicted by overwhelmingly negative/positive news, respectively.
- **Improved Notification Richness:** The Telegram alert function was updated to include the cryptocurrency symbol and its current price in the notification message, making the alerts more informative and actionable for the user.

**Reasoning:**
These decisions represent a significant step in advancing the bot's intelligence and reliability. By completing the test coverage for the data pipeline, the bot's foundation is now highly robust. Enhancing the live signal engine to use SMA and news sentiment makes its signals smarter, more nuanced, and better aligned with the project's goal of multi-source analysis. The bot is now closer to being a productive personal investment tool.

---

### ADR-005: Strategic Pivot to Technical and On-Chain Analysis

**Date:** 2025-10-14

**Decision:**
- **Pivoted the core strategy from a sentiment-based model to a purely technical and on-chain model.** Based on research into state-of-the-art trading bots, the decision was made to focus on more quantifiable and industry-standard indicators.
- **Removed Fear & Greed and News Sentiment:** The data collectors, database tables, signal engine logic, and tests for the Fear & Greed Index and NewsAPI sentiment analysis were completely removed from the project.
- **Integrated Relative Strength Index (RSI):** A new `technical_indicators.py` module was created to house technical analysis functions. The first indicator implemented is the RSI, a widely-used momentum oscillator.
- **Refactored the Signal Engine:** The signal engine was rewritten to use a three-factor model:
    1.  **Trend:** Simple Moving Average (SMA)
    2.  **Momentum:** Relative Strength Index (RSI)
    3.  **On-Chain Flow:** Whale Alert transaction analysis
- **Updated all tests and documentation** to reflect the new, more robust strategy.

**Reasoning:**
This strategic pivot addresses the core concern that sentiment-based indicators are often low-signal, lagging, and difficult to quantify reliably. The new model is aligned with best practices in algorithmic trading, which favor verifiable, data-driven technical indicators. The combination of a trend indicator (SMA) and a momentum indicator (RSI), confirmed by significant on-chain activity, creates a much stronger and more defensible foundation for generating trading signals. This makes the bot's logic more robust, testable, and less prone to noise.

---

### ADR-006: Implementation of High-Interest Wallet Tracking

**Date:** 2025-10-14

**Decision:**
- **Developed a high-priority signal override system based on the activity of specific, known wallets.** The signal engine was upgraded to check for transactions involving a user-defined list of `high_interest_wallets` (e.g., "Grayscale", "US Government") in the `settings.yaml` file.
- **Implemented Event-Driven Logic:** If a transaction from a high-interest wallet to an exchange (potential sell) or from an exchange to a high-interest wallet (potential buy) is detected, the engine generates an immediate BUY/SELL signal. This signal bypasses the standard technical analysis, allowing the bot to react swiftly to the actions of major market players.
- **Updated Tests:** The test suite was expanded to include specific test cases that verify this override logic functions correctly.

**Reasoning:**
While general on-chain volume can be noisy, the actions of known large entities (institutions, governments) are extremely high-signal events. This feature adds a layer of event-driven intelligence to the bot, allowing it to move beyond purely technical indicators and react to significant, real-world market events that are less ambiguous than typical "unknown wallet" transactions.

---

### ADR-007: Implementation of Stablecoin Flow Analysis

**Date:** 2025-10-14

**Decision:**
- **Integrated a market-wide buying pressure indicator by analyzing stablecoin inflows to exchanges.** A new function, `get_stablecoin_flows`, was added to the `whale_alert.py` collector to specifically monitor and aggregate the USD value of stablecoins (USDT, USDC, etc.) being transferred to exchange wallets.
- **Created a High-Priority BUY Signal:** The signal engine was enhanced with a new top-level check. If the total stablecoin inflow within a cycle exceeds a user-configurable `stablecoin_inflow_threshold_usd`, the engine generates an immediate, market-wide BUY signal.
- **Made it Configurable:** Both the list of stablecoins to monitor and the inflow threshold were added to the `settings.yaml` file for easy user customization.

**Reasoning:**
The movement of large amounts of stablecoins onto exchanges is a widely-recognized leading indicator of potential buying activity ("dry powder" being prepared). This feature provides the bot with a powerful, forward-looking signal that is independent of any single asset's technical chart. It allows the bot to anticipate bullish market shifts, making its strategy more proactive.

---

### ADR-008: Implementation of Transaction Velocity Analysis

**Date:** 2025-10-14

**Decision:**
- **Developed a volatility detection system by analyzing the frequency of whale transactions.** A new function, `calculate_transaction_velocity`, was created to compare the number of large transactions in the last hour against a historical baseline (e.g., the last 24 hours).
- **Generated a New Signal Type:** If the current transaction frequency exceeds the historical average by a configurable multiplier, the signal engine now generates a `VOLATILITY_WARNING` signal. This signal takes precedence over standard BUY/SELL signals.
- **Enhanced Database and Configuration:** The database was updated with a function to query historical transaction timestamps. The `settings.yaml` file was updated to allow user configuration of the baseline period and the anomaly detection threshold.

**Reasoning:**
This feature adds a crucial layer of risk management and market stability analysis. A sudden, anomalous spike in the number of large transactions often precedes high volatility, regardless of the market's direction. By detecting this activity and issuing a specific warning, the bot can avoid entering new positions during unpredictable conditions, thereby preserving capital and filtering out signals that might be generated under unstable market circumstances.

---

### ADR-009: Containerization with Docker for Production Deployment

**Date:** 2025-10-14

**Decision:**
- **Containerized the application using Docker.** A `Dockerfile` was created to define a portable, consistent, and isolated environment for the bot.
- **Implemented a centralized configuration module (`src/config.py`).** This module was developed to read settings from `settings.yaml` and securely override them with environment variables, making the application configurable at runtime.
- **Refactored the entire application** to use the new centralized config, removing local file-based configuration loading from all other modules.
- **Created a `.dockerignore` file** to ensure a lightweight and secure production image.

**Reasoning:**
Containerization is a best practice for modern application deployment. It solves the "it works on my machine" problem, enhances security by abstracting away secrets into environment variables, and simplifies the deployment process on any platform that supports Docker (from cloud VPS to Heroku). This makes the bot portable, scalable, and reliable.

---

### ADR-010: Database Migration from SQLite to PostgreSQL

**Date:** 2025-10-14

**Decision:**
- **Migrated the database backend from SQLite to PostgreSQL.** This was identified as a critical requirement for deploying to cloud platforms like Heroku, which have ephemeral filesystems where a SQLite file would be deleted daily.
- **Refactored the database module (`src/database.py`)** to be dual-purpose: it now connects to a PostgreSQL database if a `DATABASE_URL` environment variable is present, otherwise it falls back to the local SQLite file for development ease.
- **Updated all SQL queries and table definitions** to be compatible with both PostgreSQL and SQLite syntax.
- **Added `psycopg2-binary`** to `requirements.txt` as the PostgreSQL driver.
- **Created a `heroku.yml` file** to define the build and run process for Heroku, specifying a `worker` process for the bot.

**Reasoning:**
While SQLite was excellent for initial development, it is not suitable for a production, cloud-hosted application due to its file-based nature and concurrency limitations. Migrating to a robust, client-server database like PostgreSQL is essential for data persistence, scalability, and reliability. This change makes the bot a true cloud-native application, ready for serious, long-term deployment.

---

### ADR-011: Implementation of a CI/CD Pipeline with GitHub Actions

**Date:** 2025-10-15

**Decision:**
- **Implemented a full CI/CD pipeline using GitHub Actions.** A workflow was created at `.github/workflows/deploy.yml` that automates the testing and deployment of the application.
- **The workflow is triggered on every push to the `master` branch.** It performs the following steps:
    1.  Installs all project dependencies.
    2.  Runs the complete `pytest` suite.
    3.  **Only if tests pass,** it proceeds to deploy to Heroku.

**Reasoning:**
Automating the deployment process is a critical step in professionalizing the project. A CI/CD pipeline eliminates manual deployment errors, ensures that no code is deployed without passing all quality checks (tests), and creates a repeatable, reliable path to production. This significantly improves the stability and maintainability of the application.

---

### ADR-012: Comprehensive Test Suite Refactoring

**Date:** 2025-10-15

**Decision:**
- **Refactored the entire test suite** to align with the application's mature architecture, specifically the centralized configuration and dual-database system.
- **Replaced file-based test setups with mocking.** All tests that previously created temporary database files (`tests/test_database.py`) were rewritten to use `unittest.mock.patch` to inject mock database connections.
- **Corrected mock targets.** Tests that were failing due to incorrect patch targets (e.g., patching `app_config` where it was defined instead of where it was used) were fixed.

**Reasoning:**
The initial test suite was written before major architectural refactors, rendering it completely broken. The repeated CI/CD failures made it clear that a comprehensive overhaul was needed. This refactoring effort was critical to re-establishing a reliable quality gate for the project, enabling the CI/CD pipeline to function correctly, and ensuring that future development can proceed safely.

---

### ADR-013: Configuration of Heroku for Docker Deployment

**Date:** 2025-10-15

**Decision:**
- **Configured the Heroku application for a Docker-based deployment.** This involved two key changes:
    1.  **Setting the Heroku Stack:** The application's stack was manually set to `container` using the Heroku CLI. This was a one-time setup step to prepare the Heroku environment to accept Docker images instead of source code.
    2.  **Correcting `heroku.yml`:** The `heroku.yml` file was simplified and corrected to properly define a `worker` process built from the `Dockerfile`.
- **Automated Secret Management:** The GitHub Actions workflow was enhanced to automatically set the required Heroku Config Vars (environment variables) from GitHub Secrets during each deployment.

**Reasoning:**
The deployment was consistently failing due to a mismatch between the deployment method (Docker container) and the Heroku application's configuration (default source code stack). These changes aligned the Heroku environment with the project's containerized architecture. Automating the secret management ensures that the production environment is always correctly and securely configured without manual intervention.
