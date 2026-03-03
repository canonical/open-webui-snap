#!/bin/bash

HOST=$(snapctl get host)
PORT=$(snapctl get port)

SERVICE_NAME="$SNAP_INSTANCE_NAME.server"
SERVICE_INFO=$(snapctl services $SERVICE_NAME)

STATUS=$(echo "$SERVICE_INFO" | awk -v svc="$SERVICE_NAME" '$0 ~ svc {print $3}')

echo "$SERVICE_NAME: $STATUS"

echo ''
echo "Opening http://localhost:$PORT/ in your browser..."
xdg-open http://localhost:$PORT/
