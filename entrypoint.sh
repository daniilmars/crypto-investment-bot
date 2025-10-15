#!/bin/sh
# This script ensures that any command passed to the container is executed correctly.
set -e
exec "$@"
