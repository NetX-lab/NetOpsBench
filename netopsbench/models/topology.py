"""Canonical, persisted topology schemas and the legacy agent projection."""

from __future__ import annotations

from enum import StrEnum
from math import ceil
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

SCHEMA_VERSION: Literal["3"] = "3"
DEFAULT_LINK_MTU = 9232
DEFAULT_SONIC_PORT_MTU = 9100


class DeviceRole(StrEnum):
    """Supported canonical device roles."""

    SPINE = "spine"
    LEAF = "leaf"
    CORE = "core"
    AGG = "agg"
    EDGE = "edge"
    CLIENT = "client"


class _PersistedModel(BaseModel):
    """Strict base model for versioned data written to disk."""

    model_config = ConfigDict(extra="forbid")


class Device(_PersistedModel):
    name: str
    role: DeviceRole
    mgmt_ip: str | None = None
    data_ip: str | None = None
    attached_switch: str | None = None
    asn: int | None = None
    router_id: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class LinkEndpoint(_PersistedModel):
    device: str
    interface: str


class Link(_PersistedModel):
    kind: str
    endpoints: tuple[LinkEndpoint, LinkEndpoint]
    mtu: int = Field(default=DEFAULT_LINK_MTU, gt=0)


class Management(_PersistedModel):
    network: str
    ipv4_subnet: str


class Collector(_PersistedModel):
    ipv4: str


class PingmeshPolicy(_PersistedModel):
    destination_batch_size: int | None = Field(default=None, ge=1)
    rtt_port_pool_size: int = Field(default=16, ge=1)
    rtt_ports_per_cycle: int = Field(default=4, ge=1)
    cycle_interval_seconds: int = Field(default=1, ge=1)
    df_payload_size: int = Field(default=DEFAULT_SONIC_PORT_MTU - 28, ge=1)

    def destination_batch_count(self, client_count: int) -> int:
        destinations = max(0, int(client_count) - 1)
        batch_size = self.destination_batch_size or max(1, destinations)
        return max(1, ceil(destinations / batch_size))

    def port_batch_count(self) -> int:
        return max(1, ceil(self.rtt_port_pool_size / self.rtt_ports_per_cycle))

    def coverage_epoch_cycles(self, client_count: int) -> int:
        return self.destination_batch_count(client_count) * self.port_batch_count()

    def coverage_epoch_seconds(self, client_count: int) -> int:
        return self.coverage_epoch_cycles(client_count) * self.cycle_interval_seconds

    def coverage_grace_seconds(self) -> int:
        """Return ingestion grace after one complete probe coverage epoch."""
        return max(5, 2 * self.cycle_interval_seconds)

    def complete_window_seconds(self, client_count: int) -> int:
        """Return the shortest window that covers one epoch plus ingestion grace."""
        return self.coverage_epoch_seconds(client_count) + self.coverage_grace_seconds()


class TopologyDefaults(_PersistedModel):
    link_mtu: int = Field(default=DEFAULT_LINK_MTU, gt=0)
    sonic_port_mtu: int = Field(default=DEFAULT_SONIC_PORT_MTU, gt=0)


class TopologyFacts(_PersistedModel):
    """Typed scale facts shared by CLOS and fat-tree manifests."""

    num_spines: int = Field(default=0, ge=0)
    num_leafs: int = Field(default=0, ge=0)
    num_cores: int = Field(default=0, ge=0)
    num_aggs: int = Field(default=0, ge=0)
    num_edges: int = Field(default=0, ge=0)
    num_pods: int = Field(default=0, ge=0)
    clients_per_attached_switch: int = Field(ge=1)
    total_clients: int = Field(ge=0)
    total_switches: int = Field(ge=0)
    fat_tree_k: int | None = Field(default=None, ge=2)
    full_density_clients_per_attached_switch: int | None = Field(default=None, ge=1)
    host_density: Literal["standard", "sparse"] | None = None


class RoutingMetadata(_PersistedModel):
    protocol: Literal["BGP"] = "BGP"
    spine_asn: int | None = None
    leaf_asn_range: str | None = None
    core_asn_range: str | None = None
    agg_asn_range: str | None = None
    edge_asn_range: str | None = None
    ecmp_hash_policy_by_role: dict[DeviceRole, Literal[0, 1]]
    ecmp: bool = True
    bfd: bool = True


