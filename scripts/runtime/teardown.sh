#!/bin/bash
# NetOpsBench Teardown Script
# Cleans up complete environment: topology + observability stack

set -e

TOPO_DIR=${1:-clab-topology}
STOP_OBSERVABILITY_CORE=${NETOPSBENCH_STOP_OBSERVABILITY_CORE:-0}

if [ "$(id -u)" -eq 0 ]; then
    SUDO=()
else
    # Non-interactive sudo: fail fast instead of prompting for password.
    SUDO=("sudo" "-n")
fi

BASE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$BASE_DIR"

# Locate project Python interpreter (prefers repo venv, falls back to system python3).
# shellcheck source=scripts/lib/find_python.sh
source "$BASE_DIR/scripts/lib/find_python.sh"

echo "=== NetOpsBench Teardown Start ==="
echo "Topology directory: $TOPO_DIR"
echo ""

# [1/2] Destroy Containerlab topology
echo "[1/2] Destroying Containerlab topology..."

TOPOLOGY_FILE="$TOPO_DIR/dcn.clab.yaml"
if [ ! -f "$TOPOLOGY_FILE" ]; then
    # Try to auto-detect a topology file in the directory.
    TOPOLOGY_FILE="$(ls -1 "$TOPO_DIR"/*.clab.y*ml 2>/dev/null | head -n 1 || true)"
fi

LAB_NAME=""
MGMT_NETWORK=""
if [ -n "$TOPOLOGY_FILE" ] && [ -f "$TOPOLOGY_FILE" ]; then
    # Prefer explicit lab name from the topology file.
    LAB_NAME="$(grep -E '^name:' "$TOPOLOGY_FILE" 2>/dev/null | head -n 1 | sed -E 's/^name:[[:space:]]*//')"
    if [ -z "$LAB_NAME" ]; then
        # Derive lab name from filename (strip .clab.yaml/.clab.yml).
        base="$(basename "$TOPOLOGY_FILE")"
        LAB_NAME="${base%.clab.yaml}"
        LAB_NAME="${LAB_NAME%.clab.yml}"
    fi

    # If the lab already exists, prefer the exact topology file path it was deployed with.
    # Containerlab matches containers by labels, including clab-topo-file, so path mismatches can lead to
    # "no containerlab containers found" even when containers exist.
    deployed_topo_file=""
    existing_id="$("${SUDO[@]}" docker ps -a --filter "label=containerlab=${LAB_NAME}" --format '{{.ID}}' | head -n 1 || true)"
    if [ -n "$existing_id" ]; then
        deployed_topo_file="$("${SUDO[@]}" docker inspect -f '{{ index .Config.Labels "clab-topo-file" }}' "$existing_id" 2>/dev/null || true)"
    fi
    if [ -n "$deployed_topo_file" ] && [ -f "$deployed_topo_file" ]; then
        echo "Detected deployed topology file: $deployed_topo_file"
        TOPOLOGY_FILE="$deployed_topo_file"
    fi

    echo "Using topology file: $TOPOLOGY_FILE"
    echo "Lab name: $LAB_NAME"

    METADATA_FILE="$TOPO_DIR/topology.json"
    if [ -f "$METADATA_FILE" ]; then
        MGMT_NETWORK="$($PYTHON -c 'import json, sys; topo = json.load(open(sys.argv[1])); print((topo.get("management", {}) or {}).get("network", ""))' "$METADATA_FILE")"
    fi
    TELEGRAF_CONTAINER="telegraf-${LAB_NAME}"
    "${SUDO[@]}" docker rm -f "$TELEGRAF_CONTAINER" >/dev/null 2>&1 || true
    if [ -n "$MGMT_NETWORK" ]; then
        "${SUDO[@]}" docker network disconnect "$MGMT_NETWORK" influxdb >/dev/null 2>&1 || true
    fi

    # Primary: destroy by topology file.
    destroy_out="$("${SUDO[@]}" containerlab destroy -t "$TOPOLOGY_FILE" --cleanup 2>&1 || true)"
    echo "$destroy_out"

    # Fallback: if the lab was deployed from a different path, destroy by lab name.
    if echo "$destroy_out" | grep -qi "no containerlab containers found"; then
        echo "WARN: containerlab didn't match any containers by -t; falling back to --name $LAB_NAME"
        "${SUDO[@]}" containerlab destroy --name "$LAB_NAME" --cleanup || true
    fi

    echo "Topology destroy attempted"

    # If there are no containerlab containers left, remove the shared management network if present.
    if [ -z "$("${SUDO[@]}" docker ps -a --filter label=containerlab --format '{{.ID}}' | head -n 1 || true)" ]; then
        if "${SUDO[@]}" docker network ls --format '{{.Name}}' | grep -qx "clab"; then
            "${SUDO[@]}" docker network rm clab >/dev/null 2>&1 || true
        fi
    fi
    if [ -n "$MGMT_NETWORK" ] && "${SUDO[@]}" docker network ls --format '{{.Name}}' | grep -qx "$MGMT_NETWORK"; then
        "${SUDO[@]}" docker network rm "$MGMT_NETWORK" >/dev/null 2>&1 || true
    fi
else
    echo "WARNING: No topology file found in: $TOPO_DIR"
    echo "Attempting cleanup of all containerlab labs..."
    "${SUDO[@]}" containerlab destroy --all --cleanup --yes || true
fi
echo ""

# [2/2] Stop observability stack only when explicitly requested
if [ "$STOP_OBSERVABILITY_CORE" = "1" ]; then
    echo "[2/2] Stopping observability stack..."
    cd observability
    docker compose down --volumes || true
    cd "$BASE_DIR"
    echo ""
else
    echo "[2/2] Leaving observability stack running (use 'netopsbench cleanup --core' or '--all' to stop it)"
    echo ""
fi

echo "=== Teardown Complete ==="
echo ""
echo "Target lab containers stopped and removed."
echo "To verify cleanup:"
echo "  docker ps -a | grep -E 'clab|influx|grafana|telegraf'"
echo ""
