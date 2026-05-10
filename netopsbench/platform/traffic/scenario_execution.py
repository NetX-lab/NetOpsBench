"""Traffic execution helpers for scenario execution."""

from __future__ import annotations

import json
from pathlib import Path

from netopsbench.logging_utils import get_logger
from netopsbench.platform.topology.topology_utils import clab_container_name
from netopsbench.platform.traffic.controller import TrafficController, TrafficFlow
from netopsbench.platform.traffic.generator import generate_traffic_config, validate_traffic_config
from netopsbench.platform.utils.events import emit as _emit

logger = get_logger(__name__)


def setup_traffic(runner, scale: str, profile: str) -> dict:
    """Generate topology-driven traffic, start the controller, and return the config."""

    _emit(f"\n[Traffic Setup] Generating {profile} traffic for {scale} topology...")

    topology_file = Path(runner.topology_dir) / "topology.json"
    if not topology_file.exists():
        raise FileNotFoundError(f"Topology metadata not found: {topology_file}")

    traffic_config = generate_traffic_config(str(topology_file), scale, profile)
    validate_traffic_config(traffic_config, scale)

    _emit(f"  Total flows: {traffic_config['stats']['total_flows']}")
    _emit(f"  UDP flows: {traffic_config['stats']['udp_flows']}")
    _emit(f"  TCP flows: {traffic_config['stats']['tcp_flows']}")

    estimated_client_pps = traffic_config["stats"].get("estimated_max_pps_per_client")
    estimated_udp_client_pps = traffic_config["stats"].get("estimated_max_udp_pps_per_client")
    switch_pps = traffic_config["stats"].get("estimated_switch_pps", {})
    max_leaf_pps = switch_pps.get("max_leaf_pps", 0.0)
    max_spine_pps = switch_pps.get("max_spine_pps", 0.0)
    switch_limit = traffic_config["profile"].get("switch_pps_limit")
    cross_leaf_flows = traffic_config["stats"].get("cross_leaf_flows", 0)
    intra_leaf_flows = traffic_config["stats"].get("intra_leaf_flows", 0)
    cross_leaf_ratio = traffic_config["stats"].get("cross_leaf_flow_ratio", 0.0)

    if estimated_client_pps is not None:
        _emit(f"  Estimated max PPS per client (all protocols): {estimated_client_pps}")
    if estimated_udp_client_pps is not None:
        _emit(f"  Estimated max UDP PPS per client: {estimated_udp_client_pps}")

    limit_label = "unlimited" if switch_limit in (None, "") else switch_limit
    _emit(f"  Estimated switch PPS limit: {limit_label}")
    _emit(f"  Estimated max leaf PPS: {max_leaf_pps}")
    _emit(f"  Estimated max spine PPS: {max_spine_pps}")
    _emit(
        "  Path mix: "
        f"cross-leaf={cross_leaf_flows}, intra-leaf={intra_leaf_flows}, "
        f"cross-leaf-ratio={cross_leaf_ratio}"
    )

    leafs = switch_pps.get("leafs", {})
    spines = switch_pps.get("spines", {})
    if leafs:
        _emit(f"  Leaf PPS breakdown: {leafs}")
    if spines:
        _emit(f"  Spine PPS breakdown: {spines}")

    with open(topology_file, encoding="utf-8") as f:
        topology = json.load(f)

    lab_name = topology.get("name", "dcn")
    container_names = {}
    for client in topology["devices"]["clients"]:
        client_name = client["name"]
        container_names[client_name] = clab_container_name(lab_name, client_name)

    runner.traffic_controller = TrafficController(container_names)

    flows: list[TrafficFlow] = []
    for flow_dict in traffic_config["flows"]:
        flows.append(
            TrafficFlow(
                src=flow_dict["src"],
                dst=flow_dict["dst"],
                dst_ip=flow_dict["dst_ip"],
                dst_port=flow_dict.get("dst_port", 5201),
                protocol=flow_dict["protocol"],
                bandwidth=flow_dict.get("bandwidth", "1M"),
                duration=flow_dict.get("duration", 0),
                parallel=flow_dict.get("parallel", 1),
                udp_payload_len=flow_dict.get("udp_payload_len", 1400),
                tcp_mss=flow_dict.get("tcp_mss", 1360),
            )
        )

    _emit(f"\n[Traffic Start] Starting {len(flows)} flows...")
    runner.traffic_controller.start_matrix(flows)
    return traffic_config


def stop_traffic(runner) -> None:
    """Stop all active traffic flows."""

    if runner.traffic_controller:
        _emit("\n[Traffic Stop] Stopping all traffic flows...")
        runner.traffic_controller.stop_all()
        runner.traffic_controller = None