class TopologyManifest(_PersistedModel):
    """Versioned canonical topology, independent from runtime metadata formats."""

    schema_version: Literal["3"] = SCHEMA_VERSION
    topology_id: str
    name: str
    scale: str
    family: Literal["clos", "fat-tree"]
    management: Management
    collector: Collector
    defaults: TopologyDefaults
    facts: TopologyFacts
    devices: list[Device]
    links: list[Link]
    routing: RoutingMetadata
    pingmesh: PingmeshPolicy = Field(default_factory=PingmeshPolicy)

    @model_validator(mode="after")
    def _validate_topology_references(self) -> TopologyManifest:
        names = [device.name for device in self.devices]
        if len(names) != len(set(names)):
            raise ValueError("device names must be unique")

        devices_by_name = {device.name: device for device in self.devices}
        switch_roles = {device.role for device in self.switches()}
        policy_roles = set(self.routing.ecmp_hash_policy_by_role)
        if policy_roles != switch_roles:
            missing = sorted(role.value for role in switch_roles - policy_roles)
            unexpected = sorted(role.value for role in policy_roles - switch_roles)
            details = []
            if missing:
                details.append(f"missing roles: {', '.join(missing)}")
            if unexpected:
                details.append(f"unexpected roles: {', '.join(unexpected)}")
            raise ValueError("ecmp_hash_policy_by_role must match switch roles (" + "; ".join(details) + ")")

        for link in self.links:
            for endpoint in link.endpoints:
                if endpoint.device not in devices_by_name:
                    raise ValueError(f"link endpoint references unknown device: {endpoint.device}")

        for client in self.clients():
            if not client.attached_switch:
                raise ValueError(f"client {client.name} must define attached_switch")
            attached = devices_by_name.get(client.attached_switch)
            if attached is None:
                raise ValueError(f"client attached_switch references unknown device: {client.attached_switch}")
            if attached.role is DeviceRole.CLIENT:
                raise ValueError("client attached_switch must reference a non-client device")
        return self

    def device(self, name: str) -> Device | None:
        """Return a device by name, or ``None`` when the manifest has no match."""
        return next((device for device in self.devices if device.name == name), None)

    def devices_by_role(self, role: DeviceRole | str) -> list[Device]:
        """Return devices in their persisted order for one canonical role."""
        normalized = DeviceRole(role)
        return [device for device in self.devices if device.role is normalized]

    def switches(self) -> list[Device]:
        return [device for device in self.devices if device.role is not DeviceRole.CLIENT]

    def routing_devices(self) -> list[Device]:
        return self.switches()

    def edge_devices(self) -> list[Device]:
        edges = self.devices_by_role(DeviceRole.EDGE)
        return edges if edges else self.devices_by_role(DeviceRole.LEAF)

    def clients(self) -> list[Device]:
        return self.devices_by_role(DeviceRole.CLIENT)

    def client_attached_devices(self) -> list[Device]:
        attached_names = {client.attached_switch for client in self.clients() if client.attached_switch}
        return [device for device in self.switches() if device.name in attached_names]

    def to_agent_topology(self) -> dict[str, Any]:
        """Adapt canonical data to the grouped topology metadata used by agents today."""
        groups = {
            "spines": self.devices_by_role(DeviceRole.SPINE),
            "leafs": self.devices_by_role(DeviceRole.LEAF),
            "cores": self.devices_by_role(DeviceRole.CORE),
            "aggs": self.devices_by_role(DeviceRole.AGG),
            "edges": self.devices_by_role(DeviceRole.EDGE),
            "clients": self.clients(),
        }
        if self.family == "fat-tree":
            groups["spines"] = groups["cores"]
            groups["leafs"] = groups["edges"]

        projected: dict[str, Any] = {
            "name": self.name,
            "topology_id": self.topology_id,
            "topology_scale": self.scale,
            "topology_type": self.family,
            "management": self.management.model_dump(mode="json"),
            "collector": self.collector.model_dump(mode="json"),
            "defaults": self.defaults.model_dump(mode="json"),
            "mtu_semantics": {
                "link_mtu_scope": "containerlab/client link MTU",
                "sonic_port_mtu_scope": "SONiC front-panel interface MTU",
                "note": (
                    "Do not compare link_mtu 9232 directly against healthy SONiC port MTU 9100 "
                    "when diagnosing faults."
                ),
            },
            "scale": self._agent_scale_facts(),
            "devices": {
                group: [self._device_to_agent_entry(device) for device in devices] for group, devices in groups.items()
            },
            "links": [
                {
                    "type": link.kind,
                    "endpoints": [endpoint.device for endpoint in link.endpoints],
                }
                for link in self.links
            ],
            "routing": self.routing.model_dump(mode="json", exclude_none=True),
        }
        projected["pingmesh"] = {
            **self.pingmesh.model_dump(mode="json"),
            "destination_batch_count": self.pingmesh.destination_batch_count(self.facts.total_clients),
            "port_batch_count": self.pingmesh.port_batch_count(),
            "coverage_epoch_cycles": self.pingmesh.coverage_epoch_cycles(self.facts.total_clients),
            "coverage_epoch_seconds": self.pingmesh.coverage_epoch_seconds(self.facts.total_clients),
        }
        if self.family == "fat-tree":
            projected["fat_tree_k"] = self.facts.fat_tree_k
        return projected

    def _agent_scale_facts(self) -> dict[str, Any]:
        if self.family == "clos":
            return {
                "name": self.scale,
                "num_spines": self.facts.num_spines,
                "num_leafs": self.facts.num_leafs,
                "clients_per_leaf": self.facts.clients_per_attached_switch,
                "total_clients": self.facts.total_clients,
                "total_devices": self.facts.total_switches,
            }
        return {
            "name": self.scale,
            "num_core": self.facts.num_cores,
            "num_agg": self.facts.num_aggs,
            "num_edge": self.facts.num_edges,
            "num_pods": self.facts.num_pods,
            "clients_per_edge": self.facts.clients_per_attached_switch,
            "full_density_clients_per_edge": self.facts.full_density_clients_per_attached_switch,
            "host_density": self.facts.host_density,
            "total_clients": self.facts.total_clients,
            "total_devices": self.facts.total_switches,
            "num_spines": self.facts.num_cores,
            "num_leafs": self.facts.num_edges,
        }

    def _device_to_agent_entry(self, device: Device) -> dict[str, Any]:
        entry = device.model_dump(exclude={"role", "metadata"}, exclude_none=True, mode="json")
        entry.update({key: value for key, value in device.metadata.items() if key not in Device.model_fields})
        if device.role is DeviceRole.CLIENT and device.attached_switch:
            entry["leaf"] = device.attached_switch
            if self.family == "fat-tree":
                entry["edge"] = device.attached_switch
        return entry


__all__ = [
    "SCHEMA_VERSION",
    "Collector",
    "Device",
    "DeviceRole",
    "Link",
    "LinkEndpoint",
    "Management",
    "PingmeshPolicy",
    "RoutingMetadata",
    "TopologyDefaults",
    "TopologyFacts",
    "TopologyManifest",
]
