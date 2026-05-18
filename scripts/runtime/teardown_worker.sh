#!/bin/bash
# Teardown one benchmark worker lab and its dedicated telegraf sidecar.

set -e

TOPOLOGY_DIR=${1:?usage: teardown_worker.sh <topology_dir>}

if [ "$(id -u)" -eq 0 ]; then
    SUDO=()
else
    SUDO=("sudo" "-n")
fi

BASE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$BASE_DIR"

# Locate project Python interpreter (prefers repo venv, falls back to system python3).
# shellcheck source=scripts/lib/find_python.sh
source "$BASE_DIR/scripts/lib/find_python.sh"

METADATA_FILE="$TOPOLOGY_DIR/topology.json"
if [ ! -f "$METADATA_FILE" ]; then
    echo "Skipping worker teardown; topology metadata not found: $METADATA_FILE"
    exit 0
fi

readarray -t WORKER_META < <($PYTHON - <<PY
import json
with open("$METADATA_FILE") as f:
    topo = json.load(f)
print((topo.get("name") or "dcn").strip())
print((topo.get("management", {}) or {}).get("network", ""))
PY
)
LAB_NAME=${WORKER_META[0]:-dcn}
MGMT_NETWORK=${WORKER_META[1]:-clab-mgmt-${LAB_NAME}}
TOPOLOGY_FILE="$TOPOLOGY_DIR/${LAB_NAME}.clab.yaml"
TELEGRAF_CONTAINER="telegraf-${LAB_NAME}"
BGP_COLLECTOR_PID_FILE="$TOPOLOGY_DIR/bgp_collector.pid"

echo "=== Tearing Down Worker Lab ==="
echo "Topology dir: $TOPOLOGY_DIR"
echo "Lab name: $LAB_NAME"

if [ -f "$BGP_COLLECTOR_PID_FILE" ]; then
    BGP_PID=$(cat "$BGP_COLLECTOR_PID_FILE" 2>/dev/null || true)
    if [ -n "$BGP_PID" ] && kill -0 "$BGP_PID" >/dev/null 2>&1; then
        kill "$BGP_PID" >/dev/null 2>&1 || true
    fi
    rm -f "$BGP_COLLECTOR_PID_FILE"
fi

"${SUDO[@]}" docker rm -f "$TELEGRAF_CONTAINER" >/dev/null 2>&1 || true
"${SUDO[@]}" docker network disconnect "$MGMT_NETWORK" influxdb >/dev/null 2>&1 || true

if [ -f "$TOPOLOGY_FILE" ]; then
    destroy_out=$("${SUDO[@]}" containerlab destroy -t "$TOPOLOGY_FILE" --cleanup 2>&1 || true)
    echo "$destroy_out"
    if echo "$destroy_out" | grep -qi "no containerlab containers found"; then
        "${SUDO[@]}" containerlab destroy --name "$LAB_NAME" --cleanup || true
    fi
else
    "${SUDO[@]}" containerlab destroy --name "$LAB_NAME" --cleanup || true
fi

if "${SUDO[@]}" docker network ls --format '{{.Name}}' | grep -qx "$MGMT_NETWORK"; then
    "${SUDO[@]}" docker network rm "$MGMT_NETWORK" >/dev/null 2>&1 || true
fi

echo "Worker teardown complete: $LAB_NAME"
