#!/bin/bash

HOST=$(snapctl get host)
PORT=$(snapctl get port)

SERVICE_INFO=$(snapctl services $SNAP_INSTANCE_NAME.listener)

STATUS=$(echo "$SERVICE_INFO" | awk '/open-webui.listener/{print $3}')

echo "$SNAP_INSTANCE_NAME.listener: $STATUS"

if [ "$STATUS" == "active" ]; then
  echo ''
  echo "To use $SNAP_INSTANCE_NAME, open the following URL in your browser: http://$HOST:$PORT"
else
  echo ''
  echo "If you expected the service to be up, check that the port $PORT is available, not bound by some other service."
fi
