#!/bin/bash

set -e

HOST=$(snapctl get host)
PORT=$(snapctl get port)

exec open-webui serve --host "$HOST" --port "$PORT" "$@"
