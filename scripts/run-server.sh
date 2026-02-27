#!/bin/bash

set -e

HOST=$(snapctl get host)
export HOST
PORT=$(snapctl get port)
export PORT

$SNAP/backend/start.sh "$@"
