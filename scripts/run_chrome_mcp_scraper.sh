#!/bin/bash
# Runs the Chrome MCP news scraper via Claude Code headless mode.
# Requires: Chrome running with Claude extension, claude CLI installed.
#
# Usage:
#   ./scripts/run_chrome_mcp_scraper.sh
#
# Schedule with launchd (see scripts/com.cryptobot.news-scraper.plist)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
PROMPT_FILE="$SCRIPT_DIR/chrome_mcp_scraper_prompt.md"
OUTPUT_FILE="$PROJECT_DIR/data/scraped-news.json"
LOG_FILE="$PROJECT_DIR/data/chrome-scraper.log"

mkdir -p "$PROJECT_DIR/data"

echo "[$(date -Iseconds)] Starting Chrome MCP news scraper..." >> "$LOG_FILE"

# Run Claude Code in headless prompt mode with the scraper prompt
claude -p "$(cat "$PROMPT_FILE")" --chrome 2>> "$LOG_FILE"

if [ -f "$OUTPUT_FILE" ]; then
    ARTICLE_COUNT=$(python3 -c "import json; print(json.load(open('$OUTPUT_FILE'))['total_articles'])" 2>/dev/null || echo "?")
    echo "[$(date -Iseconds)] Scraping complete: $ARTICLE_COUNT articles written to $OUTPUT_FILE" >> "$LOG_FILE"
else
    echo "[$(date -Iseconds)] WARNING: Output file not created" >> "$LOG_FILE"
fi
