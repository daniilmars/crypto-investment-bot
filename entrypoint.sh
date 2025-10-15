#!/bin/sh
# This script acts as the container's entrypoint.
# It executes any command passed to it.
exec sh -c "$@"
