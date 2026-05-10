"""Traffic flow planning helpers."""

from __future__ import annotations


def candidate_destinations(clients: list[dict], src_idx: int) -> list[tuple[int, dict]]:
    total_clients = len(clients)
    ordered: list[tuple[int, dict]] = []
    for offset in range(1, total_clients):
        dst_idx = (src_idx + offset) % total_clients
        ordered.append((dst_idx, clients[dst_idx]))
    return ordered


def build_candidate_flow(
    *,
    flow_id: int,
    src_client: dict,
    dst_client: dict,
    protocol: str,
    profile,
    link_mtu_bytes: int,
    dst_port: int,
    udp_payload_len_bytes: int,
    tcp_mss_bytes: int,
    get_bandwidth,
) -> dict:
    src_ip = src_client.get("data_ip") or src_client.get("ip")
    dst_ip = dst_client.get("data_ip") or dst_client.get("ip")
    if not src_ip or not dst_ip:
        raise ValueError(
            f"Missing client IP for flow {src_client['name']} -> {dst_client['name']}. "
            "Expected 'data_ip' or 'ip' in topology metadata."
        )
    flow = {
        "flow_id": flow_id,
        "src": src_client["name"],
        "src_ip": src_ip,
        "dst": dst_client["name"],
        "dst_ip": dst_ip,
        "dst_port": dst_port,
        "protocol": protocol,
        "bandwidth": get_bandwidth(profile, protocol),
        "duration": 0,
        "parallel": 1,
    }
    if protocol == "udp":
        flow["udp_payload_len"] = udp_payload_len_bytes
    else:
        flow["path_mtu_bytes"] = link_mtu_bytes
        flow["tcp_mss"] = tcp_mss_bytes
        flow["tcp_payload_len"] = tcp_mss_bytes
    return flow


