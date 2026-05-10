"""Shared topology context for fault injection components."""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from typing import Any

from netopsbench.platform.topology.topology_utils import TopologyState


@dataclass
class FaultContext:
    """Shared topology and runtime state passed to all fault services and handlers."""

    topology_name: str = "dcn"
    container_names: dict[str, str] = field(default_factory=dict)
    topology_metadata: dict[str, Any] = field(default_factory=dict)
    device_mgmt_ips: dict[str, str] = field(default_factory=dict)
    clients: list[dict[str, Any]] = field(default_factory=list)
    clients_by_leaf: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    clab_dir: str = ""
    scenarios_dir: str = ""

    @classmethod
    def from_topology_state(
        cls,
        state: TopologyState,
        clab_dir: str = "",
        scenarios_dir: str = "",
    ) -> FaultContext:
        return cls(
            topology_name=state.topology_name,
            container_names=dict(state.container_names),
            topology_metadata=copy.deepcopy(state.topology_metadata),
            device_mgmt_ips=dict(state.device_mgmt_ips),
            clients=[copy.deepcopy(c) for c in state.clients],
            clients_by_leaf={
                leaf: [copy.deepcopy(c) for c in clients] for leaf, clients in state.clients_by_leaf.items()
            },
            clab_dir=clab_dir,
            scenarios_dir=scenarios_dir,
        )

    def update_from_topology_state(self, state: TopologyState) -> None:
        self.topology_name = state.topology_name
        self.container_names = dict(state.container_names)
        self.topology_metadata = copy.deepcopy(state.topology_metadata)
        self.device_mgmt_ips = dict(state.device_mgmt_ips)
        self.clients = [copy.deepcopy(c) for c in state.clients]
        self.clients_by_leaf = {
            leaf: [copy.deepcopy(c) for c in clients] for leaf, clients in state.clients_by_leaf.items()
        }
