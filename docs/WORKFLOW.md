# Project Workflow: The Gemini CLI Session Lifecycle

This document outlines the structured workflow used for the development of this project. Adhering to this lifecycle ensures that every session is efficient, builds on previous work, and leaves a clear record of progress.

---

### **Phase 1: Onboarding (The Learning Phase)**

*Goal: To build a complete and current "mental model" of the project before making any changes.*

At the beginning of every new session, the Gemini CLI agent will perform the following actions to understand the project's current state:

1.  **Review the `README.md`:** To re-establish the high-level project goals, architecture, and usage instructions.
2.  **Review the `docs/DECISIONS.md`:** This is the most critical step. The agent will read the entire decision log to understand the history of the project, the reasoning behind key architectural choices, and the outcome of the most recent session.
3.  **Review the Main Entry Point (`main.py`):** To understand the current orchestration of the bot's logic and how the different modules are connected.
4.  **Review Configuration (`config/settings.yaml.example`):** To understand what settings, API keys, and parameters are available and required.
5.  **Review Dependencies (`requirements.txt`):** To confirm the available libraries and tools.

This phase is complete once the agent has a clear picture of the project's status and the context of the last session.

### **Phase 2: Development (The Work Phase)**

*Goal: To execute the user's request in an iterative and verifiable manner.*

This is the core work cycle:

1.  **Plan:** Based on the user's request, the agent will form a clear plan.
2.  **Implement:** The agent will make the necessary code changes.
3.  **Verify:** The agent will run tests, linters, or the script itself to ensure the changes are working correctly.
4.  **Iterate:** The agent will refine the implementation based on verification results until the goal is achieved.

### **Phase 3: Offboarding (The Documentation Phase)**

*Goal: To create a clean handoff for the next session by documenting all significant decisions made.*

At the end of each session, the agent will perform these steps:

1.  **Summarize Changes:** The agent will internally review the work that was completed.
2.  **Draft a New Decision Record:** The agent will formulate a new entry for the `docs/DECISIONS.md` file, including the decision and the reasoning behind it.
3.  **Commit the Documentation:** The agent will commit the updated `DECISIONS.md` file to the repository, ensuring the project's memory is always version-controlled alongside the code.
