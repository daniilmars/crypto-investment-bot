# Onboarding Guide for Gemini CLI Agent

Welcome to the Crypto Investment Bot project! This guide outlines the essential steps for the Gemini CLI agent to establish a complete and current understanding of the project's state at the beginning of each session.

---

## Phase 1: Onboarding (The Learning Phase)

**Goal:** To build a complete and current "mental model" of the project before making any changes.

At the beginning of every new session, please perform the following actions to understand the project's current state:

1.  **Review the `README.md`:**
    *   **Purpose:** To re-establish the high-level project goals, architecture, and usage instructions.
    *   **Action:** Read the entire `README.md` file.

2.  **Review the `docs/DECISIONS.md`:**
    *   **Purpose:** This is a critical step. Read the entire decision log to understand the history of the project, the reasoning behind key architectural choices, and the outcome of the most recent session.
    *   **Action:** Read the entire `docs/DECISIONS.md` file.

3.  **Review the Main Entry Point (`main.py`):**
    *   **Purpose:** To understand the current orchestration of the bot's logic. The application is now built using the **FastAPI** framework, which handles incoming Telegram updates via a webhook. This file contains the main application logic, including the startup and shutdown events, the core `bot_loop`, and the `status_update_loop` which run as asynchronous background tasks.
    *   **Action:** Read the entire `main.py` file to understand the FastAPI integration and the asynchronous workflow.

4.  **Review Configuration (`config/settings.yaml.example` and `config/watch_list.yaml`):**
    *   **Purpose:** To understand what settings, API keys, parameters are available and required, and which assets are being monitored.
    *   **Action:** Read both `config/settings.yaml.example` and `config/watch_list.yaml` files.

5.  **Review Dependencies (`requirements.txt`):**
    *   **Purpose:** To confirm the available libraries and tools.
    *   **Action:** Read the entire `requirements.txt` file.

6.  **Review Investment Logic (`docs/INVESTMENT_LOGIC.md`):**
    *   **Purpose:** To understand the detailed investment and trading strategy, including technical indicators and on-chain data analysis.
    *   **Action:** Read the entire `docs/INVESTMENT_LOGIC.md` file.

7.  **Review the Backtesting Framework (`docs/BACKTESTING.md`):**
    *   **Purpose:** To understand the tools and workflow for backtesting and optimizing the trading strategy.
    *   **Action:** Read the entire `docs/BACKTESTING.md` file.

8.  **Review Cloud and Networking Documentation (`docs/CLOUD_HEALTH_CHECKS.md` and `docs/NETWORKING.md`):**
    *   **Purpose:** To understand deployment, infrastructure, and networking considerations.
    *   **Action:** Read both `docs/CLOUD_HEALTH_CHECKS.md` and `docs/NETWORKING.md` files.

9.  **Review Project Structure (`src/` and `tests/` directories):**
    *   **Purpose:** To understand the codebase organization, module responsibilities, and testing conventions.
    *   **Action:** Perform a recursive listing of the `src/` and `tests/` directories to get an overview of their contents.

---

**Completion:**

Once you have completed all the above steps and have a clear picture of the project's status and the context of the last session, please confirm your readiness for the next task.
