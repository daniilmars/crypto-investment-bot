# Project Screening Report

**Project:** crypto-investment-bot
**Date:** 2026-02-10
**Status:** Reviewed

---

## Project Overview

A Python-based crypto and stock investment alert bot that monitors whale transactions, analyzes technical indicators, and generates automated BUY/SELL/HOLD signals. Features paper trading, Telegram notifications, AI-powered market summaries (Gemini), and deployment to Google Cloud Run.

**Tech Stack:** Python 3.12, FastAPI, PostgreSQL/SQLite, Telegram Bot API, Binance API, TensorFlow (LSTM), Google Gemini

---

## Test Results

- **44 tests passed** out of 44 collected (100% pass rate)
- 4 test files could not be collected due to dependency issues (`feedparser` sgmllib3k build failure, `vertexai` not installed)
- Affected: `test_news_data.py`, `test_news_signal_integration.py`
- Core functionality (signal engines, technical indicators, database, collectors) fully tested

---

## Security Findings

### CRITICAL

| # | Issue | Location | Description |
|---|-------|----------|-------------|
| 1 | Hardcoded database credentials | `scripts/temporary_script.py:19` | PostgreSQL URL with plaintext password and IP address committed to repo |

### HIGH

| # | Issue | Location | Description |
|---|-------|----------|-------------|
| 2 | SQL injection via f-strings | `src/database.py:280,461` | Table names interpolated directly into SQL without using the existing `ALLOWED_TABLES` whitelist |
| 3 | SQL injection via f-strings | `scripts/train_lstm_model.py:41-42` | Symbol parameter interpolated into SQL queries |
| 4 | SQL injection via f-strings | `scripts/whale_price_correlation.py:31,38,45` | Unparameterized queries |
| 5 | SQL injection via f-strings | `scripts/performance_analysis.py:18` | Timestamp string interpolated into query |
| 6 | Missing input validation | Multiple collectors | Symbol parameters passed to APIs/DB without format validation |

### MEDIUM

| # | Issue | Location | Description |
|---|-------|----------|-------------|
| 7 | No webhook CSRF protection | `main.py:567-583` | Telegram webhook endpoint lacks secret token validation |
| 8 | Bare exception handlers | `src/database.py:113,117` | `except Exception: pass` silently swallows errors |
| 9 | XXE risk in RSS parsing | `src/collectors/news_data.py:94-106` | `feedparser.parse()` without XXE protections |
| 10 | Information disclosure | `src/collectors/whale_alert.py:65` | API URLs logged at DEBUG level |

### LOW

| # | Issue | Location | Description |
|---|-------|----------|-------------|
| 11 | Missing CORS configuration | `main.py` | No explicit CORS policy on FastAPI app |
| 12 | No global rate limiting | Multiple collectors | Individual backoff exists but no application-wide rate limiter |

---

## Code Quality

### Strengths
- Clear separation of concerns (collectors, analysis, execution, notification)
- Comprehensive test suite covering core signal logic and technical indicators
- Configuration driven via YAML + environment variables
- Docker setup uses non-root user (`appuser`)
- `settings.yaml` properly excluded from git via `.gitignore`
- Secrets managed through GitHub Actions secrets for production
- CI/CD pipeline with test gate before deployment
- Extensive documentation in `docs/` directory

### Areas for Improvement
- **Long parameter lists:** `generate_signal()` in `signal_engine.py` takes 12 parameters -- consider grouping into dataclasses
- **Magic numbers:** Hardcoded thresholds scattered across multiple files instead of centralized configuration
- **Inconsistent error handling:** Different patterns used across modules
- **Dependency issue:** `feedparser` sub-dependency `sgmllib3k` fails to build on Python 3.11, blocking 2 test files from collection

---

## Infrastructure Review

### Docker (Good)
- `python:3.12-slim` base image
- Non-root user execution
- Health check configured
- `.dockerignore` present

### CI/CD (Good with notes)
- GitHub Actions runs tests before deploy
- Uses pinned action versions (`actions/checkout@v3`)
- All secrets managed via GitHub Secrets
- **Note:** `actions/checkout@v3` and `google-github-actions/auth@v1` could be updated to latest versions

### Dependencies
- 16 direct dependencies in `requirements.txt`
- Most are pinned to specific versions (good)
- `google-cloud-aiplatform`, `google-genai`, `vaderSentiment`, `newsapi-python`, `feedparser` use `>=` ranges (less reproducible)

---

## Recommended Actions (Priority Order)

1. **Remove hardcoded credentials** from `scripts/temporary_script.py` immediately and rotate the exposed password
2. **Replace f-string SQL queries** with parameterized queries across `src/database.py` and all scripts
3. **Add Telegram webhook secret token** validation to the `/webhook` endpoint
4. **Add input validation** for symbol/ticker parameters at the collector boundary
5. **Fix bare exception handlers** in `src/database.py` to at least log errors
6. **Pin all dependency versions** in `requirements.txt` for reproducible builds
7. **Update GitHub Actions** to latest versions (`checkout@v4`, `setup-python@v5`, `auth@v2`)
