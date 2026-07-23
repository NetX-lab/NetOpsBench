"""Generate standardized scenario YAML files from topology metadata."""

from __future__ import annotations

import hashlib
import ipaddress
import random
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from importlib.resources import files
from pathlib import Path
from typing import Any

import yaml

from netopsbench.models.scenario import ScenarioSpec
from netopsbench.models.topology import DeviceRole, TopologyManifest
from netopsbench.platform.topology.configdb_payload import interface_names_for_config, interface_networks_for_config
from netopsbench.platform.topology.topology_utils import load_topology_manifest


def default_campaign_spec() -> Path:
    resource = files("netopsbench.platform.scenario").joinpath("specs", "fault_campaign.yaml")
    if not resource.is_file():
        raise FileNotFoundError("Packaged default scenario campaign is missing")
    return Path(str(resource))


@dataclass
class TopologyContext:
    topology_dir: Path
    manifest: TopologyManifest
    device_interfaces: dict[str, list[str]]
    leaf_interface_roles: dict[str, dict[str, list[str]]]
    client_interfaces: dict[str, list[str]]
    device_asns: dict[str, int] = field(default_factory=dict)
    bgp_neighbors: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    bgp_networks: dict[str, list[dict[str, Any]]] = field(default_factory=dict)

    @property
    def scale(self) -> str:
        return self.manifest.scale

    @property
    def metadata(self) -> dict[str, Any]:
        return self.manifest.to_agent_topology()

    def _device_names(self, role: DeviceRole) -> list[str]:
        return [device.name for device in self.manifest.devices_by_role(role)]

    @property
    def spines(self) -> list[str]:
        return self._device_names(DeviceRole.SPINE)

    @property
    def leafs(self) -> list[str]:
        return self._device_names(DeviceRole.LEAF)

    @property
    def cores(self) -> list[str]:
        return self._device_names(DeviceRole.CORE)

    @property
    def aggs(self) -> list[str]:
        return self._device_names(DeviceRole.AGG)

    @property
    def edges(self) -> list[str]:
        return self._device_names(DeviceRole.EDGE)

    @property
    def clients(self) -> list[dict[str, Any]]:
        return self.metadata["devices"]["clients"]


