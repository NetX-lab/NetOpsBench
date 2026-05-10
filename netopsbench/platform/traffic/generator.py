"""Traffic generation strategy for generated topologies."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from functools import partial

from .estimator import (
    estimate_client_pps as _estimate_client_pps,
)
from .estimator import (
    estimate_flow_pps as _estimate_flow_pps,
)
from .estimator import (
    estimate_switch_pps as _estimate_switch_pps,
)
from .estimator import (
    infer_topology_link_mtu,
)
from .planner import build_candidate_flow as _build_candidate_flow
from .planner import generate_traffic_config_from_topology as _plan_traffic


def _parse_switch_pps_limit(value: str):
    """Parse optional switch PPS limit from env (None means unlimited)."""
    if value is None:
        return None
    raw = str(value).strip().lower()
    if raw in {"", "0", "none", "unlimited", "inf", "infinite"}:
        return None
    return int(raw)


SWITCH_PPS_LIMIT = _parse_switch_pps_limit(os.getenv("NETOPSBENCH_SWITCH_PPS_LIMIT", "5000"))
BASE_SWITCH_PPS_LIMIT = 1000
DEFAULT_LINK_MTU_BYTES = 9232
UDP_PAYLOAD_LEN_BYTES = 1400
TCP_MSS_BYTES = 1360
IPERF_SERVER_PORT_BASE = 5201
IPERF_SERVER_PORT_POOL_SIZE = 8


@dataclass
class TopologySpec:
    scale: str
    num_clients: int
    num_leafs: int
    num_spines: int
    max_pps_per_client: int


TOPOLOGY_SPECS = {
    "xs": TopologySpec(scale="xs", num_clients=4, num_leafs=2, num_spines=2, max_pps_per_client=250),
    "small": TopologySpec(scale="small", num_clients=8, num_leafs=4, num_spines=2, max_pps_per_client=250),
    "medium": TopologySpec(scale="medium", num_clients=16, num_leafs=8, num_spines=4, max_pps_per_client=200),
    "large": TopologySpec(scale="large", num_clients=32, num_leafs=16, num_spines=8, max_pps_per_client=150),
}


def _scale_max_pps_per_client() -> None:
    if SWITCH_PPS_LIMIT is None:
        return
    scale_factor = SWITCH_PPS_LIMIT / BASE_SWITCH_PPS_LIMIT
    for spec in TOPOLOGY_SPECS.values():
        scaled = int(round(spec.max_pps_per_client * scale_factor))
        spec.max_pps_per_client = max(1, scaled)


_scale_max_pps_per_client()


@dataclass
class TrafficProfile:
    name: str
    description: str
    pattern: str
    udp_bandwidth_per_flow: str
    tcp_bandwidth_per_flow: str
    tcp_connections: int
    flows_per_client: int
    cross_leaf_target_ratio: float
    target_client_utilization: float


def _format_bandwidth_from_pps(pps: float, packet_size_bytes: int) -> str:
    bits_per_sec = max(pps, 1.0) * packet_size_bytes * 8
    if bits_per_sec >= 1_000_000:
        mbps = max(int(bits_per_sec / 1_000_000), 1)
        return f"{mbps}M"
    kbps = max(int(bits_per_sec / 1_000), 100)
    return f"{kbps}K"


def _get_flow_bandwidth(profile: TrafficProfile, protocol: str) -> str:
    return profile.udp_bandwidth_per_flow if protocol == "udp" else profile.tcp_bandwidth_per_flow


def estimate_flow_pps(flow: dict) -> float:
    """Estimate PPS for a single flow using module-level packet size constants."""
    return _estimate_flow_pps(flow, udp_payload_len_bytes=UDP_PAYLOAD_LEN_BYTES, tcp_mss_bytes=TCP_MSS_BYTES)


def estimate_switch_pps(topology: dict, flows: list) -> dict:
    return _estimate_switch_pps(topology, flows, estimate_flow_pps_fn=estimate_flow_pps)


def estimate_client_pps(flows: list) -> dict:
    return _estimate_client_pps(flows, estimate_flow_pps_fn=estimate_flow_pps)


def get_traffic_profile(
    scale: str, profile_type: str = "standard", link_mtu_bytes: int = DEFAULT_LINK_MTU_BYTES
) -> TrafficProfile:
    spec = TOPOLOGY_SPECS.get(scale)
    if not spec:
        raise ValueError(f"Unknown scale: {scale}")
    profile_utilization = {"light": 0.60, "standard": 1.00, "stress": 1.00}
    flows_by_profile = {"light": 2, "standard": 4, "stress": 6}
    cross_leaf_targets = {"light": 0.50, "standard": 0.65, "stress": 0.75}
    if profile_type not in profile_utilization:
        raise ValueError(f"Unknown profile type: {profile_type}")
    flows_per_client = flows_by_profile[profile_type]
    udp_flows_per_client = max(flows_per_client, 1)
    target_pps_per_client = spec.max_pps_per_client * profile_utilization[profile_type]
    target_pps_per_udp_flow = target_pps_per_client / udp_flows_per_client
    udp_packet_size = UDP_PAYLOAD_LEN_BYTES + 28
    tcp_packet_size = TCP_MSS_BYTES + 40
    udp_bandwidth_per_flow = _format_bandwidth_from_pps(target_pps_per_udp_flow, udp_packet_size)
    tcp_bandwidth_per_flow = _format_bandwidth_from_pps(target_pps_per_udp_flow, tcp_packet_size)
    tcp_connections = spec.num_clients if profile_type == "stress" else spec.num_clients // 2
    return TrafficProfile(
        name=f"{scale}_{profile_type}",
        description=f"{profile_type.capitalize()} traffic profile for {scale} topology",
        pattern="full_mesh",
        udp_bandwidth_per_flow=udp_bandwidth_per_flow,
        tcp_bandwidth_per_flow=tcp_bandwidth_per_flow,
        tcp_connections=tcp_connections,
        flows_per_client=flows_per_client,
        cross_leaf_target_ratio=cross_leaf_targets[profile_type],
        target_client_utilization=profile_utilization[profile_type],
    )


def generate_traffic_config_from_topology(topology: dict, scale: str, profile_type: str = "standard") -> dict:
    link_mtu_bytes = infer_topology_link_mtu(topology, DEFAULT_LINK_MTU_BYTES)
    profile = get_traffic_profile(scale, profile_type, link_mtu_bytes=link_mtu_bytes)
    spec = TOPOLOGY_SPECS[scale]

    bound_build_flow = partial(
        _build_candidate_flow,
        udp_payload_len_bytes=UDP_PAYLOAD_LEN_BYTES,
        tcp_mss_bytes=TCP_MSS_BYTES,
        get_bandwidth=_get_flow_bandwidth,
    )

    config = _plan_traffic(
        topology=topology,
        scale=scale,
        profile=profile,
        spec=spec,
        link_mtu_bytes=link_mtu_bytes,
        switch_pps_limit=SWITCH_PPS_LIMIT,
        iperf_server_port_base=IPERF_SERVER_PORT_BASE,
        iperf_server_port_pool_size=IPERF_SERVER_PORT_POOL_SIZE,
        build_candidate_flow_fn=bound_build_flow,
        estimate_flow_pps_fn=estimate_flow_pps,
        estimate_client_pps_fn=estimate_client_pps,
        estimate_switch_pps_fn=estimate_switch_pps,
    )
    config["profile"].update(
        {
            "udp_payload_len_bytes": UDP_PAYLOAD_LEN_BYTES,
            "tcp_mss_bytes": TCP_MSS_BYTES,
            "tcp_payload_len_bytes_estimate": TCP_MSS_BYTES,
        }
    )
    return config


def generate_traffic_config(topology_file: str, scale: str, profile_type: str = "standard") -> dict:
    with open(topology_file, encoding="utf-8") as f:
        topology = json.load(f)
    return generate_traffic_config_from_topology(topology, scale, profile_type)


def validate_traffic_config(config: dict, scale: str) -> bool:
    spec = TOPOLOGY_SPECS.get(scale)
    if not spec:
        raise ValueError(f"Unknown scale: {scale}")
    stats = config.get("stats", {})
    estimated_clients = stats.get("estimated_pps_per_client") or stats.get("estimated_udp_pps_per_client", {})
    max_client_pps = stats.get("estimated_max_pps_per_client") or stats.get("estimated_max_udp_pps_per_client", 0.0)
    switch_pps = stats.get("estimated_switch_pps", {})
    max_leaf_pps = switch_pps.get("max_leaf_pps", 0.0)
    max_spine_pps = switch_pps.get("max_spine_pps", 0.0)
    if max_client_pps > spec.max_pps_per_client:
        raise ValueError(
            f"Estimated PPS per client too high ({max_client_pps:.2f} > {spec.max_pps_per_client}). "
            f"Details: {estimated_clients}"
        )
    if SWITCH_PPS_LIMIT is not None and (max_leaf_pps > SWITCH_PPS_LIMIT or max_spine_pps > SWITCH_PPS_LIMIT):
        raise ValueError(
            f"Estimated switch PPS too high (leaf max={max_leaf_pps:.2f}, spine max={max_spine_pps:.2f}, "
            f"limit={SWITCH_PPS_LIMIT})."
        )
    return True
