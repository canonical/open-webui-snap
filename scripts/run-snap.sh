#!/bin/bash

set -e

mkdir -p "$STATIC_DIR"

export HOST=$(snapctl get host)
export PORT=$(snapctl get port)

open-webui serve "$@"