def load_yaml(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def load_topology(scale: str, topology_dir: str | None) -> TopologyContext:
    if topology_dir:
        topo_dir = Path(topology_dir)
    else:
        topo_dir = Path.cwd() / "lab-topology" / f"generated_topology_{scale}"

    metadata_path = topo_dir / "topology.json"
    if not metadata_path.exists():
        raise FileNotFoundError(f"Topology metadata not found: {metadata_path}")

    manifest = load_topology_manifest(metadata_path)
    device_interfaces, leaf_interface_roles, client_interfaces = _interface_facts_from_manifest(manifest)
    device_asns: dict[str, int] = {}
    bgp_neighbors: dict[str, list[dict[str, Any]]] = {}
    bgp_networks: dict[str, list[dict[str, Any]]] = {}

    config_dir = topo_dir / "configs"
    switch_devices = [device.name for device in manifest.switches()]
    for device in switch_devices:
        cfg = _device_config_path(config_dir, device)
        frr_cfg = config_dir / "frr" / f"{device}.conf"
        if not cfg.is_file():
            raise FileNotFoundError(f"SONiC ConfigDB artifact not found: {cfg}")
        if not frr_cfg.is_file():
            raise FileNotFoundError(f"FRR artifact not found: {frr_cfg}")
        parsed_interfaces = parse_network_interfaces(cfg)
        if not parsed_interfaces:
            raise ValueError(f"SONiC ConfigDB has no interfaces: {cfg}")
        device_interfaces[device] = parsed_interfaces
        bgp_info = parse_bgp_config(frr_cfg)
        if bgp_info.get("local_as") is None:
            raise ValueError(f"FRR artifact has no BGP router stanza: {frr_cfg}")
        device_asns[device] = int(bgp_info["local_as"])
        neighbors = list(bgp_info.get("neighbors") or [])
        interface_networks = interface_networks_for_config(cfg)
        interface_roles = leaf_interface_roles.get(device) or {}
        for neighbor in neighbors:
            try:
                peer_ip = ipaddress.ip_address(str(neighbor["peer_ip"]))
            except (KeyError, ValueError):
                continue
            interface = next(
                (
                    name
                    for name, network in interface_networks.items()
                    if peer_ip in ipaddress.ip_network(network, strict=False)
                ),
                None,
            )
            if interface is None:
                continue
            neighbor["interface"] = interface
            neighbor["interface_role"] = next(
                (
                    role
                    for role in ("uplink", "downlink")
                    if interface in (interface_roles.get(role) or [])
                ),
                None,
            )
        bgp_neighbors[device] = neighbors
        bgp_networks[device] = list(bgp_info.get("networks") or [])

    return TopologyContext(
        topology_dir=topo_dir,
        manifest=manifest,
        device_interfaces=device_interfaces,
        leaf_interface_roles=leaf_interface_roles,
        client_interfaces=client_interfaces,
        device_asns=device_asns,
        bgp_neighbors=bgp_neighbors,
        bgp_networks=bgp_networks,
    )


def _device_config_path(config_dir: Path, device: str) -> Path:
    return config_dir / "sonic" / device / "config_db.json"


def parse_network_interfaces(cfg_path: Path) -> list[str]:
    return interface_names_for_config(cfg_path)


def parse_bgp_config(cfg_path: Path) -> dict[str, Any]:
    """Parse local ASN, neighbors, and advertised networks from one FRR config."""
    info: dict[str, Any] = {"local_as": None, "neighbors": [], "networks": []}
    if not cfg_path.exists():
        return info

    local_as_re = re.compile(r"^router bgp (\d+)$")
    neighbor_re = re.compile(r"^neighbor (\S+) remote-as (\d+)$")
    neighbor_rm_re = re.compile(r"^neighbor (\S+) route-map (\S+) (in|out)$")
    network_re = re.compile(r"^network (\S+)(?: route-map (\S+))?$")

    neighbors: dict[str, dict[str, Any]] = {}
    networks: list[dict[str, Any]] = []
    seen_networks = set()

    with cfg_path.open("r", encoding="utf-8") as f:
        for raw in f:
            line = raw.strip()
            match = local_as_re.match(line)
            if match:
                info["local_as"] = int(match.group(1))
                continue

            match = neighbor_re.match(line)
            if match:
                peer_ip = match.group(1)
                peer = neighbors.setdefault(
                    peer_ip,
                    {"peer_ip": peer_ip, "remote_as": None, "route_maps": []},
                )
                peer["remote_as"] = int(match.group(2))
                continue

            match = neighbor_rm_re.match(line)
            if match:
                peer_ip, route_map, direction = match.groups()
                peer = neighbors.setdefault(
                    peer_ip,
                    {"peer_ip": peer_ip, "remote_as": None, "route_maps": []},
                )
                attachment = {"name": route_map, "direction": direction}
                if attachment not in peer["route_maps"]:
                    peer["route_maps"].append(attachment)
                continue

            match = network_re.match(line)
            if match:
                prefix, route_map = match.groups()
                identity = (prefix, route_map or "")
                if identity in seen_networks:
                    continue
                seen_networks.add(identity)
                networks.append({"prefix": prefix, "route_map": route_map})

    info["neighbors"] = sorted(neighbors.values(), key=lambda item: item["peer_ip"])
    info["networks"] = sorted(networks, key=lambda item: item["prefix"])
    return info


def _interface_facts_from_manifest(
    manifest: TopologyManifest,
) -> tuple[dict[str, list[str]], dict[str, dict[str, list[str]]], dict[str, list[str]]]:
    """Derive interface ownership and fabric direction from canonical links."""
    devices = {device.name: device for device in manifest.devices}
    switch_interfaces: dict[str, set[str]] = {}
    client_interfaces: dict[str, set[str]] = {}
    interface_roles: dict[str, dict[str, set[str]]] = {}

    def add_role(device: str, interface: str, role: str) -> None:
        normalized = normalize_sonic_interface(interface)
        if normalized is None:
            return
        roles = interface_roles.setdefault(device, {"uplink": set(), "downlink": set(), "any": set()})
        roles[role].add(normalized)
        roles["any"].add(normalized)

    uplink_peers = {
        DeviceRole.LEAF: {DeviceRole.SPINE},
        DeviceRole.EDGE: {DeviceRole.AGG},
        DeviceRole.AGG: {DeviceRole.CORE},
    }
    downlink_peers = {
        DeviceRole.LEAF: {DeviceRole.CLIENT},
        DeviceRole.EDGE: {DeviceRole.CLIENT},
        DeviceRole.AGG: {DeviceRole.EDGE},
    }
    for link in manifest.links:
        left, right = link.endpoints
        for endpoint, peer in ((left, right), (right, left)):
            device = devices[endpoint.device]
            peer_role = devices[peer.device].role
            if device.role is DeviceRole.CLIENT:
                client_interfaces.setdefault(device.name, set()).add(endpoint.interface)
                continue
            normalized = normalize_sonic_interface(endpoint.interface)
            if normalized is not None:
                switch_interfaces.setdefault(device.name, set()).add(normalized)
            if peer_role in uplink_peers.get(device.role, set()):
                add_role(device.name, endpoint.interface, "uplink")
            elif peer_role in downlink_peers.get(device.role, set()):
                add_role(device.name, endpoint.interface, "downlink")

    return (
        {name: sorted(interfaces) for name, interfaces in switch_interfaces.items()},
        {
            name: {role: sorted(interfaces) for role, interfaces in roles.items()}
            for name, roles in interface_roles.items()
        },
        {name: sorted(interfaces) for name, interfaces in client_interfaces.items()},
    )


def normalize_sonic_interface(name: str | None) -> str | None:
    """Normalize interface names to SONiC EthernetX when possible."""
    if not name:
        return name
    raw = name.strip()
    lower = raw.lower()
    if lower.startswith("ethernet") and raw[8:].isdigit():
        return f"Ethernet{int(raw[8:])}"
    if lower.startswith("eth") and raw[3:].isdigit():
        idx = int(raw[3:])
        if idx >= 1:
            return f"Ethernet{(idx - 1) * 4}"
    if lower.startswith("e1-") and raw[3:].isdigit():
        idx = int(raw[3:])
        if idx >= 1:
            return f"Ethernet{(idx - 1) * 4}"
    if "ethernet-1/" in lower:
        try:
            idx = int(raw.split("/")[-1])
            if idx >= 1:
                return f"Ethernet{(idx - 1) * 4}"
        except ValueError:
            return raw
    return raw


def count_for_scale(item: dict[str, Any], scale: str, default_count: int) -> int:
    cps = item.get("count_per_scale", {})
    if isinstance(cps, dict) and scale in cps:
        return int(cps[scale])
    return int(item.get("count", default_count))


def device_candidates(role: str, topo: TopologyContext) -> list[str]:
    if role == "spine":
        return topo.cores if topo.cores else topo.spines
    if role == "core":
        return topo.cores if topo.cores else topo.spines
    if role == "leaf":
        return topo.edges if topo.edges else topo.leafs
    if role == "edge":
        return topo.edges if topo.edges else topo.leafs
    if role == "agg":
        if not topo.aggs:
            raise ValueError("device_role 'agg' requires a fat-tree topology")
        return topo.aggs
    if role == "client":
        return [c["name"] for c in topo.clients]
    raise ValueError(f"Unknown role: {role}")


def pick_device(role: str, topo: TopologyContext, rng: random.Random) -> str:
    return rng.choice(device_candidates(role, topo))


def pick_network_interface(device: str, topo: TopologyContext, rng: random.Random) -> str:
    candidates = topo.device_interfaces.get(device) or ["Ethernet0"]
    normalized = [normalize_sonic_interface(c) for c in candidates if normalize_sonic_interface(c)]
    return rng.choice(normalized) if normalized else "Ethernet0"


def pick_bgp_neighbor(
    device: str,
    topo: TopologyContext,
    rng: random.Random,
    interface_role: str | None = None,
) -> dict[str, Any]:
    candidates = topo.bgp_neighbors.get(device) or []
    if interface_role:
        candidates = [item for item in candidates if item.get("interface_role") == interface_role]
    if not candidates:
        suffix = f" for interface_role={interface_role}" if interface_role else ""
        raise ValueError(f"Device {device} has no parsed BGP neighbors{suffix}")
    return dict(rng.choice(candidates))


def pick_advertised_network(device: str, topo: TopologyContext, rng: random.Random) -> dict[str, Any]:
    candidates = topo.bgp_networks.get(device) or []
    if not candidates:
        raise ValueError(f"Device {device} has no parsed BGP networks")
    return dict(rng.choice(candidates))


def _client_leaf(client: dict[str, Any]) -> str:
    return str(client.get("leaf") or client.get("edge") or "").strip()


def _pick_client_for_route_fault(
    topo: TopologyContext,
    rng: random.Random,
    excluded_leaf: str | None = None,
) -> dict[str, Any]:
    # Prefer remote leaf destinations to avoid selecting a local directly-connected subnet
    # on the same leaf where the route fault is injected.
    candidates = list(topo.clients)
    if excluded_leaf:
        remote = [c for c in candidates if _client_leaf(c) != excluded_leaf]
        if remote:
            candidates = remote
    return rng.choice(candidates)


def _client_subnet(client: dict[str, Any], prefix_len: int = 30) -> str:
    ip_str = str(client.get("data_ip") or "").strip()
    if not ip_str:
        raise ValueError(f"Client {client.get('name', 'unknown')} missing data_ip in topology metadata")
    ip_obj = ipaddress.ip_address(ip_str)
    net = ipaddress.ip_network(f"{ip_obj}/{prefix_len}", strict=False)
    return str(net)


def _client_host_route(client: dict[str, Any]) -> str:
    ip_str = str(client.get("data_ip") or "").strip()
    if not ip_str:
        raise ValueError(f"Client {client.get('name', 'unknown')} missing data_ip in topology metadata")
    return f"{ip_str}/32"


def pick_link_down_interface(device: str, topo: TopologyContext, rng: random.Random) -> str:
    candidates = topo.device_interfaces.get(device) or ["Ethernet0"]
    candidates = [normalize_sonic_interface(c) for c in candidates if normalize_sonic_interface(c)]
    if not candidates:
        candidates = ["Ethernet0"]
    manifest_device = topo.manifest.device(device)
    if manifest_device is not None and manifest_device.role in {DeviceRole.LEAF, DeviceRole.EDGE}:
        roles = topo.leaf_interface_roles.get(device)
        if roles and roles.get("downlink"):
            return rng.choice(roles["downlink"])
    return rng.choice(candidates)


def pick_leaf_interface(device: str, topo: TopologyContext, rng: random.Random, role: str) -> str:
    roles = topo.leaf_interface_roles.get(device) or {}
    role = (role or "any").lower()
    pool = roles.get(role) or roles.get("any")
    if pool:
        return rng.choice(pool)
    return pick_network_interface(device, topo, rng)


def is_role_aware_switch(device: str, topo: TopologyContext) -> bool:
    manifest_device = topo.manifest.device(device)
    return manifest_device is not None and manifest_device.role in {
        DeviceRole.LEAF,
        DeviceRole.EDGE,
        DeviceRole.AGG,
    }


def pick_client_interface(device: str, topo: TopologyContext, rng: random.Random) -> str:
    candidates = topo.client_interfaces.get(device)
    if candidates:
        return rng.choice(candidates)
    return "eth1"


@dataclass
class FaultBuildTarget:
    device: str
    interface: str | None = None
    prefix: str | None = None
    mtu: int | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


def _generic_fault_target(
    topo: TopologyContext,
    rng: random.Random,
    template: dict[str, Any],
    target_device: str | None = None,
) -> FaultBuildTarget:
    return FaultBuildTarget(
        device=target_device or pick_device(template.get("device_role", "leaf"), topo, rng)
    )


def _link_down_target(
    topo: TopologyContext,
    rng: random.Random,
    template: dict[str, Any],
    target_device: str | None = None,
) -> FaultBuildTarget:
    target = _generic_fault_target(topo, rng, template, target_device)
    interface_role = template.get("interface_role")
    if not interface_role:
        target.interface = pick_link_down_interface(target.device, topo, rng)
    elif is_role_aware_switch(target.device, topo):
        target.interface = pick_leaf_interface(target.device, topo, rng, interface_role)
    else:
        target.interface = pick_network_interface(target.device, topo, rng)
    return target


def _role_interface_target(
    topo: TopologyContext,
    rng: random.Random,
    template: dict[str, Any],
    target_device: str | None = None,
) -> FaultBuildTarget:
    target = _generic_fault_target(topo, rng, template, target_device)
    role = template.get("interface_role") or "uplink"
    target.interface = (
        pick_leaf_interface(target.device, topo, rng, role)
        if is_role_aware_switch(target.device, topo)
        else pick_network_interface(target.device, topo, rng)
    )
    return target


def _packet_fault_target(
    topo: TopologyContext,
    rng: random.Random,
    template: dict[str, Any],
    target_device: str | None = None,
) -> FaultBuildTarget:
    target = _generic_fault_target(topo, rng, template, target_device)
    manifest_device = topo.manifest.device(target.device)
    if manifest_device is not None and manifest_device.role is DeviceRole.CLIENT:
        target.interface = pick_client_interface(target.device, topo, rng)
    elif is_role_aware_switch(target.device, topo):
        target.interface = pick_leaf_interface(
            target.device,
            topo,
            rng,
            template.get("interface_role") or "uplink",
        )
    else:
        target.interface = pick_network_interface(target.device, topo, rng)
    return target


def _blackhole_target(
    topo: TopologyContext,
    rng: random.Random,
    template: dict[str, Any],
    target_device: str | None = None,
) -> FaultBuildTarget:
    target = _generic_fault_target(topo, rng, template, target_device)
    client = _pick_client_for_route_fault(topo, rng, excluded_leaf=target.device)
    target.prefix = _client_subnet(client, prefix_len=30)
    return target


def _static_route_target(
    topo: TopologyContext,
    rng: random.Random,
    template: dict[str, Any],
    target_device: str | None = None,
) -> FaultBuildTarget:
    device = target_device or pick_device("leaf", topo, rng)
    client = _pick_client_for_route_fault(topo, rng, excluded_leaf=device)
    return FaultBuildTarget(
        device=device,
        metadata={"target_ip": _client_host_route(client), "wrong_nexthop": "auto"},
    )


def _bgp_neighbor_target(
    topo: TopologyContext,
    rng: random.Random,
    template: dict[str, Any],
    target_device: str | None = None,
) -> FaultBuildTarget:
    target = _generic_fault_target(topo, rng, template, target_device)
    neighbor = pick_bgp_neighbor(
        target.device,
        topo,
        rng,
        interface_role=template.get("interface_role"),
    )
    target.metadata = {
        "peer_ip": neighbor["peer_ip"],
        "misconfig_kind": template.get("misconfig_kind", "peer_as_mismatch"),
    }
    if neighbor.get("remote_as") is not None:
        target.metadata["original_remote_as"] = int(neighbor["remote_as"])
    return target


def _route_policy_target(
    topo: TopologyContext,
    rng: random.Random,
    template: dict[str, Any],
    target_device: str | None = None,
) -> FaultBuildTarget:
    target = _generic_fault_target(topo, rng, template, target_device)
    network = pick_advertised_network(target.device, topo, rng)
    target.prefix = network["prefix"]
    target.metadata = {"misconfig_kind": template.get("misconfig_kind", "network_statement_missing")}
    if network.get("route_map"):
        target.metadata["route_map"] = network["route_map"]
    return target


def _acl_target(
    topo: TopologyContext,
    rng: random.Random,
    template: dict[str, Any],
    target_device: str | None = None,
) -> FaultBuildTarget:
    target = _generic_fault_target(topo, rng, template, target_device)
    target.prefix = pick_advertised_network(target.device, topo, rng)["prefix"]
    target.interface = (
        pick_leaf_interface(target.device, topo, rng, "uplink")
        if is_role_aware_switch(target.device, topo)
        else pick_network_interface(target.device, topo, rng)
    )
    target.metadata = {"direction": template.get("direction", "in")}
    return target


FaultScenarioBuilder = Callable[
    [TopologyContext, random.Random, dict[str, Any], str | None],
    FaultBuildTarget,
]

FAULT_SCENARIO_BUILDERS: dict[str, FaultScenarioBuilder] = {
    "device_down": _generic_fault_target,
    "link_down": _link_down_target,
    "link_flapping": _role_interface_target,
    "mtu_mismatch": _role_interface_target,
    "high_latency": _role_interface_target,
    "packet_loss": _packet_fault_target,
    "packet_corruption": _packet_fault_target,
    "blackhole_route": _blackhole_target,
    "static_route_misconfig": _static_route_target,
    "bgp_neighbor_misconfig": _bgp_neighbor_target,
    "route_policy_misconfig": _route_policy_target,
    "acl_misconfig": _acl_target,
}


def diagnostic_observation_duration(base_duration: int, manifest: TopologyManifest) -> int:
    """Return one complete Pingmesh epoch plus topology-derived ingestion grace."""
    complete_seconds = manifest.pingmesh.complete_window_seconds(manifest.facts.total_clients)
    return max(base_duration, complete_seconds)


def _validate_traffic_profiles(spec: dict[str, Any]) -> None:
    configured = [spec.get("defaults", {}).get("traffic_profile")]
    configured.extend(template.get("traffic_profile") for template in spec.get("fault_templates", []))
    invalid = next((profile for profile in configured if profile not in (None, "standard")), None)
    if invalid is not None:
        raise ValueError(f"Only the standard traffic profile is supported, got: {invalid}")


def build_fault_instance(
    fault_type: str,
    difficulty: str,
    topo: TopologyContext,
    rng: random.Random,
    defaults: dict[str, Any],
    template: dict[str, Any],
    idx: int,
    target_device: str | None = None,
) -> dict[str, Any]:
    # Healthy scenarios use the same diagnosable episode lifecycle without injection.
    if fault_type == "none":
        observation_duration = diagnostic_observation_duration(
            int(defaults.get("fault_duration_seconds", 30)),
            topo.manifest,
        )
        scenario_id = f"generated_healthy_network_{topo.scale}_{idx:03d}"
        return {
            "schema_version": "1",
            "scenario_id": scenario_id,
            "name": f"Generated healthy network case #{idx} ({topo.scale})",
            "description": f"Auto-generated healthy network scenario ({topo.scale} topology).",
            "topology_scale": topo.scale,
            "traffic_profile": "standard",
            "metadata": {
                "difficulty": difficulty,
                "generator": {
                    "seed": defaults.get("seed"),
                    "topology_dir": str(topo.topology_dir),
                    "template": template.get("name", "healthy_network"),
                },
            },
            "episode": {
                "episode_id": "diagnosis",
                "description": "Observe healthy network without fault injection",
                "fault_type": "none",
                "duration_seconds": observation_duration,
                "stabilization_time": 0,
            },
        }

    # Merge nested ``parameters`` dict into the top-level template so that
    # campaign-level overrides like ``parameters: {misconfig_kind: ...}``
    # are visible to ``template.get("misconfig_kind")`` lookups below.
    if "parameters" in template:
        template = {**template, **template["parameters"]}
    try:
        builder = FAULT_SCENARIO_BUILDERS[fault_type]
    except KeyError as exc:
        raise ValueError(f"No scenario builder registered for fault type: {fault_type}") from exc
    target = builder(topo, rng, template, target_device)
    extra_episode_metadata = dict(target.metadata)

    if template.get("variant"):
        extra_episode_metadata["variant"] = template["variant"]
    if template.get("recovery_mode"):
        extra_episode_metadata["recovery_mode"] = template["recovery_mode"]

    parameter_defaults = {
        "link_flapping": {"iterations": 6, "down_time": 2, "up_time": 3},
        "packet_loss": {"loss_pct": 20},
        "packet_corruption": {"corruption_pct": 20},
        "high_latency": {"latency_ms": 100},
    }
    for key, default in parameter_defaults.get(fault_type, {}).items():
        extra_episode_metadata[key] = int(template.get(key, default))
    if fault_type == "mtu_mismatch":
        target.mtu = int(template.get("mtu", 1400))

    scenario_id = f"generated_{fault_type}_{topo.scale}_{idx:03d}"
    scenario_name = f"Generated {fault_type} case #{idx} ({topo.scale})"

    fault_duration = diagnostic_observation_duration(
        int(defaults.get("fault_duration_seconds", 30)),
        topo.manifest,
    )
    fault_stabilization = int(
        template.get("fault_stabilization_seconds", defaults.get("fault_stabilization_seconds", 5))
    )
    episode_fault = {
        "episode_id": "diagnosis",
        "description": f"Inject {fault_type} on {target.device}",
        "fault_type": fault_type,
        "target_device": target.device,
        "duration_seconds": fault_duration,
        "stabilization_time": fault_stabilization,
        "metadata": {"severity": template.get("severity", "medium")},
    }
    if target.interface:
        episode_fault["target_interface"] = target.interface
    if target.prefix:
        episode_fault["target_prefix"] = target.prefix
    if target.mtu:
        episode_fault["mtu"] = target.mtu
    episode_fault["metadata"].update(extra_episode_metadata)

    return {
        "schema_version": "1",
        "scenario_id": scenario_id,
        "name": scenario_name,
        "description": template.get(
            "description",
            f"Auto-generated {fault_type} localization scenario for {topo.scale} topology.",
        ),
        "topology_scale": topo.scale,
        "traffic_profile": "standard",
        "metadata": {
            "difficulty": difficulty,
            "generator": {
                "seed": defaults.get("seed"),
                "topology_dir": str(topo.topology_dir),
                "template": template.get("name", fault_type),
            },
        },
        "episode": episode_fault,
    }


def _stable_template_seed(seed: int, scale: str, template_name: str) -> int:
    identity = f"{seed}\0{scale}\0{template_name}".encode()
    return int.from_bytes(hashlib.sha256(identity).digest()[:8], "big")


def _ordered_template_devices(
    role: str,
    topo: TopologyContext,
    rng: random.Random,
) -> list[str]:
    candidates = device_candidates(role, topo)
    by_name = {device.name: device for device in topo.manifest.devices}
    pod_groups: dict[int, list[str]] = {}
    without_pod: list[str] = []
    for name in candidates:
        pod = (by_name[name].metadata or {}).get("pod")
        if pod is None:
            without_pod.append(name)
        else:
            pod_groups.setdefault(int(pod), []).append(name)

    if not pod_groups:
        ordered = list(candidates)
        rng.shuffle(ordered)
        return ordered

    pod_order = sorted(pod_groups)
    rng.shuffle(pod_order)
    for names in pod_groups.values():
        rng.shuffle(names)
    rng.shuffle(without_pod)

    ordered: list[str] = []
    while any(pod_groups.values()):
        for pod in pod_order:
            if pod_groups[pod]:
                ordered.append(pod_groups[pod].pop())
    ordered.extend(without_pod)
    return ordered


def generate(spec: dict[str, Any], topo: TopologyContext, out_dir: Path, seed: int) -> list[Path]:
    _validate_traffic_profiles(spec)
    defaults = dict(spec.get("defaults", {}))
    defaults["seed"] = seed
    default_count = int(defaults.get("count_per_fault", 5))

    out_dir.mkdir(parents=True, exist_ok=True)
    generated: list[Path] = []
    next_index_by_fault: dict[str, int] = {}

    for template in spec.get("fault_templates", []):
        fault_type = template["fault_type"]
        difficulty = template.get("difficulty", "medium")
        count = count_for_scale(template, topo.scale, default_count)
        if count <= 0:
            continue

        template_name = str(template.get("name") or fault_type)
        rng = random.Random(_stable_template_seed(seed, topo.scale, template_name))
        target_devices: list[str | None]
        if fault_type == "none":
            target_devices = [None] * count
        else:
            # Static-route faults intentionally remain access/origin scoped to
            # match the current handler's device-local route semantics.
            role = "leaf" if fault_type == "static_route_misconfig" else str(
                template.get("device_role") or "leaf"
            )
            ordered_devices = _ordered_template_devices(role, topo, rng)
            if count > len(ordered_devices):
                raise ValueError(
                    f"Template {template_name} requests {count} unique {role} devices, "
                    f"but topology {topo.scale} has only {len(ordered_devices)}"
                )
            target_devices = ordered_devices[:count]

        for target_device in target_devices:
            idx = next_index_by_fault.get(fault_type, 0) + 1
            next_index_by_fault[fault_type] = idx
            payload = build_fault_instance(
                fault_type=fault_type,
                difficulty=difficulty,
                topo=topo,
                rng=rng,
                defaults=defaults,
                template=template,
                idx=idx,
                target_device=target_device,
            )
            scenario = ScenarioSpec.model_validate(payload)
            out_path = out_dir / f"{payload['scenario_id']}.yaml"
            with out_path.open("w", encoding="utf-8") as f:
                yaml.safe_dump(
                    scenario.model_dump(mode="json", exclude_none=True),
                    f,
                    sort_keys=False,
                    allow_unicode=False,
                )
            generated.append(out_path)

    return generated


def cleanup_existing_outputs(out_dir: Path) -> int:
    if not out_dir.exists():
        return 0

    removed = 0
    for pattern in ("*.yaml", "*.yml"):
        for existing in out_dir.glob(pattern):
            if existing.is_file():
                existing.unlink()
                removed += 1
    return removed
