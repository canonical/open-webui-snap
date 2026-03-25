#!/bin/bash

set -e

# Apply configs from content sharing interfaces
python3 $SNAP/bin/sync-configs.py

HOST=$(snapctl get host)
PORT=$(snapctl get port)

exec open-webui serve --host "$HOST" --port "$PORT" "$@"
