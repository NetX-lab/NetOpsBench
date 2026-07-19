"""Generate the canonical standard background-traffic matrix."""

from __future__ import annotations

from functools import partial

from netopsbench.models.profiles import ScaleRegistry, get_scale_profile
from netopsbench.models.topology import DEFAULT_LINK_MTU
from netopsbench.platform.topology.topology_utils import coerce_topology_manifest, load_topology_manifest

from .estimator import estimate_client_pps as _estimate_client_pps
from .estimator import estimate_flow_pps as _estimate_flow_pps
from .estimator import estimate_switch_pps as _estimate_switch_pps
from .estimator import infer_topology_link_mtu
from .planner import build_candidate_flow as _build_candidate_flow
from .planner import generate_traffic_config_from_topology as plan_standard_traffic
from .settings import TrafficSettings

BASE_SWITCH_PPS_LIMIT = 1000
DEFAULT_LINK_MTU_BYTES = DEFAULT_LINK_MTU
UDP_PAYLOAD_LEN_BYTES = 1400
TCP_MSS_BYTES = 1360
IPERF_SERVER_PORT_BASE = 5201
IPERF_SERVER_PORT_POOL_SIZE = 4
FLOWS_PER_CLIENT = 4


def _format_bandwidth_from_pps(pps: float, packet_size_bytes: int) -> str:
    bits_per_sec = max(pps, 1.0) * packet_size_bytes * 8
    if bits_per_sec >= 1_000_000:
        return f"{max(int(bits_per_sec / 1_000_000), 1)}M"
    return f"{max(int(bits_per_sec / 1_000), 100)}K"


def _max_pps_per_client(scale: str, switch_pps_limit: int | None, registry: ScaleRegistry | None = None) -> int:
    base = get_scale_profile(scale, registry).traffic_max_pps_per_client
    if switch_pps_limit is None:
        return base
    return max(1, int(round(base * switch_pps_limit / BASE_SWITCH_PPS_LIMIT)))


def _standard_bandwidths(max_pps_per_client: int) -> dict[str, str]:
    pps_per_flow = max_pps_per_client / FLOWS_PER_CLIENT
    return {
        "udp": _format_bandwidth_from_pps(pps_per_flow, UDP_PAYLOAD_LEN_BYTES + 28),
        "tcp": _format_bandwidth_from_pps(pps_per_flow, TCP_MSS_BYTES + 40),
    }


def estimate_flow_pps(flow: dict) -> float:
    return _estimate_flow_pps(flow, udp_payload_len_bytes=UDP_PAYLOAD_LEN_BYTES, tcp_mss_bytes=TCP_MSS_BYTES)


def estimate_switch_pps(topology: dict, flows: list) -> dict:
    return _estimate_switch_pps(topology, flows, estimate_flow_pps_fn=estimate_flow_pps)


def estimate_client_pps(flows: list) -> dict:
    return _estimate_client_pps(flows, estimate_flow_pps_fn=estimate_flow_pps)


def generate_traffic_config_from_topology(
    topology: dict,
    scale: str,
    profile_type: str = "standard",
    *,
    settings: TrafficSettings | None = None,
    scale_registry: ScaleRegistry | None = None,
) -> dict:
    if profile_type != "standard":
        raise ValueError(f"Only the standard traffic profile is supported, got: {profile_type}")
    get_scale_profile(scale, scale_registry)
    projected = coerce_topology_manifest(topology).to_agent_topology()
    settings = settings or TrafficSettings.from_env()
    max_pps_per_client = _max_pps_per_client(scale, settings.switch_pps_limit, scale_registry)
    bandwidths = _standard_bandwidths(max_pps_per_client)
    link_mtu_bytes = infer_topology_link_mtu(projected, DEFAULT_LINK_MTU_BYTES)

    build_flow = partial(
        _build_candidate_flow,
        udp_payload_len_bytes=UDP_PAYLOAD_LEN_BYTES,
        tcp_mss_bytes=TCP_MSS_BYTES,
    )
    traffic = plan_standard_traffic(
        topology=projected,
        scale=scale,
        flows_per_client=FLOWS_PER_CLIENT,
        max_pps_per_client=max_pps_per_client,
        bandwidth_by_protocol=bandwidths,
        link_mtu_bytes=link_mtu_bytes,
        switch_pps_limit=settings.switch_pps_limit,
        iperf_server_port_base=IPERF_SERVER_PORT_BASE,
        iperf_server_port_pool_size=IPERF_SERVER_PORT_POOL_SIZE,
        build_candidate_flow_fn=build_flow,
        estimate_flow_pps_fn=estimate_flow_pps,
        estimate_client_pps_fn=estimate_client_pps,
        estimate_switch_pps_fn=estimate_switch_pps,
    )
    traffic["profile"].update(
        {
            "udp_payload_len_bytes": UDP_PAYLOAD_LEN_BYTES,
            "tcp_mss_bytes": TCP_MSS_BYTES,
            "tcp_payload_len_bytes_estimate": TCP_MSS_BYTES,
        }
    )
    return traffic


def generate_traffic_config(
    topology_file: str,
    scale: str,
    profile_type: str = "standard",
    *,
    settings: TrafficSettings | None = None,
    scale_registry: ScaleRegistry | None = None,
) -> dict:
    return generate_traffic_config_from_topology(
        load_topology_manifest(topology_file).model_dump(mode="json"),
        scale,
        profile_type,
        settings=settings,
        scale_registry=scale_registry,
    )


def validate_traffic_config(
    config: dict,
    scale: str,
    *,
    settings: TrafficSettings | None = None,
    scale_registry: ScaleRegistry | None = None,
) -> bool:
    settings = settings or TrafficSettings.from_env()
    max_allowed_client_pps = _max_pps_per_client(scale, settings.switch_pps_limit, scale_registry)
    stats = config.get("stats", {})
    estimated_clients = stats.get("estimated_pps_per_client") or stats.get("estimated_udp_pps_per_client", {})
    max_client_pps = stats.get("estimated_max_pps_per_client") or stats.get("estimated_max_udp_pps_per_client", 0.0)
    switch_pps = stats.get("estimated_switch_pps", {})
    max_leaf_pps = switch_pps.get("max_leaf_pps", 0.0)
    max_spine_pps = switch_pps.get("max_spine_pps", 0.0)
    if max_client_pps > max_allowed_client_pps:
        raise ValueError(
            f"Estimated PPS per client too high ({max_client_pps:.2f} > {max_allowed_client_pps}). "
            f"Details: {estimated_clients}"
        )
    if settings.switch_pps_limit is not None and (
        max_leaf_pps > settings.switch_pps_limit or max_spine_pps > settings.switch_pps_limit
    ):
        raise ValueError(
            f"Estimated switch PPS too high (leaf max={max_leaf_pps:.2f}, spine max={max_spine_pps:.2f}, "
            f"limit={settings.switch_pps_limit})."
        )
    return True


__all__ = [
    "BASE_SWITCH_PPS_LIMIT",
    "FLOWS_PER_CLIENT",
    "IPERF_SERVER_PORT_BASE",
    "IPERF_SERVER_PORT_POOL_SIZE",
    "estimate_client_pps",
    "estimate_flow_pps",
    "estimate_switch_pps",
    "generate_traffic_config",
    "generate_traffic_config_from_topology",
    "validate_traffic_config",
]
