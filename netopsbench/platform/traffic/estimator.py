"""Traffic PPS estimation helpers."""

from __future__ import annotations

UDP_IP_OVERHEAD_BYTES = 28
TCP_IP_OVERHEAD_BYTES = 40
MIN_TCP_PAYLOAD_BYTES = 536


def bandwidth_to_bps(bandwidth: str) -> float:
    if not bandwidth:
        return 0.0
    value = bandwidth.strip().upper()
    if value.endswith("K"):
        return float(value[:-1]) * 1_000
    if value.endswith("M"):
        return float(value[:-1]) * 1_000_000
    if value.endswith("G"):
        return float(value[:-1]) * 1_000_000_000
    return float(value)


def infer_topology_link_mtu(topology: dict, default_link_mtu_bytes: int) -> int:
    candidates = [
        topology.get("defaults", {}).get("link_mtu"),
        topology.get("fabric", {}).get("link_mtu"),
        topology.get("routing", {}).get("link_mtu"),
        topology.get("link_mtu"),
    ]
    for candidate in candidates:
        if isinstance(candidate, int) and candidate > 0:
            return candidate
    for client in topology.get("devices", {}).get("clients", []):
        client_mtu = client.get("mtu")
        if isinstance(client_mtu, int) and client_mtu > 0:
            return client_mtu
    return default_link_mtu_bytes


def estimate_packet_size_bytes(
    flow: dict,
    *,
    udp_payload_len_bytes: int,
    tcp_mss_bytes: int,
) -> float:
    protocol = flow.get("protocol")
    if protocol == "udp":
        udp_payload = float(flow.get("udp_payload_len", udp_payload_len_bytes))
        return max(udp_payload, 64.0) + UDP_IP_OVERHEAD_BYTES
    if protocol == "tcp":
        tcp_payload = flow.get("tcp_payload_len")
        if tcp_payload is None:
            tcp_payload = flow.get("tcp_mss", tcp_mss_bytes)
        return max(float(tcp_payload), float(MIN_TCP_PAYLOAD_BYTES)) + TCP_IP_OVERHEAD_BYTES
    return 0.0


def estimate_flow_pps(
    flow: dict,
    *,
    udp_payload_len_bytes: int,
    tcp_mss_bytes: int,
) -> float:
    if flow.get("protocol") not in ["udp", "tcp"]:
        return 0.0
    bps = bandwidth_to_bps(flow.get("bandwidth", "0"))
    if bps <= 0:
        return 0.0
    packet_size_bytes = estimate_packet_size_bytes(
        flow,
        udp_payload_len_bytes=udp_payload_len_bytes,
        tcp_mss_bytes=tcp_mss_bytes,
    )
    if packet_size_bytes <= 0:
        return 0.0
    return bps / (packet_size_bytes * 8)


def estimate_switch_pps(
    topology: dict,
    flows: list[dict],
    *,
    estimate_flow_pps_fn,
) -> dict:
    clients = topology.get("devices", {}).get("clients", [])
    leafs = topology.get("devices", {}).get("leafs", [])
    spines = topology.get("devices", {}).get("spines", [])

    client_to_leaf = {client["name"]: client.get("leaf") for client in clients}
    leaf_pps = {leaf["name"]: 0.0 for leaf in leafs}
    spine_pps = {spine["name"]: 0.0 for spine in spines}
    spine_count = len(spines)

    for flow in flows:
        pps = estimate_flow_pps_fn(flow)
        if pps <= 0:
            continue
        src_leaf = client_to_leaf.get(flow.get("src"))
        dst_leaf = client_to_leaf.get(flow.get("dst"))
        if src_leaf in leaf_pps:
            leaf_pps[src_leaf] += pps
        if dst_leaf in leaf_pps:
            leaf_pps[dst_leaf] += pps
        if src_leaf and dst_leaf and src_leaf != dst_leaf and spine_count > 0:
            per_spine_pps = pps / spine_count
            for spine_name in spine_pps:
                spine_pps[spine_name] += per_spine_pps

    return {
        "leafs": {k: round(v, 2) for k, v in leaf_pps.items()},
        "spines": {k: round(v, 2) for k, v in spine_pps.items()},
        "max_leaf_pps": round(max(leaf_pps.values()) if leaf_pps else 0.0, 2),
        "max_spine_pps": round(max(spine_pps.values()) if spine_pps else 0.0, 2),
    }


def estimate_client_pps(flows: list[dict], *, estimate_flow_pps_fn) -> dict[str, float]:
    per_client_pps: dict[str, float] = {}
    for flow in flows:
        src_name = flow.get("src")
        if not src_name:
            continue
        per_client_pps[src_name] = per_client_pps.get(src_name, 0.0) + estimate_flow_pps_fn(flow)
    return per_client_pps
