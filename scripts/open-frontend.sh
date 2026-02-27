#!/bin/bash

PORT=$(snapctl get port)

xdg-open http://localhost:$PORT/
