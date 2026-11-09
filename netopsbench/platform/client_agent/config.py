"""Generate the compact configuration consumed by the native client agent."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from netopsbench.models.topology import TopologyManifest
from netopsbench.platform.utils.files import atomic_write_text

CLIENT_AGENT_CONFIG_NAME = "client-agent.json"
CLIENT_AGENT_SCHEMA_VERSION = 2


def build_client_agent_config(manifest: TopologyManifest) -> dict:
    """Return the shared roster and metadata for immutable per-client plans."""

    clients = []
    for client in manifest.clients():
        if not client.data_ip or not client.mgmt_ip or not client.attached_switch:
            raise ValueError(
                f"Client {client.name} requires data_ip, mgmt_ip, and attached_switch for the native client agent"
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

    plan = manifest.require_pingmesh_plan()
    return {
        "schema_version": CLIENT_AGENT_SCHEMA_VERSION,
        "topology_id": manifest.topology_id,
        "pingmesh_plan": plan.settings.model_dump(mode="json"),
        "client_plan_sha256": {
            client.name: hashlib.sha256(_client_plan_bytes(manifest, client)).hexdigest() for client in plan.clients
        },
        "clients": clients,
    }


def _client_plan_bytes(manifest, client) -> bytes:
    payload = {
        "schema_version": CLIENT_AGENT_SCHEMA_VERSION,
        "plan_id": manifest.require_pingmesh_plan().settings.plan_id,
        **client.model_dump(mode="json"),
    }
    return (json.dumps(payload, separators=(",", ":")) + "\n").encode()


def write_client_agent_config(manifest: TopologyManifest, output: str | Path) -> Path:
    path = Path(output)
    path.parent.mkdir(parents=True, exist_ok=True)
    plans_dir = path.parent / "pinglists"
    plans_dir.mkdir(parents=True, exist_ok=True)
    for client in manifest.require_pingmesh_plan().clients:
        atomic_write_text(plans_dir / f"{client.name}.json", _client_plan_bytes(manifest, client).decode())
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
