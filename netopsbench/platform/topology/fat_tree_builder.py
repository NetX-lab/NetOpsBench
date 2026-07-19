"""Build complete artifact-independent plans for k-ary fat-tree fabrics."""

from __future__ import annotations

import ipaddress

from netopsbench.models.topology import (
    Collector,
    Device,
    DeviceRole,
    Link,
    LinkEndpoint,
    Management,
    RoutingMetadata,
    TopologyDefaults,
    TopologyFacts,
    TopologyManifest,
)

from .config import FatTreeConfig
from .plan import BGPNeighborPlan, DevicePlan, FabricPlan
from .planning import (
    ManagementAddressing,
    build_render_settings,
    client_commands,
    pingmesh_policy_for_scale,
    sonic_port_name,
)


def _validate_config(config: FatTreeConfig) -> None:
    if config.num_total_edge > 155:
        raise ValueError(
            "num_total_edge must fit the client subnet scheme 192.168.(100+edge).0/24 "
            f"(1-155), got {config.num_total_edge}"
        )
    if int(config.clients_per_edge or 0) > 64:
        raise ValueError("clients_per_edge must fit the per-edge /30 allocation")


def build_fat_tree_plan(config: FatTreeConfig) -> FabricPlan:
    """Return a complete fat-tree plan without writing any artifacts."""
    _validate_config(config)
    half = config.half
    clients_per_edge = int(config.clients_per_edge or 0)

    core_agg_links: list[tuple[int, int]] = []
    agg_edge_links: list[tuple[int, int]] = []
    core_neighbors: dict[int, list[int]] = {}
    agg_core_neighbors: dict[int, list[int]] = {}
    agg_edge_neighbors: dict[int, list[int]] = {}
    edge_agg_neighbors: dict[int, list[int]] = {}
    agg_pod: dict[int, int] = {}
    edge_pod: dict[int, int] = {}

    for pod in range(1, config.k + 1):
        for local_agg in range(1, half + 1):
            global_agg = (pod - 1) * half + local_agg
            agg_pod[global_agg] = pod
            agg_core_neighbors[global_agg] = []
            agg_edge_neighbors[global_agg] = []
            for core_offset in range(1, half + 1):
                core_idx = (local_agg - 1) * half + core_offset
                core_agg_links.append((core_idx, global_agg))
                core_neighbors.setdefault(core_idx, []).append(global_agg)
                agg_core_neighbors[global_agg].append(core_idx)
            for local_edge in range(1, half + 1):
                global_edge = (pod - 1) * half + local_edge
                agg_edge_links.append((global_agg, global_edge))
                agg_edge_neighbors[global_agg].append(global_edge)
                edge_agg_neighbors.setdefault(global_edge, []).append(global_agg)
                edge_pod.setdefault(global_edge, pod)

    core_agg_ips: dict[tuple[int, int], tuple[str, str]] = {}
    core_agg_subnets = ipaddress.ip_network("10.1.0.0/16").subnets(new_prefix=30)
    for link in sorted(core_agg_links):
        hosts = tuple(next(core_agg_subnets).hosts())
        core_agg_ips[link] = (str(hosts[0]), str(hosts[1]))

    agg_edge_ips: dict[tuple[int, int], tuple[str, str]] = {}
    agg_edge_subnets = ipaddress.ip_network("10.2.0.0/16").subnets(new_prefix=30)
    for link in sorted(agg_edge_links):
        hosts = tuple(next(agg_edge_subnets).hosts())
        agg_edge_ips[link] = (str(hosts[0]), str(hosts[1]))

    core_mgmt_offset = 10
    agg_mgmt_offset = core_mgmt_offset + config.num_core
    edge_mgmt_offset = agg_mgmt_offset + config.num_total_agg
    client_mgmt_offset = edge_mgmt_offset + config.num_total_edge
    last_device_offset = client_mgmt_offset + config.num_total_clients
    allocated_offsets = {
        *(core_mgmt_offset + idx for idx in range(1, config.num_core + 1)),
        *(agg_mgmt_offset + idx for idx in range(1, config.num_total_agg + 1)),
        *(edge_mgmt_offset + idx for idx in range(1, config.num_total_edge + 1)),
        *(client_mgmt_offset + idx for idx in range(1, config.num_total_clients + 1)),
    }
    addressing = ManagementAddressing.create(config.mgmt_ipv4_subnet, allocated_offsets)
    collector_ip = config.collector_ip or addressing.default_collector(last_device_offset, prefer_offset_200=False)
    addressing.validate_collector(collector_ip)
    render_settings = build_render_settings(addressing, collector_ip)

    devices: list[Device] = []
    device_plans: list[DevicePlan] = []

    for core_idx in range(1, config.num_core + 1):
        name = f"core{core_idx}"
        router_id = f"10.0.1.{core_idx}"
        asn = config.core_asn_start + core_idx - 1
        device = Device(
            name=name,
            role=DeviceRole.CORE,
            mgmt_ip=addressing.host_ip(core_mgmt_offset + core_idx),
            asn=asn,
            router_id=router_id,
        )
        neighbors_for_device = sorted(core_neighbors[core_idx])
        interfaces = {
            sonic_port_name(port_idx): (f"{core_agg_ips[(core_idx, agg_idx)][0]}/30",)
            for port_idx, agg_idx in enumerate(neighbors_for_device, start=1)
        }
        neighbors = tuple(
            BGPNeighborPlan(
                peer_ip=core_agg_ips[(core_idx, agg_idx)][1],
                remote_as=config.agg_asn_start + agg_idx - 1,
            )
            for agg_idx in neighbors_for_device
        )
        devices.append(device)
        device_plans.append(
            DevicePlan(
                device=device,
                required_ports=config.k,
                configdb_interface_cidrs=interfaces,
                bgp_asn=asn,
                bgp_router_id=router_id,
                bgp_neighbors=neighbors,
            )
        )

    for agg_idx in range(1, config.num_total_agg + 1):
        name = f"agg{agg_idx}"
        router_id = f"10.0.2.{agg_idx}"
        asn = config.agg_asn_start + agg_idx - 1
        device = Device(
            name=name,
            role=DeviceRole.AGG,
            mgmt_ip=addressing.host_ip(agg_mgmt_offset + agg_idx),
            asn=asn,
            router_id=router_id,
            metadata={"pod": agg_pod[agg_idx]},
        )
        core_peers = sorted(agg_core_neighbors[agg_idx])
        edge_peers = sorted(agg_edge_neighbors[agg_idx])
        interfaces = {
            sonic_port_name(port_idx): (f"{core_agg_ips[(core_idx, agg_idx)][1]}/30",)
            for port_idx, core_idx in enumerate(core_peers, start=1)
        }
        interfaces.update(
            {
                sonic_port_name(half + port_offset): (f"{agg_edge_ips[(agg_idx, edge_idx)][0]}/30",)
                for port_offset, edge_idx in enumerate(edge_peers, start=1)
            }
        )
        neighbors = tuple(
            [
                BGPNeighborPlan(
                    peer_ip=core_agg_ips[(core_idx, agg_idx)][0],
                    remote_as=config.core_asn_start + core_idx - 1,
                )
                for core_idx in core_peers
            ]
            + [
                BGPNeighborPlan(
                    peer_ip=agg_edge_ips[(agg_idx, edge_idx)][1],
                    remote_as=config.edge_asn_start + edge_idx - 1,
                )
                for edge_idx in edge_peers
            ]
        )
        devices.append(device)
        device_plans.append(
            DevicePlan(
                device=device,
                required_ports=config.k,
                configdb_interface_cidrs=interfaces,
                bgp_asn=asn,
                bgp_router_id=router_id,
                bgp_neighbors=neighbors,
            )
        )

    for edge_idx in range(1, config.num_total_edge + 1):
        name = f"edge{edge_idx}"
        router_id = f"10.0.3.{edge_idx}"
        asn = config.edge_asn_start + edge_idx - 1
        device = Device(
            name=name,
            role=DeviceRole.EDGE,
            mgmt_ip=addressing.host_ip(edge_mgmt_offset + edge_idx),
            asn=asn,
            router_id=router_id,
            metadata={
                "pod": edge_pod[edge_idx],
                "client_subnet": f"192.168.{100 + edge_idx}.0/24",
            },
        )
        agg_peers = sorted(edge_agg_neighbors[edge_idx])
        interfaces = {
            sonic_port_name(port_idx): (f"{agg_edge_ips[(agg_idx, edge_idx)][1]}/30",)
            for port_idx, agg_idx in enumerate(agg_peers, start=1)
        }
        networks: list[str] = []
        for client_position in range(1, clients_per_edge + 1):
            subnet_base = (client_position - 1) * 4
            interfaces[sonic_port_name(half + client_position)] = (f"192.168.{100 + edge_idx}.{subnet_base + 1}/30",)
            networks.append(f"192.168.{100 + edge_idx}.{subnet_base}/30")
        neighbors = tuple(
            BGPNeighborPlan(
                peer_ip=agg_edge_ips[(agg_idx, edge_idx)][0],
                remote_as=config.agg_asn_start + agg_idx - 1,
            )
            for agg_idx in agg_peers
        )
        devices.append(device)
        device_plans.append(
            DevicePlan(
                device=device,
                required_ports=half + clients_per_edge,
                configdb_interface_cidrs=interfaces,
                bgp_asn=asn,
                bgp_router_id=router_id,
                bgp_neighbors=neighbors,
                bgp_networks=tuple(networks),
            )
        )

    client_idx = 0
    for edge_idx in range(1, config.num_total_edge + 1):
        for client_position in range(1, clients_per_edge + 1):
            client_idx += 1
            subnet_base = (client_position - 1) * 4
            client_ip = f"192.168.{100 + edge_idx}.{subnet_base + 2}"
            gateway = f"192.168.{100 + edge_idx}.{subnet_base + 1}"
            client = Device(
                name=f"client{client_idx}",
                role=DeviceRole.CLIENT,
                mgmt_ip=addressing.host_ip(client_mgmt_offset + client_idx),
                data_ip=client_ip,
                attached_switch=f"edge{edge_idx}",
                metadata={"rack": f"pod{edge_pod[edge_idx]}-edge{edge_idx}"},
            )
            devices.append(client)
            device_plans.append(DevicePlan(device=client, client_commands=client_commands(client_ip, gateway)))

    links: list[Link] = []
    for core_idx, agg_idx in sorted(core_agg_links):
        core_port = sorted(core_neighbors[core_idx]).index(agg_idx) + 1
        agg_port = sorted(agg_core_neighbors[agg_idx]).index(core_idx) + 1
        links.append(
            Link(
                kind="core-agg",
                endpoints=(
                    LinkEndpoint(device=f"core{core_idx}", interface=f"eth{core_port}"),
                    LinkEndpoint(device=f"agg{agg_idx}", interface=f"eth{agg_port}"),
                ),
            )
        )
    for agg_idx, edge_idx in sorted(agg_edge_links):
        agg_port = half + sorted(agg_edge_neighbors[agg_idx]).index(edge_idx) + 1
        edge_port = sorted(edge_agg_neighbors[edge_idx]).index(agg_idx) + 1
        links.append(
            Link(
                kind="agg-edge",
                endpoints=(
                    LinkEndpoint(device=f"agg{agg_idx}", interface=f"eth{agg_port}"),
                    LinkEndpoint(device=f"edge{edge_idx}", interface=f"eth{edge_port}"),
                ),
            )
        )
    client_idx = 0
    for edge_idx in range(1, config.num_total_edge + 1):
        for client_position in range(1, clients_per_edge + 1):
            client_idx += 1
            links.append(
                Link(
                    kind="edge-client",
                    endpoints=(
                        LinkEndpoint(device=f"edge{edge_idx}", interface=f"eth{half + client_position}"),
                        LinkEndpoint(device=f"client{client_idx}", interface="eth1"),
                    ),
                )
            )

    scale_name = config.scale_name or f"fat-tree-k{config.k}"
    manifest = TopologyManifest(
        topology_id=config.name,
        name=config.name,
        scale=scale_name,
        family="fat-tree",
        management=Management(
            network=config.mgmt_network_name or f"clab-mgmt-{config.name}",
            ipv4_subnet=str(addressing.network),
        ),
        collector=Collector(ipv4=render_settings.syslog_collector),
        defaults=TopologyDefaults(),
        facts=TopologyFacts(
            num_cores=config.num_core,
            num_aggs=config.num_total_agg,
            num_edges=config.num_total_edge,
            num_pods=config.num_pods,
            clients_per_attached_switch=clients_per_edge,
            total_clients=config.num_total_clients,
            total_switches=config.num_core + config.num_total_agg + config.num_total_edge,
            fat_tree_k=config.k,
            full_density_clients_per_attached_switch=half,
            host_density=config.host_density,
        ),
        devices=devices,
        links=links,
        routing=RoutingMetadata(
            core_asn_range=f"{config.core_asn_start}-{config.core_asn_start + config.num_core - 1}",
            agg_asn_range=f"{config.agg_asn_start}-{config.agg_asn_start + config.num_total_agg - 1}",
            edge_asn_range=f"{config.edge_asn_start}-{config.edge_asn_start + config.num_total_edge - 1}",
            ecmp_hash_policy_by_role={DeviceRole.CORE: 1, DeviceRole.AGG: 0, DeviceRole.EDGE: 1},
        ),
        pingmesh=config.pingmesh_policy or pingmesh_policy_for_scale(scale_name),
    )
    header = f"""# {config.name.upper()} Fat-tree Topology Configuration
# Generated by NetOpsBench Topology Generator
# Scale: k={config.k}, {config.num_core} core, {config.num_total_agg} agg, {config.num_total_edge} edge, {clients_per_edge} clients/edge
#
# Features:
# - BGP with ECMP
# - Preseeded SONiC startup artifacts

"""
    return FabricPlan(
        manifest=manifest,
        device_plans=tuple(device_plans),
        nos_kind=config.nos_kind,
        nos_image=config.nos_image,
        client_image=config.client_image,
        render_settings=render_settings,
        yaml_header=header,
    )


__all__ = ["build_fat_tree_plan"]
