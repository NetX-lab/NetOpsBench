"""Canonical topology context shared by fault services and handlers."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from netopsbench.models.topology import TopologyManifest
from netopsbench.platform.topology.topology_utils import clab_container_name


@dataclass(frozen=True, slots=True)
class FaultRuntimeContext:
    """Canonical manifest-derived state shared by fault handlers."""

    manifest: TopologyManifest
    topology_dir: Path

    @property
    def topology_file(self) -> Path:
        return self.topology_dir / f"{self.manifest.name}.clab.yaml"

    @property
    def container_names(self) -> dict[str, str]:
        return {device.name: clab_container_name(self.manifest.name, device.name) for device in self.manifest.devices}

    @property
    def clients(self) -> list[dict[str, Any]]:
        return [client.model_dump(mode="json") for client in self.manifest.clients()]

    @property
    def clients_by_leaf(self) -> dict[str, list[dict[str, Any]]]:
        grouped: dict[str, list[dict[str, Any]]] = {}
        for client in self.clients:
            attached_switch = str(client.get("attached_switch") or "").strip()
            if attached_switch:
                grouped.setdefault(attached_switch, []).append(client)
        return grouped


__all__ = ["FaultRuntimeContext"]
