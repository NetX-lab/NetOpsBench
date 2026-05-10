#!/bin/bash
# Start shared observability core services needed by parallel workers.

set -e

if [ "$(id -u)" -eq 0 ]; then
    SUDO=()
else
    SUDO=("sudo" "-n")
fi

BASE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$BASE_DIR/observability"

echo "=== Starting Observability Core ==="
"${SUDO[@]}" docker compose up -d influxdb grafana
