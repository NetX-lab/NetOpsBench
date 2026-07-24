"""Traffic execution helpers for scenario execution."""

from __future__ import annotations

from pathlib import Path

from netopsbench.logging_utils import get_logger
from netopsbench.platform.topology.topology_utils import load_topology_manifest
from netopsbench.platform.traffic.controller import TrafficController, TrafficFlow
from netopsbench.platform.traffic.generator import generate_traffic_config, validate_traffic_config

logger = get_logger(__name__)


def setup_traffic(runner, scale: str, profile: str) -> dict:
    """Generate topology-driven traffic, start the controller, and return the config."""

    logger.info(f"\n[Traffic Setup] Generating {profile} traffic for {scale} topology...")

    topology_file = Path(runner.topology_dir) / "topology.json"
    if not topology_file.exists():
        raise FileNotFoundError(f"Topology metadata not found: {topology_file}")

    traffic_config = generate_traffic_config(
        str(topology_file),
        scale,
        profile,
        scale_registry=runner.scale_registry,
    )
    validate_traffic_config(traffic_config, scale, scale_registry=runner.scale_registry)

    logger.info(f"  Total flows: {traffic_config['stats']['total_flows']}")
    logger.info(f"  UDP flows: {traffic_config['stats']['udp_flows']}")
    logger.info(f"  TCP flows: {traffic_config['stats']['tcp_flows']}")

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
        logger.info(f"  Estimated max PPS per client (all protocols): {estimated_client_pps}")
    if estimated_udp_client_pps is not None:
        logger.info(f"  Estimated max UDP PPS per client: {estimated_udp_client_pps}")

    limit_label = "unlimited" if switch_limit in (None, "") else switch_limit
    logger.info(f"  Estimated switch PPS limit: {limit_label}")
    logger.info(f"  Estimated max leaf PPS: {max_leaf_pps}")
    logger.info(f"  Estimated max spine PPS: {max_spine_pps}")
    logger.info(
        f"  Path mix: cross-leaf={cross_leaf_flows}, intra-leaf={intra_leaf_flows}, cross-leaf-ratio={cross_leaf_ratio}"
    )

    leafs = switch_pps.get("leafs", {})
    spines = switch_pps.get("spines", {})
    if leafs:
        logger.debug("Leaf PPS breakdown: %s", leafs)
    if spines:
        logger.debug("Spine PPS breakdown: %s", spines)

    manifest = load_topology_manifest(topology_file)
    management_ips = {client.name: client.mgmt_ip for client in manifest.clients() if client.mgmt_ip}
    if len(management_ips) != len(manifest.clients()):
        raise RuntimeError("Every traffic client requires a management IP")

    runner.traffic_controller = TrafficController(management_ips)

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
                udp_payload_len=flow_dict.get("udp_payload_len", 1400),
                tcp_mss=flow_dict.get("tcp_mss", 1360),
            )
        )

    logger.info(f"\n[Traffic Start] Starting {len(flows)} flows...")
    started_flow_ids = runner.traffic_controller.start_matrix(flows)
    traffic_config["runtime"] = runner.traffic_controller.last_start_stats.to_dict()
    if len(started_flow_ids) != len(flows):
        runner.traffic_controller.stop_all()
        runner.traffic_controller = None
        raise RuntimeError(f"Background traffic matrix incomplete: started {len(started_flow_ids)}/{len(flows)} flows")
    return traffic_config


def stop_traffic(runner) -> None:
    """Stop all active traffic flows."""

    if runner.traffic_controller:
        logger.info("\n[Traffic Stop] Stopping all traffic flows...")
        runner.traffic_controller.stop_all()
        runner.traffic_controller = None
