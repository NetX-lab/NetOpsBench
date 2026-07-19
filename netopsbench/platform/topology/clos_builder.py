"""Build complete artifact-independent plans for two-tier CLOS fabrics."""

from __future__ import annotations

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

from .config import TopologyConfig
from .plan import BGPNeighborPlan, DevicePlan, FabricPlan
from .planning import (
    ManagementAddressing,
    build_render_settings,
    client_commands,
    pingmesh_policy_for_scale,
    sonic_port_name,
)


def _validate_config(config: TopologyConfig) -> None:
    if not 1 <= config.num_spines <= 255:
        raise ValueError(f"num_spines must fit in one IPv4 octet (1-255), got {config.num_spines}")
    if not 1 <= config.num_leafs <= 155:
        raise ValueError(
            "num_leafs must fit the client subnet scheme 192.168.(100+leaf).0/24 " f"(1-155), got {config.num_leafs}"
        )
    if not 1 <= config.clients_per_leaf <= 64:
        raise ValueError(
            "clients_per_leaf must fit the per-leaf /30 allocation (1-64), " f"got {config.clients_per_leaf}"
        )


def build_clos_plan(config: TopologyConfig) -> FabricPlan:
    """Return a complete CLOS plan without writing any artifacts."""
    _validate_config(config)
    spine_mgmt_offset = 10
    leaf_mgmt_offset = spine_mgmt_offset + config.num_spines
    client_mgmt_offset = leaf_mgmt_offset + config.num_leafs
    total_clients = config.num_leafs * config.clients_per_leaf
    last_device_offset = client_mgmt_offset + total_clients
    allocated_offsets = {
        *(spine_mgmt_offset + idx for idx in range(1, config.num_spines + 1)),
        *(leaf_mgmt_offset + idx for idx in range(1, config.num_leafs + 1)),
        *(client_mgmt_offset + idx for idx in range(1, total_clients + 1)),
    }
    addressing = ManagementAddressing.create(config.mgmt_ipv4_subnet, allocated_offsets)
    collector_ip = config.collector_ip or addressing.default_collector(last_device_offset, prefer_offset_200=True)
    addressing.validate_collector(collector_ip)
    render_settings = build_render_settings(addressing, collector_ip)

    devices: list[Device] = []
    device_plans: list[DevicePlan] = []
    links: list[Link] = []

    for spine_idx in range(1, config.num_spines + 1):
        name = f"spine{spine_idx}"
        router_id = f"10.0.0.{spine_idx}"
        device = Device(
            name=name,
            role=DeviceRole.SPINE,
            mgmt_ip=addressing.host_ip(spine_mgmt_offset + spine_idx),
            asn=config.spine_asn,
            router_id=router_id,
        )
        interfaces = {
            sonic_port_name(leaf_idx): (f"10.{spine_idx}.{leaf_idx}.1/30",)
            for leaf_idx in range(1, config.num_leafs + 1)
        }
        neighbors = tuple(
            BGPNeighborPlan(
                peer_ip=f"10.{spine_idx}.{leaf_idx}.2",
                remote_as=config.leaf_asn_start + leaf_idx - 1,
            )
            for leaf_idx in range(1, config.num_leafs + 1)
        )
        devices.append(device)
        device_plans.append(
            DevicePlan(
                device=device,
                required_ports=config.num_leafs,
                configdb_interface_cidrs=interfaces,
                bgp_asn=config.spine_asn,
                bgp_router_id=router_id,
                bgp_neighbors=neighbors,
            )
        )

    for leaf_idx in range(1, config.num_leafs + 1):
        leaf_name = f"leaf{leaf_idx}"
        router_id = f"10.0.0.{10 + leaf_idx}"
        leaf_asn = config.leaf_asn_start + leaf_idx - 1
        subnet_octet = 100 + leaf_idx
        leaf = Device(
            name=leaf_name,
            role=DeviceRole.LEAF,
            mgmt_ip=addressing.host_ip(leaf_mgmt_offset + leaf_idx),
            asn=leaf_asn,
            router_id=router_id,
            metadata={"client_subnet": f"192.168.{subnet_octet}.0/24"},
        )
        interfaces = {
            sonic_port_name(spine_idx): (f"10.{spine_idx}.{leaf_idx}.2/30",)
            for spine_idx in range(1, config.num_spines + 1)
        }
        neighbors = tuple(
            BGPNeighborPlan(peer_ip=f"10.{spine_idx}.{leaf_idx}.1", remote_as=config.spine_asn)
            for spine_idx in range(1, config.num_spines + 1)
        )
        networks: list[str] = []
        for client_position in range(1, config.clients_per_leaf + 1):
            subnet_base = (client_position - 1) * 4
            interfaces[sonic_port_name(config.num_spines + client_position)] = (
                f"192.168.{subnet_octet}.{subnet_base + 1}/30",
            )
            networks.append(f"192.168.{subnet_octet}.{subnet_base}/30")
        devices.append(leaf)
        device_plans.append(
            DevicePlan(
                device=leaf,
                required_ports=config.num_spines + config.clients_per_leaf,
                configdb_interface_cidrs=interfaces,
                bgp_asn=leaf_asn,
                bgp_router_id=router_id,
                bgp_neighbors=neighbors,
                bgp_networks=tuple(networks),
            )
        )

        for client_position in range(1, config.clients_per_leaf + 1):
            client_idx = (leaf_idx - 1) * config.clients_per_leaf + client_position
            subnet_base = (client_position - 1) * 4
            client_ip = f"192.168.{subnet_octet}.{subnet_base + 2}"
            gateway = f"192.168.{subnet_octet}.{subnet_base + 1}"
            client = Device(
                name=f"client{client_idx}",
                role=DeviceRole.CLIENT,
                mgmt_ip=addressing.host_ip(client_mgmt_offset + client_idx),
                data_ip=client_ip,
                attached_switch=leaf_name,
                metadata={"rack": f"rack{leaf_idx}"},
            )
            devices.append(client)
            device_plans.append(DevicePlan(device=client, client_commands=client_commands(client_ip, gateway)))

    for spine_idx in range(1, config.num_spines + 1):
        for leaf_idx in range(1, config.num_leafs + 1):
            links.append(
                Link(
                    kind="spine-leaf",
                    endpoints=(
                        LinkEndpoint(device=f"spine{spine_idx}", interface=f"eth{leaf_idx}"),
                        LinkEndpoint(device=f"leaf{leaf_idx}", interface=f"eth{spine_idx}"),
                    ),
                )
            )
    for leaf_idx in range(1, config.num_leafs + 1):
        for client_position in range(1, config.clients_per_leaf + 1):
            client_idx = (leaf_idx - 1) * config.clients_per_leaf + client_position
            links.append(
                Link(
                    kind="leaf-client",
                    endpoints=(
                        LinkEndpoint(device=f"leaf{leaf_idx}", interface=f"eth{config.num_spines + client_position}"),
                        LinkEndpoint(device=f"client{client_idx}", interface="eth1"),
                    ),
                )
            )

    scale_name = config.scale_name or "custom"
    manifest = TopologyManifest(
        topology_id=config.name,
        name=config.name,
        scale=scale_name,
        family="clos",
        management=Management(
            network=config.mgmt_network_name or f"clab-mgmt-{config.name}",
            ipv4_subnet=str(addressing.network),
        ),
        collector=Collector(ipv4=render_settings.syslog_collector),
        defaults=TopologyDefaults(),
        facts=TopologyFacts(
            num_spines=config.num_spines,
            num_leafs=config.num_leafs,
            clients_per_attached_switch=config.clients_per_leaf,
            total_clients=total_clients,
            total_switches=config.num_spines + config.num_leafs,
        ),
        devices=devices,
        links=links,
        routing=RoutingMetadata(
            spine_asn=config.spine_asn,
            leaf_asn_range=f"{config.leaf_asn_start}-{config.leaf_asn_start + config.num_leafs - 1}",
            ecmp_hash_policy_by_role={DeviceRole.SPINE: 1, DeviceRole.LEAF: 1},
        ),
        pingmesh=config.pingmesh_policy or pingmesh_policy_for_scale(scale_name),
    )
    header = f"""# {config.name.upper()} Topology Configuration
# Generated by NetOpsBench Topology Generator
# Scale: {config.num_spines} spines, {config.num_leafs} leafs, {config.clients_per_leaf} clients/leaf
#
# Features:
# - BGP with ECMP
# - Observability: Syslog and gNMI

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


__all__ = ["build_clos_plan"]
