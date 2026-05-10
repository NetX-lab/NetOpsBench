#!/usr/bin/env python3
"""Generate standardized scenario YAML files from topology metadata."""

from __future__ import annotations

import argparse
import ipaddress
import json
import random
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[3]


@dataclass
class TopologyContext:
    scale: str
    topology_dir: Path
    metadata: dict[str, Any]
    spines: list[str]
    leafs: list[str]
    clients: list[dict[str, Any]]
    device_interfaces: dict[str, list[str]]
    leaf_interface_roles: dict[str, dict[str, list[str]]]
    client_interfaces: dict[str, list[str]]
    device_asns: dict[str, int] = field(default_factory=dict)
    bgp_neighbors: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    bgp_networks: dict[str, list[dict[str, Any]]] = field(default_factory=dict)


def load_yaml(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def load_topology(scale: str, topology_dir: str | None) -> TopologyContext:
    if topology_dir:
        topo_dir = Path(topology_dir)
    else:
        topo_dir = ROOT / "lab-topology" / f"generated_topology_{scale}"

    metadata_path = topo_dir / "topology.json"
    if not metadata_path.exists():
        raise FileNotFoundError(f"Topology metadata not found: {metadata_path}")

    with metadata_path.open("r", encoding="utf-8") as f:
        metadata = json.load(f)

    devices = metadata.get("devices", {})
    spines = [d["name"] for d in devices.get("spines", [])]
    leafs = [d["name"] for d in devices.get("leafs", [])]
    clients = list(devices.get("clients", []))

    device_interfaces: dict[str, list[str]] = {}
    leaf_interface_roles: dict[str, dict[str, list[str]]] = {}
    client_interfaces: dict[str, list[str]] = {}
    device_asns: dict[str, int] = {}
    bgp_neighbors: dict[str, list[dict[str, Any]]] = {}
    bgp_networks: dict[str, list[dict[str, Any]]] = {}

    clab_path = topo_dir / "dcn.clab.yaml"
    if clab_path.exists():
        device_interfaces, leaf_interface_roles, client_interfaces = parse_clab_topology(clab_path)

    config_dir = topo_dir / "configs"
    for device in spines + leafs:
        cfg = config_dir / f"{device}.sh"
        if device not in device_interfaces:
            device_interfaces[device] = parse_network_interfaces(cfg)
        bgp_info = parse_bgp_config(cfg)
        if bgp_info.get("local_as") is not None:
            device_asns[device] = int(bgp_info["local_as"])
        bgp_neighbors[device] = list(bgp_info.get("neighbors") or [])
        bgp_networks[device] = list(bgp_info.get("networks") or [])

    if not leaf_interface_roles:
        leaf_interface_roles = build_leaf_interface_roles(device_interfaces, metadata)

    if not client_interfaces:
        for client in clients:
            client_interfaces[client["name"]] = ["eth1"]

    return TopologyContext(
        scale=scale,
        topology_dir=topo_dir,
        metadata=metadata,
        spines=spines,
        leafs=leafs,
        clients=clients,
        device_interfaces=device_interfaces,
        leaf_interface_roles=leaf_interface_roles,
        client_interfaces=client_interfaces,
        device_asns=device_asns,
        bgp_neighbors=bgp_neighbors,
        bgp_networks=bgp_networks,
    )


def parse_network_interfaces(cfg_path: Path) -> list[str]:
    if not cfg_path.exists():
        return ["Ethernet0"]

    interfaces = set()
    with cfg_path.open("r", encoding="utf-8") as f:
        for raw in f:
            line = raw.strip()
            if line.startswith("config interface startup"):
                parts = line.split()
                if len(parts) >= 4:
                    normalized = normalize_sonic_interface(parts[3])
                    if normalized:
                        interfaces.add(normalized)
            elif line.startswith("config interface ip add"):
                parts = line.split()
                if len(parts) >= 5:
                    normalized = normalize_sonic_interface(parts[4])
                    if normalized:
                        interfaces.add(normalized)

    return sorted(interfaces) if interfaces else ["Ethernet0"]


def parse_bgp_config(cfg_path: Path) -> dict[str, Any]:
    """Parse local ASN, neighbors, and advertised networks from one SONiC config script."""
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


def parse_clab_topology(clab_path: Path):
    data = load_yaml(clab_path)
    topo = data.get("topology", {})
    links = topo.get("links", []) or []

    device_interfaces: dict[str, set] = {}
    leaf_interface_roles: dict[str, dict[str, set]] = {}
    client_interfaces: dict[str, set] = {}

    def add_iface(device: str, iface: str):
        if device.startswith("client"):
            client_interfaces.setdefault(device, set()).add(iface)
        else:
            normalized = normalize_sonic_interface(iface)
            if normalized:
                device_interfaces.setdefault(device, set()).add(normalized)

    def add_leaf_role(leaf: str, role: str, iface: str):
        normalized = normalize_sonic_interface(iface)
        if not normalized:
            return
        leaf_interface_roles.setdefault(leaf, {"uplink": set(), "downlink": set(), "any": set()})
        leaf_interface_roles[leaf][role].add(normalized)
        leaf_interface_roles[leaf]["any"].add(normalized)

    for link in links:
        endpoints = link.get("endpoints", [])
        if len(endpoints) != 2:
            continue
        left = endpoints[0]
        right = endpoints[1]
        if ":" not in left or ":" not in right:
            continue
        dev_a, if_a = left.split(":", 1)
        dev_b, if_b = right.split(":", 1)

        add_iface(dev_a, if_a)
        add_iface(dev_b, if_b)

        if dev_a.startswith("leaf") and dev_b.startswith("spine"):
            add_leaf_role(dev_a, "uplink", if_a)
        if dev_b.startswith("leaf") and dev_a.startswith("spine"):
            add_leaf_role(dev_b, "uplink", if_b)
        if dev_a.startswith("leaf") and dev_b.startswith("client"):
            add_leaf_role(dev_a, "downlink", if_a)
        if dev_b.startswith("leaf") and dev_a.startswith("client"):
            add_leaf_role(dev_b, "downlink", if_b)

    return (
        {k: sorted(v) for k, v in device_interfaces.items()},
        {k: {rk: sorted(rv) for rk, rv in roles.items()} for k, roles in leaf_interface_roles.items()},
        {k: sorted(v) for k, v in client_interfaces.items()},
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


def build_leaf_interface_roles(
    device_interfaces: dict[str, list[str]],
    metadata: dict[str, Any],
) -> dict[str, dict[str, list[str]]]:
    roles: dict[str, dict[str, list[str]]] = {}
    num_spines = int(metadata.get("scale", {}).get("num_spines", 0) or 0)

    for device, interfaces in device_interfaces.items():
        if not device.startswith("leaf"):
            continue
        roles[device] = {"uplink": [], "downlink": [], "any": []}
        for iface in interfaces:
            roles[device]["any"].append(iface)
            if not (iface.startswith("Ethernet") and iface[8:].isdigit()):
                continue
            port_idx = int(iface[8:])
            if port_idx % 4 != 0:
                continue
            eth_idx = (port_idx // 4) + 1
            if num_spines and eth_idx <= num_spines:
                roles[device]["uplink"].append(iface)
            elif num_spines and eth_idx > num_spines:
                roles[device]["downlink"].append(iface)
        for role in ("uplink", "downlink", "any"):
            roles[device][role] = sorted(set(roles[device][role])) or ["Ethernet0"]

    return roles


def count_for_scale(item: dict[str, Any], scale: str, default_count: int) -> int:
    cps = item.get("count_per_scale", {})
    if isinstance(cps, dict) and scale in cps:
        return int(cps[scale])
    return int(item.get("count", default_count))


def pick_device(role: str, topo: TopologyContext, rng: random.Random) -> str:
    if role == "spine":
        return rng.choice(topo.spines)
    if role == "leaf":
        return rng.choice(topo.leafs)
    if role == "client":
        return rng.choice([c["name"] for c in topo.clients])
    raise ValueError(f"Unknown role: {role}")


def pick_network_interface(device: str, topo: TopologyContext, rng: random.Random) -> str:
    candidates = topo.device_interfaces.get(device) or ["Ethernet0"]
    normalized = [normalize_sonic_interface(c) for c in candidates if normalize_sonic_interface(c)]
    return rng.choice(normalized) if normalized else "Ethernet0"


def pick_bgp_neighbor(device: str, topo: TopologyContext, rng: random.Random) -> dict[str, Any]:
    candidates = topo.bgp_neighbors.get(device) or []
    if not candidates:
        raise ValueError(f"Device {device} has no parsed BGP neighbors")
    return dict(rng.choice(candidates))


def pick_advertised_network(device: str, topo: TopologyContext, rng: random.Random) -> dict[str, Any]:
    candidates = topo.bgp_networks.get(device) or []
    if not candidates:
        raise ValueError(f"Device {device} has no parsed BGP networks")
    return dict(rng.choice(candidates))


def _pick_client_for_route_fault(
    topo: TopologyContext,
    rng: random.Random,
    excluded_leaf: str | None = None,
) -> dict[str, Any]:
    # Prefer remote leaf destinations to avoid selecting a local directly-connected subnet
    # on the same leaf where the route fault is injected.
    candidates = list(topo.clients)
    if excluded_leaf:
        remote = [c for c in candidates if c.get("leaf") != excluded_leaf]
        if remote:
            candidates = remote
    return rng.choice(candidates)


def pick_client_subnet(
    topo: TopologyContext,
    rng: random.Random,
    prefix_len: int = 30,
    excluded_leaf: str | None = None,
) -> str:
    client = _pick_client_for_route_fault(topo, rng, excluded_leaf=excluded_leaf)
    ip_str = str(client.get("data_ip") or "").strip()
    if not ip_str:
        raise ValueError(f"Client {client.get('name', 'unknown')} missing data_ip in topology metadata")
    ip_obj = ipaddress.ip_address(ip_str)
    net = ipaddress.ip_network(f"{ip_obj}/{prefix_len}", strict=False)
    return str(net)


def pick_client_host_route(
    topo: TopologyContext,
    rng: random.Random,
    excluded_leaf: str | None = None,
) -> str:
    client = _pick_client_for_route_fault(topo, rng, excluded_leaf=excluded_leaf)
    ip_str = str(client.get("data_ip") or "").strip()
    if not ip_str:
        raise ValueError(f"Client {client.get('name', 'unknown')} missing data_ip in topology metadata")
    return f"{ip_str}/32"


def pick_link_down_interface(device: str, topo: TopologyContext, rng: random.Random) -> str:
    candidates = topo.device_interfaces.get(device) or ["Ethernet0"]
    candidates = [normalize_sonic_interface(c) for c in candidates if normalize_sonic_interface(c)]
    if not candidates:
        candidates = ["Ethernet0"]
    if device.startswith("leaf"):
        roles = topo.leaf_interface_roles.get(device)
        if roles and roles.get("downlink"):
            return rng.choice(roles["downlink"])
    # On leafs, prefer client-facing links to create visible impact.
    if device.startswith("leaf"):
        num_spines = int(topo.metadata.get("scale", {}).get("num_spines", 0) or 0)
        client_facing = []
        for iface in candidates:
            if iface.startswith("Ethernet") and iface[8:].isdigit():
                port_idx = int(iface[8:])
                if port_idx % 4 != 0:
                    continue
                eth_idx = (port_idx // 4) + 1
                if num_spines and eth_idx > num_spines:
                    client_facing.append(iface)
        if client_facing:
            return rng.choice(client_facing)
    return rng.choice(candidates)


def pick_leaf_interface(device: str, topo: TopologyContext, rng: random.Random, role: str) -> str:
    roles = topo.leaf_interface_roles.get(device) or {}
    role = (role or "any").lower()
    pool = roles.get(role) or roles.get("any")
    if pool:
        return rng.choice(pool)
    return pick_network_interface(device, topo, rng)


def pick_client_interface(device: str, topo: TopologyContext, rng: random.Random) -> str:
    candidates = topo.client_interfaces.get(device)
    if candidates:
        return rng.choice(candidates)
    return "eth1"


def build_fault_instance(
    fault_type: str,
    difficulty: str,
    topo: TopologyContext,
    rng: random.Random,
    defaults: dict[str, Any],
    template: dict[str, Any],
    idx: int,
) -> dict[str, Any]:
    # Negative sample: healthy network, no fault injected.
    if fault_type == "none":
        baseline_duration = int(defaults.get("baseline_duration_seconds", 20))
        recovery_duration = int(defaults.get("recovery_duration_seconds", 20))
        baseline_stabilization = int(defaults.get("baseline_stabilization_seconds", 5))
        scenario_id = f"generated_healthy_network_{topo.scale}_{idx:03d}"
        return {
            "scenario_id": scenario_id,
            "name": f"Generated healthy network case #{idx} ({topo.scale})",
            "description": f"Auto-generated negative sample — no fault injected ({topo.scale} topology).",
            "topology_scale": topo.scale,
            "traffic_profile": template.get("traffic_profile", defaults.get("traffic_profile", "standard")),
            "metadata": {
                "difficulty": difficulty,
                "negative_sample": True,
                "generator": {
                    "seed": defaults.get("seed"),
                    "topology_dir": str(topo.topology_dir),
                    "template": template.get("name", "healthy_network"),
                },
            },
            "episodes": [
                {
                    "episode_id": "ep001_observation_1",
                    "description": "Observe healthy network — no faults",
                    "fault_type": "none",
                    "duration_seconds": baseline_duration,
                    "stabilization_time": baseline_stabilization,
                },
                {
                    "episode_id": "ep002_observation_2",
                    "description": "Continue observing healthy network",
                    "fault_type": "none",
                    "duration_seconds": baseline_duration,
                    "stabilization_time": baseline_stabilization,
                },
                {
                    "episode_id": "ep003_observation_3",
                    "description": "Final healthy network observation",
                    "fault_type": "none",
                    "duration_seconds": recovery_duration,
                    "stabilization_time": baseline_stabilization,
                },
            ],
        }

    # Merge nested ``parameters`` dict into the top-level template so that
    # campaign-level overrides like ``parameters: {misconfig_kind: ...}``
    # are visible to ``template.get("misconfig_kind")`` lookups below.
    if "parameters" in template:
        template = {**template, **template["parameters"]}
    device_role = template.get("device_role", "leaf")
    target_device = pick_device(device_role, topo, rng)
    target_interface = None
    extra_episode_metadata: dict[str, Any] = {}
    target_prefix = None
    mtu = None

    if template.get("variant"):
        extra_episode_metadata["variant"] = template["variant"]
    if template.get("recovery_mode"):
        extra_episode_metadata["recovery_mode"] = template["recovery_mode"]

    if fault_type == "link_down":
        target_interface = pick_link_down_interface(target_device, topo, rng)
    elif fault_type in {"link_flapping", "mtu_mismatch", "high_latency"}:
        if target_device.startswith("leaf"):
            interface_role = template.get("interface_role")
            if not interface_role and fault_type in {"high_latency", "mtu_mismatch"}:
                interface_role = "uplink"
            target_interface = pick_leaf_interface(target_device, topo, rng, interface_role or "any")
        else:
            target_interface = pick_network_interface(target_device, topo, rng)
    elif fault_type in {"packet_loss", "packet_corruption"}:
        if target_device.startswith("client"):
            target_interface = pick_client_interface(target_device, topo, rng)
        elif target_device.startswith("leaf"):
            interface_role = template.get("interface_role") or "uplink"
            target_interface = pick_leaf_interface(target_device, topo, rng, interface_role)
        else:
            target_interface = pick_network_interface(target_device, topo, rng)
    elif fault_type == "blackhole_route":
        target_prefix = pick_client_subnet(topo, rng, prefix_len=30, excluded_leaf=target_device)
    elif fault_type == "static_route_misconfig":
        target_device = pick_device("leaf", topo, rng)
        extra_episode_metadata["target_ip"] = pick_client_host_route(
            topo,
            rng,
            excluded_leaf=target_device,
        )
        extra_episode_metadata["wrong_nexthop"] = "auto"
    elif fault_type == "bgp_neighbor_misconfig":
        target_device = pick_device(device_role, topo, rng)
        neighbor = pick_bgp_neighbor(target_device, topo, rng)
        extra_episode_metadata["peer_ip"] = neighbor["peer_ip"]
        if neighbor.get("remote_as") is not None:
            extra_episode_metadata["original_remote_as"] = int(neighbor["remote_as"])
        extra_episode_metadata["misconfig_kind"] = template.get("misconfig_kind", "peer_as_mismatch")
    elif fault_type == "route_policy_misconfig":
        target_device = pick_device(device_role, topo, rng)
        network = pick_advertised_network(target_device, topo, rng)
        target_prefix = network["prefix"]
        if network.get("route_map"):
            extra_episode_metadata["route_map"] = network["route_map"]
        extra_episode_metadata["misconfig_kind"] = template.get("misconfig_kind", "network_statement_missing")
    elif fault_type == "acl_misconfig":
        target_device = pick_device(device_role, topo, rng)
        network = pick_advertised_network(target_device, topo, rng)
        target_prefix = network["prefix"]
        if target_device.startswith("leaf"):
            target_interface = pick_leaf_interface(target_device, topo, rng, "uplink")
        else:
            target_interface = pick_network_interface(target_device, topo, rng)
        extra_episode_metadata["direction"] = template.get("direction", "in")
    if fault_type == "link_flapping":
        extra_episode_metadata["iterations"] = int(template.get("iterations", 6))
        extra_episode_metadata["down_time"] = int(template.get("down_time", 2))
        extra_episode_metadata["up_time"] = int(template.get("up_time", 3))
    elif fault_type == "packet_loss":
        extra_episode_metadata["loss_pct"] = int(template.get("loss_pct", 20))
    elif fault_type == "packet_corruption":
        extra_episode_metadata["corruption_pct"] = int(template.get("corruption_pct", 20))
    elif fault_type == "high_latency":
        extra_episode_metadata["latency_ms"] = int(template.get("latency_ms", 100))
    elif fault_type == "mtu_mismatch":
        mtu = int(template.get("mtu", 1400))

    expected_location = {"device": target_device}
    if target_interface:
        if target_device.startswith("client"):
            expected_location["interface"] = target_interface
        else:
            expected_location["interface"] = normalize_sonic_interface(target_interface)

    scenario_id = f"generated_{fault_type}_{topo.scale}_{idx:03d}"
    scenario_name = f"Generated {fault_type} case #{idx} ({topo.scale})"

    baseline_duration = int(defaults.get("baseline_duration_seconds", 20))
    fault_duration = int(defaults.get("fault_duration_seconds", 30))
    recovery_duration = int(defaults.get("recovery_duration_seconds", 20))
    baseline_stabilization = int(defaults.get("baseline_stabilization_seconds", 5))
    fault_stabilization = int(
        template.get("fault_stabilization_seconds", defaults.get("fault_stabilization_seconds", 5))
    )
    recovery_stabilization = int(defaults.get("recovery_stabilization_seconds", 10))

    episode_fault = {
        "episode_id": "ep002_fault",
        "description": f"Inject {fault_type} on {target_device}",
        "fault_type": fault_type,
        "target_device": target_device,
        "duration_seconds": fault_duration,
        "stabilization_time": fault_stabilization,
        "metadata": {"severity": template.get("severity", "medium")},
    }
    if target_interface:
        episode_fault["target_interface"] = target_interface
    if target_prefix:
        episode_fault["target_prefix"] = target_prefix
    if mtu:
        episode_fault["mtu"] = mtu
    episode_fault["metadata"].update(extra_episode_metadata)

    return {
        "scenario_id": scenario_id,
        "name": scenario_name,
        "description": template.get(
            "description",
            f"Auto-generated {fault_type} localization scenario for {topo.scale} topology.",
        ),
        "topology_scale": topo.scale,
        "traffic_profile": template.get("traffic_profile", defaults.get("traffic_profile", "standard")),
        "metadata": {
            "difficulty": difficulty,
            "expected_diagnosis": fault_type,
            "expected_location": expected_location,
            "generator": {
                "seed": defaults.get("seed"),
                "topology_dir": str(topo.topology_dir),
                "template": template.get("name", fault_type),
            },
        },
        "episodes": [
            {
                "episode_id": "ep001_baseline",
                "description": "Establish baseline - no faults",
                "fault_type": "none",
                "target_device": target_device,
                "duration_seconds": baseline_duration,
                "stabilization_time": baseline_stabilization,
            },
            episode_fault,
            {
                "episode_id": "ep003_recovery_verify",
                "description": "Verify recovery after fault removal",
                "fault_type": "none",
                "target_device": target_device,
                "duration_seconds": recovery_duration,
                "stabilization_time": recovery_stabilization,
            },
        ],
    }


def generate(spec: dict[str, Any], topo: TopologyContext, out_dir: Path, seed: int) -> list[Path]:
    rng = random.Random(seed)
    defaults = dict(spec.get("defaults", {}))
    defaults["seed"] = seed
    default_count = int(defaults.get("count_per_fault", 5))

    out_dir.mkdir(parents=True, exist_ok=True)
    generated: list[Path] = []

    for template in spec.get("fault_templates", []):
        fault_type = template["fault_type"]
        difficulty = template.get("difficulty", "medium")
        count = count_for_scale(template, topo.scale, default_count)
        for idx in range(1, count + 1):
            payload = build_fault_instance(
                fault_type=fault_type,
                difficulty=difficulty,
                topo=topo,
                rng=rng,
                defaults=defaults,
                template=template,
                idx=idx,
            )
            out_path = out_dir / f"{payload['scenario_id']}.yaml"
            with out_path.open("w", encoding="utf-8") as f:
                yaml.safe_dump(payload, f, sort_keys=False, allow_unicode=False)
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


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generate randomized NetOpsBench scenarios")
    parser.add_argument(
        "--spec",
        default="scenarios/specs/fault_campaign.yaml",
        help="Scenario generation spec file",
    )
    parser.add_argument(
        "--scale",
        required=True,
        choices=["xs", "small", "medium", "large"],
        help="Topology scale to generate scenarios for",
    )
    parser.add_argument(
        "--topology-dir",
        help="Override topology directory (default: lab-topology/generated_topology_<scale>)",
    )
    parser.add_argument(
        "--out",
        help="Output directory (default: scenarios/generated/<scale>)",
    )
    parser.add_argument(
        "--seed",
        type=int,
        default=42,
        help="Random seed for reproducible generation",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    spec_path = Path(args.spec)
    spec = load_yaml(spec_path)
    topo = load_topology(args.scale, args.topology_dir)

    out_dir = Path(args.out) if args.out else ROOT / "scenarios" / "generated" / args.scale
    removed = cleanup_existing_outputs(out_dir)
    if removed:
        print(f"Removed {removed} existing scenario file(s) under: {out_dir}")
    generated = generate(spec, topo, out_dir, args.seed)

    print(f"Generated {len(generated)} scenario file(s) under: {out_dir}")
    for path in generated[:8]:
        print(f"  - {path}")
    if len(generated) > 8:
        print(f"  ... and {len(generated) - 8} more")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
