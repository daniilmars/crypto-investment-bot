#!/bin/sh
# This script acts as the container's entrypoint.
# It logs the received command and then executes it.
echo "ENTRYPOINT: Received command: $@" >&2
exec "$@"