def generate_traffic_config_from_topology(
    *,
    topology: dict,
    scale: str,
    profile,
    spec,
    link_mtu_bytes: int,
    switch_pps_limit,
    iperf_server_port_base: int,
    iperf_server_port_pool_size: int,
    build_candidate_flow_fn,
    estimate_flow_pps_fn,
    estimate_client_pps_fn,
    estimate_switch_pps_fn,
) -> dict:
    clients = [d for d in topology["devices"]["clients"]]
    client_to_leaf = {client["name"]: client.get("leaf") for client in clients}
    leaf_to_clients: dict[str, list[dict]] = {}
    for client in clients:
        leaf_name = client.get("leaf")
        if leaf_name:
            leaf_to_clients.setdefault(leaf_name, []).append(client)

    flows = []
    flow_id = 0
    rejected_candidates = 0
    leaf_pps = {leaf["name"]: 0.0 for leaf in topology.get("devices", {}).get("leafs", [])}
    spine_pps = {spine["name"]: 0.0 for spine in topology.get("devices", {}).get("spines", [])}
    spine_count = len(spine_pps)
    client_pps = {client["name"]: 0.0 for client in clients}
    source_flow_count = {client["name"]: 0 for client in clients}
    source_used_dsts = {client["name"]: set() for client in clients}
    incoming_flow_count = {client["name"]: 0 for client in clients}
    cross_leaf_flows = 0
    intra_leaf_flows = 0

    def flow_type(src_name: str, dst_name: str) -> str:
        src_leaf = client_to_leaf.get(src_name)
        dst_leaf = client_to_leaf.get(dst_name)
        if src_leaf and dst_leaf and src_leaf != dst_leaf:
            return "cross"
        return "intra"

    def can_admit(flow: dict) -> bool:
        nonlocal rejected_candidates
        src_name = flow["src"]
        dst_name = flow["dst"]
        pps = estimate_flow_pps_fn(flow)
        if client_pps.get(src_name, 0.0) + pps > spec.max_pps_per_client:
            return False
        src_leaf = client_to_leaf.get(src_name)
        dst_leaf = client_to_leaf.get(dst_name)
        if switch_pps_limit is not None:
            leaf_additions: dict[str, float] = {}
            if src_leaf in leaf_pps:
                leaf_additions[src_leaf] = leaf_additions.get(src_leaf, 0.0) + pps
            if dst_leaf in leaf_pps:
                leaf_additions[dst_leaf] = leaf_additions.get(dst_leaf, 0.0) + pps
            for leaf_name, added_pps in leaf_additions.items():
                if (leaf_pps[leaf_name] + added_pps) > switch_pps_limit:
                    return False
        if switch_pps_limit is not None and src_leaf and dst_leaf and src_leaf != dst_leaf and spine_count > 0:
            per_spine_pps = pps / spine_count
            for current in spine_pps.values():
                if (current + per_spine_pps) > switch_pps_limit:
                    return False
        if source_flow_count[src_name] >= profile.flows_per_client:
            return False
        return True

    def admit_flow(flow: dict):
        nonlocal flow_id, cross_leaf_flows, intra_leaf_flows
        src_name = flow["src"]
        dst_name = flow["dst"]
        pps = estimate_flow_pps_fn(flow)
        client_pps[src_name] = client_pps.get(src_name, 0.0) + pps
        source_flow_count[src_name] += 1
        source_used_dsts[src_name].add(dst_name)
        incoming_flow_count[dst_name] = incoming_flow_count.get(dst_name, 0) + 1
        src_leaf = client_to_leaf.get(src_name)
        dst_leaf = client_to_leaf.get(dst_name)
        if src_leaf in leaf_pps:
            leaf_pps[src_leaf] += pps
        if dst_leaf in leaf_pps:
            leaf_pps[dst_leaf] += pps
        if src_leaf and dst_leaf and src_leaf != dst_leaf and spine_count > 0:
            per_spine_pps = pps / spine_count
            for spine_name in spine_pps:
                spine_pps[spine_name] += per_spine_pps
        if flow_type(src_name, dst_name) == "cross":
            cross_leaf_flows += 1
        else:
            intra_leaf_flows += 1
        flows.append(flow)
        flow_id += 1

    def candidate_pools(src_idx: int) -> tuple[list[dict], list[dict]]:
        src_client = clients[src_idx]
        src_leaf = src_client.get("leaf")
        cross_candidates: list[dict] = []
        intra_candidates: list[dict] = []
        for _, dst_client in candidate_destinations(clients, src_idx):
            if dst_client["name"] in source_used_dsts[src_client["name"]]:
                continue
            dst_leaf = dst_client.get("leaf")
            if src_leaf and dst_leaf and src_leaf != dst_leaf:
                cross_candidates.append(dst_client)
            else:
                intra_candidates.append(dst_client)
        return cross_candidates, intra_candidates

    def try_add_from_pool(src_idx: int, pool_type: str) -> bool:
        nonlocal rejected_candidates
        src_client = clients[src_idx]
        cross_pool, intra_pool = candidate_pools(src_idx)
        candidate_pool = cross_pool if pool_type == "cross" else intra_pool
        for dst_client in candidate_pool:
            protocol = "udp" if (flow_id % 2 == 0) else "tcp"
            dst_name = dst_client["name"]
            dst_port = iperf_server_port_base + (incoming_flow_count.get(dst_name, 0) % iperf_server_port_pool_size)
            candidate_flow = build_candidate_flow_fn(
                flow_id=flow_id,
                src_client=src_client,
                dst_client=dst_client,
                protocol=protocol,
                profile=profile,
                link_mtu_bytes=link_mtu_bytes,
                dst_port=dst_port,
            )
            if can_admit(candidate_flow):
                admit_flow(candidate_flow)
                return True
            rejected_candidates += 1
        return False

    for src_idx, src_client in enumerate(clients):
        src_leaf = src_client.get("leaf")
        same_leaf_peers = [c for c in leaf_to_clients.get(src_leaf, []) if c["name"] != src_client["name"]]
        has_cross_candidates = len(clients) > len(leaf_to_clients.get(src_leaf, []))
        if has_cross_candidates:
            try_add_from_pool(src_idx, "cross")
        if same_leaf_peers:
            try_add_from_pool(src_idx, "intra")

    progress = True
    while progress:
        progress = False
        for src_idx, src_client in enumerate(clients):
            src_name = src_client["name"]
            if source_flow_count[src_name] >= profile.flows_per_client:
                continue
            src_total_flows = source_flow_count[src_name]
            src_cross_flows = sum(1 for f in flows if f["src"] == src_name and flow_type(f["src"], f["dst"]) == "cross")
            preferred_pool = (
                "cross"
                if src_total_flows == 0 or (src_cross_flows / src_total_flows) < profile.cross_leaf_target_ratio
                else "intra"
            )
            if try_add_from_pool(src_idx, preferred_pool):
                progress = True
                continue
            fallback_pool = "intra" if preferred_pool == "cross" else "cross"
            if try_add_from_pool(src_idx, fallback_pool):
                progress = True

    total_path_flows = cross_leaf_flows + intra_leaf_flows
    cross_leaf_ratio = (cross_leaf_flows / total_path_flows) if total_path_flows else 0.0
    per_client_pps = estimate_client_pps_fn(flows)
    per_client_udp_pps = {}
    for flow in flows:
        if flow["protocol"] == "udp":
            src_name = flow["src"]
            per_client_udp_pps[src_name] = per_client_udp_pps.get(src_name, 0.0) + estimate_flow_pps_fn(flow)
    switch_pps = estimate_switch_pps_fn(topology, flows)

    return {
        "profile": {
            "name": profile.name,
            "description": profile.description,
            "scale": scale,
            "max_pps_per_client": spec.max_pps_per_client,
            "switch_pps_limit": switch_pps_limit,
            "cross_leaf_target_ratio": profile.cross_leaf_target_ratio,
            "target_client_utilization": profile.target_client_utilization,
            "link_mtu_bytes": link_mtu_bytes,
        },
        "flows": flows,
        "stats": {
            "total_flows": len(flows),
            "udp_flows": sum(1 for f in flows if f["protocol"] == "udp"),
            "tcp_flows": sum(1 for f in flows if f["protocol"] == "tcp"),
            "rejected_candidates": rejected_candidates,
            "estimated_pps_per_client": {client: round(pps, 2) for client, pps in per_client_pps.items()},
            "estimated_max_pps_per_client": round(max(per_client_pps.values()) if per_client_pps else 0.0, 2),
            "estimated_udp_pps_per_client": {client: round(pps, 2) for client, pps in per_client_udp_pps.items()},
            "estimated_max_udp_pps_per_client": round(
                max(per_client_udp_pps.values()) if per_client_udp_pps else 0.0, 2
            ),
            "cross_leaf_flows": cross_leaf_flows,
            "intra_leaf_flows": intra_leaf_flows,
            "cross_leaf_flow_ratio": round(cross_leaf_ratio, 3),
            "estimated_switch_pps": switch_pps,
        },
    }
