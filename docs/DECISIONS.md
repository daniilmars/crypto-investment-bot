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
