"""Generate the compact configuration consumed by the native client agent."""

from __future__ import annotations

import json
from pathlib import Path

from netopsbench.models.topology import TopologyManifest
from netopsbench.platform.utils.files import atomic_write_text

CLIENT_AGENT_CONFIG_NAME = "client-agent.json"
CLIENT_AGENT_SCHEMA_VERSION = 1


def build_client_agent_config(manifest: TopologyManifest) -> dict:
    """Return an O(N) client description; each client derives its own probes."""

    clients = []
    for client in manifest.clients():
        if not client.data_ip or not client.mgmt_ip or not client.attached_switch:
            raise ValueError(
                f"Client {client.name} requires data_ip, mgmt_ip, and attached_switch " "for the native client agent"
            )
        rack = str(client.metadata.get("rack") or client.attached_switch)
        clients.append(
            {
                "name": client.name,
                "data_ip": client.data_ip,
                "management_ip": client.mgmt_ip,
                "rack": rack,
                "leaf": client.attached_switch,
            }
        )

    client_count = len(clients)
    policy = {
        **manifest.pingmesh.model_dump(mode="json"),
        "destination_batch_count": manifest.pingmesh.destination_batch_count(client_count),
        "port_batch_count": manifest.pingmesh.port_batch_count(),
        "coverage_epoch_cycles": manifest.pingmesh.coverage_epoch_cycles(client_count),
        "coverage_epoch_seconds": manifest.pingmesh.coverage_epoch_seconds(client_count),
    }
    return {
        "schema_version": CLIENT_AGENT_SCHEMA_VERSION,
        "topology_id": manifest.topology_id,
        "pingmesh_policy": policy,
        "clients": clients,
    }


def write_client_agent_config(manifest: TopologyManifest, output: str | Path) -> Path:
    path = Path(output)
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(
        path,
        json.dumps(build_client_agent_config(manifest), indent=2) + "\n",
    )
    return path


__all__ = [
    "CLIENT_AGENT_CONFIG_NAME",
    "CLIENT_AGENT_SCHEMA_VERSION",
    "build_client_agent_config",
    "write_client_agent_config",
]
