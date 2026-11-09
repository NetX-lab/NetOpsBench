"""Topology-family-neutral fabric planning models."""

from __future__ import annotations

from dataclasses import dataclass, field

from netopsbench.models.topology import Device, DeviceRole, TopologyManifest


@dataclass(frozen=True, slots=True)
class BGPNeighborPlan:
    peer_ip: str
    remote_as: int


@dataclass(frozen=True, slots=True)
class RenderSettings:
    syslog_collector: str


@dataclass(frozen=True, slots=True)
class DevicePlan:
    """Canonical identity and every input needed to render one node."""

    device: Device
    required_ports: int = 0
    configdb_interface_cidrs: dict[str, tuple[str, ...]] = field(default_factory=dict)
    bgp_asn: int | None = None
    bgp_router_id: str | None = None
    bgp_neighbors: tuple[BGPNeighborPlan, ...] = ()
    bgp_networks: tuple[str, ...] = ()
    client_commands: tuple[str, ...] = ()

    @property
    def name(self) -> str:
        return self.device.name

    @property
    def is_client(self) -> bool:
        return self.device.role is DeviceRole.CLIENT


@dataclass(frozen=True, slots=True)
class FabricPlan:
    """A complete, artifact-independent plan for one generated fabric."""

    manifest: TopologyManifest
    device_plans: tuple[DevicePlan, ...]
    nos_kind: str
    nos_image: str
    client_image: str
    render_settings: RenderSettings
    yaml_header: str

    def __post_init__(self) -> None:
        manifest_names = [device.name for device in self.manifest.devices]
        plan_names = [device_plan.name for device_plan in self.device_plans]
        if manifest_names != plan_names:
            raise ValueError("FabricPlan device plans must match canonical manifest device order")
        used_interfaces: set[tuple[str, str]] = set()
        degrees: dict[str, int] = {}
        for link in self.manifest.links:
            for endpoint in link.endpoints:
                key = (endpoint.device, endpoint.interface)
                if key in used_interfaces:
                    raise ValueError(f"Topology reuses data interface {endpoint.device}:{endpoint.interface}")
                used_interfaces.add(key)
                degrees[endpoint.device] = degrees.get(endpoint.device, 0) + 1
        for device_plan in self.device_plans:
            if device_plan.is_client:
                continue
            if device_plan.required_ports <= 0:
                raise ValueError(f"switch {device_plan.name} must define required_ports")
            if degrees.get(device_plan.name, 0) != device_plan.required_ports:
                raise ValueError(f"switch {device_plan.name} port capacity does not match its data links")
            if device_plan.bgp_asn is None or device_plan.bgp_router_id is None:
                raise ValueError(f"switch {device_plan.name} must define BGP identity")


__all__ = ["BGPNeighborPlan", "DevicePlan", "FabricPlan", "RenderSettings"]
