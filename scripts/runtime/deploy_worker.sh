#!/bin/bash
# Deploy one benchmark worker lab with dedicated management network, bucket, and telegraf.

set -e

SCALE=${1:?usage: deploy_worker.sh <scale> <topology_dir> <lab_name> <mgmt_subnet> [bucket]}
TOPOLOGY_DIR=${2:?usage: deploy_worker.sh <scale> <topology_dir> <lab_name> <mgmt_subnet> [bucket]}
LAB_NAME=${3:?usage: deploy_worker.sh <scale> <topology_dir> <lab_name> <mgmt_subnet> [bucket]}
MGMT_SUBNET=${4:?usage: deploy_worker.sh <scale> <topology_dir> <lab_name> <mgmt_subnet> [bucket]}
INFLUXDB_BUCKET=${5:-${NETOPSBENCH_INFLUXDB_BUCKET:-netopsbench}}
MGMT_NETWORK="clab-mgmt-${LAB_NAME}"
TOPOLOGY_ID=${NETOPSBENCH_TOPOLOGY_ID:-$LAB_NAME}

if [ "$(id -u)" -eq 0 ]; then
    SUDO=()
else
    SUDO=("sudo" "-n")
fi

BASE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$BASE_DIR"

# Locate project Python interpreter (prefers repo venv, falls back to system python3).
# Override via NETOPSBENCH_PYTHON env var if needed (e.g. for git worktrees).
# shellcheck source=scripts/lib/find_python.sh
source "$BASE_DIR/scripts/lib/find_python.sh"

mkdir -p "$TOPOLOGY_DIR"

echo "=== Deploying Worker Lab ==="
echo "Scale: $SCALE"
echo "Topology dir: $TOPOLOGY_DIR"
echo "Lab name: $LAB_NAME"
echo "Management subnet: $MGMT_SUBNET"
echo "InfluxDB bucket: $INFLUXDB_BUCKET"

if [ "${NETOPSBENCH_SKIP_OBSERVABILITY_CORE_START:-0}" = "1" ]; then
    echo "[1/8] Observability core already ensured by caller, skipping shared startup"
else
    echo "[1/8] Starting observability core..."
    bash scripts/observability/start_observability_core.sh
fi
$PYTHON -m netopsbench.platform.observability.influxdb \
    --url "${NETOPSBENCH_INFLUXDB_URL:-http://localhost:8086}" \
    --token "${NETOPSBENCH_INFLUXDB_TOKEN:-replace-me}" \
    --org "${NETOPSBENCH_INFLUXDB_ORG:-netopsbench}" \
    --bucket "$INFLUXDB_BUCKET"

echo "[2/8] Generating worker topology..."
# Clean stale configs/clab dirs from any previous run that may have left
# root-owned files behind (containerlab creates these).
"${SUDO[@]}" rm -rf "$TOPOLOGY_DIR/configs" "$TOPOLOGY_DIR/clab-"* 2>/dev/null || true
$PYTHON - <<PY
from netopsbench.platform.topology.generator import generate_topology
generate_topology(
    scale="$SCALE",
    output_dir="$TOPOLOGY_DIR",
    name="$LAB_NAME",
    mgmt_subnet="$MGMT_SUBNET",
    mgmt_network="$MGMT_NETWORK",
)
PY

TOPOLOGY_FILE="$TOPOLOGY_DIR/${LAB_NAME}.clab.yaml"
if [ ! -f "$TOPOLOGY_FILE" ]; then
    echo "ERROR: topology file not found: $TOPOLOGY_FILE" >&2
    exit 1
fi

echo "[3/8] Deploying containerlab topology..."
"${SUDO[@]}" containerlab deploy -t "$TOPOLOGY_FILE" --reconfigure

echo "[4/8] Applying SONiC configs..."
$PYTHON -m netopsbench.platform.runtime.apply_configs "$TOPOLOGY_DIR" 4 "$LAB_NAME"

readarray -t WORKER_META < <($PYTHON - <<PY
import json
with open("$TOPOLOGY_DIR/topology.json") as f:
    topo = json.load(f)
print((topo.get("management", {}) or {}).get("network", ""))
print((topo.get("collector", {}) or {}).get("ipv4", ""))
PY
)
MGMT_NETWORK=${WORKER_META[0]:-$MGMT_NETWORK}
COLLECTOR_IP=${WORKER_META[1]:-}
if [ -z "$COLLECTOR_IP" ]; then
    echo "ERROR: collector IP missing from topology metadata" >&2
    exit 1
