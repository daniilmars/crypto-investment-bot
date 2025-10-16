#!/bin/sh
set -e

# Start the main application
if [ "$RUN_TELEGRAM_BOT" = "true" ]; then
  echo "Starting Telegram bot..."
  python3 main.py &
else
  echo "Skipping Telegram bot..."
fi

# Execute the CMD from the Dockerfile
exec "$@"
