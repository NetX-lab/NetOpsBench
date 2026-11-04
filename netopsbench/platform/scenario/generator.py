"""Generate standardized scenario YAML files from topology metadata."""

from __future__ import annotations

import hashlib
import ipaddress
import random
from dataclasses import dataclass
from importlib.resources import files
from pathlib import Path
from typing import Any

import yaml

from netopsbench.models.profiles import ScaleRegistry, default_scale_registry
from netopsbench.models.scenario import ScenarioSpec
from netopsbench.models.topology import TopologyManifest
from netopsbench.platform.faults.defaults import IMPAIRMENT_PARAMETERS
from netopsbench.platform.topology.configdb_payload import (
    interface_names_from_payload,
    interface_networks_from_payload,
    load_configdb_payload,
)
from netopsbench.platform.topology.topology_utils import load_topology_manifest
from netopsbench.platform.utils.frr import parse_bgp_config

from .candidates import CandidatePaths


def default_campaign_spec() -> Path:
    resource = files("netopsbench.platform.scenario").joinpath("specs", "fault_campaign.yaml")
    if not resource.is_file():
        raise FileNotFoundError("Packaged default scenario campaign is missing")
    return Path(str(resource))


@dataclass
class TopologyContext:
    manifest: TopologyManifest
    bgp_neighbors: dict[str, list[dict[str, Any]]]

    @property
    def scale(self) -> str:
        return self.manifest.scale


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
    bgp_neighbors: dict[str, list[dict[str, Any]]] = {}

    config_dir = topo_dir / "configs"
    switch_devices = [device.name for device in manifest.switches()]
    for device in switch_devices:
        cfg = config_dir / "sonic" / device / "config_db.json"
        frr_cfg = config_dir / "frr" / f"{device}.conf"
        if not cfg.is_file():
            raise FileNotFoundError(f"SONiC ConfigDB artifact not found: {cfg}")
        if not frr_cfg.is_file():
            raise FileNotFoundError(f"FRR artifact not found: {frr_cfg}")
        configdb = load_configdb_payload(cfg)
        if not interface_names_from_payload(configdb):
            raise ValueError(f"SONiC ConfigDB has no interfaces: {cfg}")
        bgp_info = parse_bgp_config(frr_cfg.read_text())
        if bgp_info.get("local_as") is None:
            raise ValueError(f"FRR artifact has no BGP router stanza: {frr_cfg}")
        neighbors = list(bgp_info.get("neighbors") or [])
        interface_networks = interface_networks_from_payload(configdb)
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
        bgp_neighbors[device] = neighbors

    return TopologyContext(manifest=manifest, bgp_neighbors=bgp_neighbors)


@dataclass
class _Placement:
    device: str
    interface: str | None
    peer: str | None
    prefix: str
    parameters: dict[str, Any]
    witnesses: list[dict[str, str]]
    controls: list[dict[str, str]]