fi

echo "[5/8] Attaching InfluxDB and starting worker telegraf..."
if ! "${SUDO[@]}" docker inspect influxdb >/dev/null 2>&1; then
    echo "ERROR: shared influxdb container is not running" >&2
    exit 1
fi
"${SUDO[@]}" docker network connect "$MGMT_NETWORK" influxdb --alias influxdb >/dev/null 2>&1 || true
NETOPSBENCH_TOPOLOGY_ID="$TOPOLOGY_ID" \
NETOPSBENCH_INFLUXDB_BUCKET="$INFLUXDB_BUCKET" \
NETOPSBENCH_TELEGRAF_INFLUXDB_URL="http://influxdb:8086" \
    bash scripts/observability/start_worker_telegraf.sh "$TOPOLOGY_DIR" "telegraf-${LAB_NAME}"

echo "[6/8] Starting BGP collector..."
BGP_COLLECTOR_PID_FILE="$TOPOLOGY_DIR/bgp_collector.pid"
BGP_COLLECTOR_LOG_FILE="$TOPOLOGY_DIR/bgp_collector.log"
BGP_COLLECTOR_OUTPUT_FILE="$TOPOLOGY_DIR/bgp_neighbors.lp"
rm -f "$BGP_COLLECTOR_PID_FILE"
$PYTHON scripts/runtime/run_bgp_collector.py \
    "$TOPOLOGY_DIR/topology.json" \
    --output "$BGP_COLLECTOR_OUTPUT_FILE" \
    --interval "${NETOPSBENCH_BGP_POLL_INTERVAL_SECONDS:-10}" \
    >> "$BGP_COLLECTOR_LOG_FILE" 2>&1 &
echo $! > "$BGP_COLLECTOR_PID_FILE"

echo "[7/8] Deploying Pingmesh agents..."
NETOPSBENCH_TOPOLOGY_ID="$TOPOLOGY_ID" \
NETOPSBENCH_INFLUXDB_BUCKET="$INFLUXDB_BUCKET" \
NETOPSBENCH_PINGMESH_INFLUXDB_URL="http://influxdb:8086" \
    $PYTHON -m netopsbench.platform.pingmesh.deploy "$TOPOLOGY_DIR/pinglist.json" "$TOPOLOGY_DIR"

echo "[8/8] Validating worker health..."
# Accept either NETOPSBENCH_WORKER_HEALTH_RETRIES (set by run_all_benchmarks.sh) or
# the legacy NETOPSBENCH_WORKER_HEALTH_ATTEMPTS name, defaulting to 5.
HEALTH_ATTEMPTS=${NETOPSBENCH_WORKER_HEALTH_RETRIES:-${NETOPSBENCH_WORKER_HEALTH_ATTEMPTS:-5}}
HEALTH_RETRY_DELAY_SECONDS=${NETOPSBENCH_WORKER_HEALTH_RETRY_DELAY_SECONDS:-20}
HEALTH_OK=0
for attempt in $(seq 1 "$HEALTH_ATTEMPTS"); do
    if NETOPSBENCH_INFLUXDB_BUCKET="$INFLUXDB_BUCKET" \
       NETOPSBENCH_TOPOLOGY_ID="$TOPOLOGY_ID" \
       $PYTHON -m netopsbench.platform.worker.health "$TOPOLOGY_DIR"; then
        HEALTH_OK=1
        break
    fi
    if [ "$attempt" -lt "$HEALTH_ATTEMPTS" ]; then
        echo "Worker health check attempt $attempt/$HEALTH_ATTEMPTS failed; retrying in ${HEALTH_RETRY_DELAY_SECONDS}s..."
        sleep "$HEALTH_RETRY_DELAY_SECONDS"
    fi
done

if [ "$HEALTH_OK" -ne 1 ]; then
    echo "ERROR: worker health check failed after $HEALTH_ATTEMPTS attempt(s)" >&2
    exit 1
fi

echo "Worker deployment complete: $LAB_NAME"
