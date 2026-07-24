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
    devices = topology.get("devices", {})
    role_names = {
        role: [device["name"] for device in devices.get(f"{role}s", [])]
        for role in ("spine", "leaf", "core", "agg", "edge")
    }
    switch_pps = {name: 0.0 for names in role_names.values() for name in names}
    client_to_switch = {
        client["name"]: client.get("leaf") or client.get("edge") or client.get("attached_switch") for client in clients
    }
    device_pods = {
        device["name"]: device.get("pod") for role in ("agg", "edge") for device in devices.get(f"{role}s", [])
    }

    for flow in flows:
        pps = estimate_flow_pps_fn(flow)
        if pps <= 0:
            continue
        src_switch = client_to_switch.get(flow.get("src"))
        dst_switch = client_to_switch.get(flow.get("dst"))
        for name, addition in switch_path_pps(
            role_names,
            device_pods,
            src_switch,
            dst_switch,
            pps,
        ).items():
            switch_pps[name] += addition

    result: dict[str, object] = {}
    for role, names in role_names.items():
        values = {name: round(switch_pps[name], 2) for name in names}
        result[f"{role}s"] = values
        result[f"max_{role}_pps"] = round(max(values.values()) if values else 0.0, 2)
    result["max_switch_pps"] = round(max(switch_pps.values()) if switch_pps else 0.0, 2)
    return result


def switch_path_pps(
    role_names: dict[str, list[str]],
    device_pods: dict[str, object],
    src_switch: str | None,
    dst_switch: str | None,
    pps: float,
) -> dict[str, float]:
    """Return per-switch PPS additions for a CLOS or fat-tree path."""
    additions: dict[str, float] = {}
    for endpoint in (src_switch, dst_switch):
        if endpoint is None:
            continue
        if endpoint in role_names["leaf"] or endpoint in role_names["edge"]:
            additions[endpoint] = additions.get(endpoint, 0.0) + pps
    if not src_switch or not dst_switch or src_switch == dst_switch:
        return additions

    if src_switch in role_names["leaf"] and dst_switch in role_names["leaf"]:
        for spine in role_names["spine"]:
            additions[spine] = additions.get(spine, 0.0) + pps / len(role_names["spine"])
        return additions

    src_pod = device_pods.get(src_switch)
    dst_pod = device_pods.get(dst_switch)
    transit_pods = {src_pod} if src_pod == dst_pod else {src_pod, dst_pod}
    for pod in transit_pods - {None}:
        aggs = [name for name in role_names["agg"] if device_pods.get(name) == pod]
        for agg in aggs:
            additions[agg] = additions.get(agg, 0.0) + pps / len(aggs)
    if src_pod != dst_pod:
        for core in role_names["core"]:
            additions[core] = additions.get(core, 0.0) + pps / len(role_names["core"])
    return additions


def estimate_client_pps(flows: list[dict], *, estimate_flow_pps_fn) -> dict[str, float]:
    per_client_pps: dict[str, float] = {}
    for flow in flows:
        src_name = flow.get("src")
        if not src_name:
            continue
        per_client_pps[src_name] = per_client_pps.get(src_name, 0.0) + estimate_flow_pps_fn(flow)
    return per_client_pps
