#!/bin/sh
echo "--- Starting Crypto Bot ---"
set -e

# Start the main application
exec python3 main.py "$@"
