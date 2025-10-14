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