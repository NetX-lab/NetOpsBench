#!/bin/bash
# Start a worker-local telegraf instance attached to one lab management network.

set -e

TOPOLOGY_DIR=${1:?usage: start_worker_telegraf.sh <topology_dir> [container_name]}
CONTAINER_NAME=${2:-}

if [ "$(id -u)" -eq 0 ]; then
    SUDO=()
else
    SUDO=("sudo" "-n")
fi

BASE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$BASE_DIR"

METADATA_FILE="$TOPOLOGY_DIR/topology.json"
if [ ! -f "$METADATA_FILE" ]; then
    echo "ERROR: topology metadata not found: $METADATA_FILE" >&2
    exit 1
fi

readarray -t WORKER_META < <(python3 - <<PY
import json
with open("$METADATA_FILE") as f:
    topo = json.load(f)
print((topo.get("name") or "dcn").strip())
print((topo.get("management", {}) or {}).get("network", ""))
print((topo.get("collector", {}) or {}).get("ipv4", ""))
PY
)

LAB_NAME=${WORKER_META[0]:-dcn}
MGMT_NETWORK=${WORKER_META[1]:-clab-mgmt-${LAB_NAME}}
COLLECTOR_IP=${WORKER_META[2]:-}
CONTAINER_NAME=${CONTAINER_NAME:-telegraf-${LAB_NAME}}
CONFIG_DIR="$(cd "$TOPOLOGY_DIR" && pwd)"
CONFIG_PATH="$CONFIG_DIR/${CONTAINER_NAME}.conf"
BGP_FILE_PATH="$CONFIG_DIR/bgp_neighbors.lp"

if [ -z "$COLLECTOR_IP" ]; then
    echo "ERROR: collector IP missing from topology metadata" >&2
    exit 1
fi

python3 -m netopsbench.platform.observability.telegraf \
    "$METADATA_FILE" \
    --output "$CONFIG_PATH" \
    --influxdb-url "${NETOPSBENCH_TELEGRAF_INFLUXDB_URL:-http://influxdb:8086}" \
    --influxdb-token "${NETOPSBENCH_INFLUXDB_TOKEN:-replace-me}" \
    --influxdb-org "${NETOPSBENCH_INFLUXDB_ORG:-netopsbench}" \
    --bucket "${NETOPSBENCH_INFLUXDB_BUCKET:-netopsbench}" \
    --topology-id "${NETOPSBENCH_TOPOLOGY_ID:-$LAB_NAME}"

: > "$BGP_FILE_PATH"

"${SUDO[@]}" docker rm -f "$CONTAINER_NAME" >/dev/null 2>&1 || true
"${SUDO[@]}" docker run -d \
    --name "$CONTAINER_NAME" \
    --restart unless-stopped \
    --network "$MGMT_NETWORK" \
    --ip "$COLLECTOR_IP" \
    -v "$CONFIG_PATH:/etc/telegraf/telegraf.conf:ro" \
    -v "$CONFIG_DIR:/var/lib/netopsbench:ro" \
    telegraf:latest >/dev/null

echo "Started worker telegraf: $CONTAINER_NAME"
echo "  network: $MGMT_NETWORK"
echo "  collector ip: $COLLECTOR_IP"