class _CandidateIndex(CandidatePaths):
    """Shortest-path DAG membership, without enumerating complete paths."""

    def __init__(self, topo: TopologyContext, seed: int):
        from collections import Counter

        super().__init__(topo.manifest)
        self.topo = topo
        random.Random(seed).shuffle(self.pairs)
        self.device_uses, self.pod_uses, self.link_uses = Counter(), Counter(), Counter()
        self.seed = seed

    def _roots(self, unit):
        candidates = [d for d in self.topo.manifest.switches() if d.role.value == unit.role]

        # Prefer unused pods, devices and low/middle/high number ranges. High
        # numbered nodes are eligible from the first cycle, including K18 >155.
        def key(d):
            pod = d.metadata.get("pod")
            rank = next(i for i, item in enumerate(candidates) if item.name == d.name)
            bucket = min(2, rank * 3 // max(1, len(candidates)))
            return (
                self.pod_uses[unit.role, pod] if pod is not None else 0,
                self.device_uses[d.name],
                self.pod_uses[unit.role, "bucket", bucket],
                _stable_template_seed(self.seed, self.topo.scale, unit.id + d.name),
            )

        return sorted(candidates, key=key)

    def place(self, unit):
        fault = unit.fault_type
        for device in self._roots(unit):
            root = device.name
            peers = [None]
            if unit.position:
                peer_role = unit.position.split("-")[-1]
                peers = [
                    peer for (node, peer) in self.ports if node == root and self.devices[peer].role.value == peer_role
                ]
                peers.sort(
                    key=lambda peer: (
                        self.link_uses[frozenset((root, peer))],
                        _stable_template_seed(self.seed, self.topo.scale, unit.id + peer),
                    )
                )
            for peer in peers:
                found = []
                parameters = {}
                for src, dst in self.pairs:
                    source, target = self.clients[src], self.clients[dst]
                    reverse = fault in {"routing_loop", "route_flapping"} or unit.mechanism == "export_filter"
                    member = self.contains(src, dst, root, peer) if not reverse else self.contains(src, dst, peer, root)
                    if not member:
                        continue
                    if fault == "route_origination_misconfig" or unit.scope == "destination":
                        if target.attached_switch != root:
                            continue
                    if unit.scope == "source" or fault == "static_route_misconfig":
                        if source.attached_switch != root or target.attached_switch == root:
                            continue
                    if fault in {"ecmp_misconfig", "route_filter_misconfig"} and not reverse:
                        hops = [
                            neighbor
                            for neighbor in self.adj[root]
                            if self.distance(target.attached_switch)[neighbor] + 1
                            == self.distance(target.attached_switch)[root]
                        ]
                        if not hops:
                            continue
                        if fault == "ecmp_misconfig" or unit.scope in {"reduced", "reduced_remote_pod"}:
                            if len(hops) < 2:
                                continue
                        if unit.role == "agg" and (fault == "ecmp_misconfig" or unit.scope == "reduced_remote_pod"):
                            if device.metadata.get("pod") == self.devices[target.attached_switch].metadata.get("pod"):
                                continue
                    else:
                        hops = []
                    prefix = str(ipaddress.ip_network(f"{target.data_ip}/30", strict=False))
                    parameters = {"mechanism": unit.mechanism} if unit.mechanism else {}
                    if (
                        fault.startswith("bgp_")
                        and fault != "bgp_process_down"
                        or fault == "route_flapping"
                        or unit.mechanism == "export_filter"
                    ):
                        parameters["peer_ip"] = self._peer_ip(root, peer)
                    if unit.mechanism == "import_filter":
                        chosen = self.adj[root] if unit.scope == "missing" else hops[:1]
                        parameters["peer_ips"] = [self._peer_ip(root, neighbor) for neighbor in chosen]
                    if fault == "static_route_misconfig":
                        local = [c for c in self.clients.values() if c.attached_switch == root and c.name != src]
                        if not local:
                            continue
                        parameters.update(target_ip=f"{target.data_ip}/32", wrong_nexthop=local[0].data_ip)
                        prefix = f"{target.data_ip}/32"
                    if fault == "blackhole_route":
                        prefix = f"{target.data_ip}/32"
                    if fault == "routing_loop":
                        parameters["next_hop"] = self._peer_ip(root, peer)
                        prefix = f"{target.data_ip}/32"
                    if fault.startswith("acl_"):
                        parameters["direction"] = "out"
                        if fault == "acl_source_misconfig":
                            parameters["source_prefix"] = f"{source.data_ip}/32"
                        if fault == "acl_l4_port_misconfig":
                            parameters["destination_port"] = 33434
                    parameters.update(IMPAIRMENT_PARAMETERS.get(fault, {}))
                    found.append({"src": src, "dst": dst})
                    break
                if not found:
                    continue
                affected = found[0]
                found = self.affected_pairs(fault, root, peer, affected, reverse=reverse)
                # Choose controls from the same immutable plan, based on the
                # scope of this configuration rather than synthetic extra traffic.
                controls = []
                for src, dst in self.pairs:
                    if {"src": src, "dst": dst} == affected:
                        continue
                    if fault == "acl_source_misconfig":
                        eligible = affected["src"] not in {src, dst} and self.contains(src, dst, root, peer)
                    elif fault == "acl_destination_misconfig":
                        eligible = dst != affected["dst"] and src != affected["dst"]
                    elif fault in {
                        "route_origination_misconfig",
                        "route_filter_misconfig",
                        "routing_loop",
                        "blackhole_route",
                        "static_route_misconfig",
                        "route_flapping",
                    }:
                        eligible = dst != affected["dst"] and src != affected["dst"]
                    elif fault in {"device_down", "bgp_process_down", "ecmp_misconfig"}:
                        eligible = not self.contains(src, dst, root)
                    else:
                        eligible = not self.contains(src, dst, root, peer)
                        if peer and peer not in self.clients:
                            eligible = eligible and not self.contains(src, dst, peer, root)
                        if peer in self.clients:
                            eligible = eligible and peer not in {src, dst}
                    if eligible:
                        controls.append({"src": src, "dst": dst})
                        break
                if not controls:
                    if self.topo.manifest.purpose != "validation":
                        continue
                    # XS has only two directed flows; the reverse is a comparison,
                    # not proof of an unaffected path. XS is validation only.
                    controls = [{"src": affected["dst"], "dst": affected["src"]}]
                self.device_uses[root] += 1
                self.pod_uses[unit.role, device.metadata.get("pod")] += 1
                candidates = [d for d in self.topo.manifest.switches() if d.role.value == unit.role]
                rank = next(i for i, d in enumerate(candidates) if d.name == root)
                self.pod_uses[unit.role, "bucket", min(2, rank * 3 // len(candidates))] += 1
                if peer:
                    self.link_uses[frozenset((root, peer))] += 1
                return _Placement(root, self.ports.get((root, peer)), peer, prefix, parameters, found, controls)
        raise ValueError(f"No planned candidate for necessary coverage unit {unit.id} in {self.topo.scale}")

    def _peer_ip(self, root, peer):
        interface = self.ports.get((root, peer))
        neighbors = self.topo.bgp_neighbors.get(root, [])
        match = next((n for n in neighbors if n.get("interface") == interface), None)
        if not match:
            raise ValueError(f"No configured neighbor for {root}->{peer}")
        return match["peer_ip"]


def _stable_template_seed(seed: int, scale: str, template_name: str) -> int:
    return int.from_bytes(hashlib.sha256(f"{seed}\0{scale}\0{template_name}".encode()).digest()[:8], "big")


def generate(
    spec: dict[str, Any],
    topo: TopologyContext,
    out_dir: Path,
    seed: int,
    *,
    scale_registry: ScaleRegistry | None = None,
) -> list[Path]:
    import json

    from netopsbench.platform.faults.catalog import FAULT_CATALOG_VERSION, coverage_units

    if spec.get("fault_catalog_version") != FAULT_CATALOG_VERSION:
        raise ValueError("Obsolete campaign spec; use fault catalog version 2 and regenerate")
    defaults = spec.get("defaults", {})
    if "fault_duration_seconds" in defaults:
        raise ValueError("Configure experiment.observation_seconds in scale profiles and regenerate scenarios")
    from netopsbench.models.recovery import RecoveryPolicy

    recovery_policy = RecoveryPolicy.model_validate(spec.get("recovery_policy", {}))
    policy = (scale_registry or default_scale_registry()).get(topo.scale).experiment
    duration = policy.window_seconds(topo.manifest.require_pingmesh_plan().settings.complete_window_seconds)
    if defaults.get("traffic_profile", "standard") != "standard":
        raise ValueError("Only standard traffic is supported")
    validation = topo.manifest.purpose == "validation"
    units = coverage_units(topo.manifest.family, validation=validation)
    index = _CandidateIndex(topo, seed)
    out_dir.mkdir(parents=True, exist_ok=True)
    rows, paths = [], []
    fault_indices = {}
    for unit in units:
        placement = index.place(unit)
        rng = random.Random(_stable_template_seed(seed, topo.scale, unit.id))
        if unit.fault_type == "interface_ip_misconfig":
            placement.parameters["fault_address"] = f"198.51.100.{rng.randrange(64) * 4 + 1}/30"
        if unit.fault_type == "bgp_update_source_mismatch":
            placement.parameters["fault_update_source"] = f"Loopback{rng.randrange(1, 4096)}"
        placement.parameters.update(affected_candidates=placement.witnesses, control_candidates=placement.controls)
        if unit.scope:
            placement.parameters["impact_scope"] = unit.scope
        if unit.fault_type in {"link_flapping", "route_flapping"}:
            placement.parameters.update(down_time=policy.flapping_down_seconds, up_time=policy.flapping_up_seconds)
        fault_indices[unit.fault_type] = fault_indices.get(unit.fault_type, 0) + 1
        scenario_id = f"generated_{unit.fault_type}_{topo.scale}_{fault_indices[unit.fault_type]:03d}"
        episode = {
            "episode_id": "diagnosis",
            "fault_type": unit.fault_type,
            "target_device": placement.device,
            "duration_seconds": duration,
            "stabilization_time": 0,
            "parameters": placement.parameters,
        }
        if placement.interface:
            episode["target_interface"] = placement.interface
        if unit.fault_type in {
            "routing_loop",
            "route_origination_misconfig",
            "route_filter_misconfig",
            "route_flapping",
            "ecmp_misconfig",
            "blackhole_route",
            "static_route_misconfig",
            "acl_source_misconfig",
            "acl_destination_misconfig",
            "acl_l4_port_misconfig",
        }:
            episode["target_prefix"] = placement.prefix
        payload = {
            "schema_version": "1",
            "scenario_id": scenario_id,
            "name": scenario_id,
            "topology_scale": topo.scale,
            "traffic_profile": "standard",
            "metadata": {
                "fault_catalog_version": FAULT_CATALOG_VERSION,
                "difficulty": "medium",
                "coverage_unit": unit.id,
                "purpose": "validation" if validation else "benchmark",
                "experiment": policy.model_dump(mode="json"),
                "recovery_policy": recovery_policy.model_dump(mode="json"),
                "generator": {"seed": seed, "version": 3},
            },
            "episode": episode,
        }
        scenario = ScenarioSpec.model_validate(payload)
        path = out_dir / f"{scenario_id}.yaml"
        path.write_text(yaml.safe_dump(scenario.model_dump(mode="json", exclude_none=True), sort_keys=False))
        paths.append(path)
        rows.append(
            {
                "scenario_id": scenario_id,
                "unit": unit.id,
                "fault_type": unit.fault_type,
                "mechanism": unit.mechanism or unit.fault_type,
                "root": placement.device,
                "role": unit.role,
                "pod": index.devices[placement.device].metadata.get("pod"),
                "interface": placement.interface,
                "neighbor": placement.peer,
                "prefix": placement.prefix,
                "affected_candidates": placement.witnesses,
                "control_candidates": placement.controls,
            }
        )
    for number in range(1, (1 if validation else 4) + 1):
        scenario_id = f"generated_healthy_network_{topo.scale}_{number:03d}"
        payload = {
            "schema_version": "1",
            "scenario_id": scenario_id,
            "name": scenario_id,
            "topology_scale": topo.scale,
            "metadata": {
                "negative_sample": True,
                "generator": {"seed": seed, "version": 3},
                "difficulty": "medium",
                "fault_catalog_version": FAULT_CATALOG_VERSION,
                "purpose": "validation" if validation else "benchmark",
                "experiment": policy.model_dump(mode="json"),
                "recovery_policy": recovery_policy.model_dump(mode="json"),
            },
            "episode": {
                "episode_id": "diagnosis",
                "fault_type": "none",
                "stabilization_time": 0,
                "duration_seconds": duration,
            },
        }
        scenario = ScenarioSpec.model_validate(payload)
        path = out_dir / f"{scenario_id}.yaml"
        path.write_text(yaml.safe_dump(scenario.model_dump(mode="json", exclude_none=True), sort_keys=False))
        paths.append(path)
    matrix = {
        "fault_catalog_version": FAULT_CATALOG_VERSION,
        "scale": topo.scale,
        "seed": seed,
        "plan_id": topo.manifest.require_pingmesh_plan().settings.plan_id,
        "coverage_units": rows,
        "positive_cases": len(rows),
        "negative_cases": 1 if validation else 4,
    }
    (out_dir / "coverage.json").write_text(json.dumps(matrix, indent=2) + "\n")
    return paths


def cleanup_existing_outputs(out_dir: Path) -> int:
    if not out_dir.exists():
        return 0
    paths = [p for pattern in ("*.yaml", "*.yml", "coverage.json") for p in out_dir.glob(pattern) if p.is_file()]
    for path in paths:
        path.unlink()
    return len(paths)
