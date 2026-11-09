"""Shared address allocation and render-setting helpers for fabric builders."""

from __future__ import annotations

import ipaddress
import shlex
from dataclasses import dataclass

from netopsbench.models.profiles import ScaleProfile, ScaleRegistry, get_scale_profile
from netopsbench.models.topology import PingmeshPolicy

from .plan import RenderSettings


@dataclass(frozen=True, slots=True)
class ManagementAddressing:
    network: ipaddress.IPv4Network
    allocated_offsets: frozenset[int]

    @classmethod
    def create(cls, subnet: str, allocated_offsets: set[int]) -> ManagementAddressing:
        network = ipaddress.ip_network(subnet, strict=False)
        if not isinstance(network, ipaddress.IPv4Network):
            raise ValueError(f"Only IPv4 management subnets are supported: {subnet}")
        addressing = cls(network=network, allocated_offsets=frozenset(allocated_offsets))
        for offset in allocated_offsets:
            addressing.host_ip(offset)
        return addressing

    @property
    def allocated_ips(self) -> set[ipaddress.IPv4Address]:
        return {ipaddress.ip_address(int(self.network.network_address) + offset) for offset in self.allocated_offsets}

    def host_ip(self, offset: int) -> str:
        if offset <= 0:
            raise ValueError(f"Management host offset must be positive, got {offset}")
        candidate = ipaddress.ip_address(int(self.network.network_address) + offset)
        if candidate not in self.network:
            raise ValueError(f"Management host offset {offset} falls outside subnet {self.network}")
        if candidate in {self.network.network_address, self.network.broadcast_address}:
            raise ValueError(f"Management host offset {offset} resolves to reserved address {candidate}")
        return str(candidate)

    def validate_collector(self, collector_ip: str) -> str:
        candidate = ipaddress.ip_address(collector_ip)
        if candidate not in self.network:
            raise ValueError(f"Collector IP {collector_ip} falls outside management subnet {self.network}")
        if candidate in {self.network.network_address, self.network.broadcast_address}:
            raise ValueError(f"Collector IP {collector_ip} is reserved in management subnet {self.network}")
        if candidate in self.allocated_ips:
            raise ValueError(f"Collector IP {collector_ip} overlaps a generated device management address")
        return str(candidate)

    def default_collector(self, last_device_offset: int, prefer_offset_200: bool) -> str:
        if prefer_offset_200 and last_device_offset < 200:
            preferred = ipaddress.ip_address(int(self.network.network_address) + 200)
            if preferred in self.network and preferred not in {
                self.network.network_address,
                self.network.broadcast_address,
            }:
                return self.validate_collector(str(preferred))

        fallback = ipaddress.ip_address(int(self.network.network_address) + last_device_offset + 1)
        if fallback not in self.network or fallback in {
            self.network.network_address,
            self.network.broadcast_address,
        }:
            fallback = ipaddress.ip_address(int(self.network.broadcast_address) - 1)
        return self.validate_collector(str(fallback))


def build_render_settings(
    addressing: ManagementAddressing,
    collector_ip: str,
) -> RenderSettings:
    return RenderSettings(syslog_collector=addressing.validate_collector(collector_ip))


def sonic_port_name(eth_index: int) -> str:
    return f"Ethernet{(eth_index - 1) * 4}"


@dataclass(frozen=True, slots=True)
class ClientAddressing:
    """One /24 per rack and one /30 per access link, for both fabric families."""

    rack_count: int
    clients_per_rack: int

    def __post_init__(self) -> None:
        if not 1 <= self.rack_count <= 255:
            raise ValueError("Client address pool supports 1-255 racks")
        if not 1 <= self.clients_per_rack <= 64:
            raise ValueError("Each client /24 supports 1-64 access links")

    def rack_subnet(self, rack: int) -> ipaddress.IPv4Network:
        if not 1 <= rack <= self.rack_count:
            raise ValueError("Client rack index exceeds allocated address pool")
        return ipaddress.IPv4Network((int(ipaddress.IPv4Address("192.168.0.0")) + rack * 256, 24))

    def access(self, rack: int, position: int) -> tuple[str, str, str]:
        if not 1 <= position <= self.clients_per_rack:
            raise ValueError("Client position exceeds allocated rack capacity")
        network = ipaddress.IPv4Network((int(self.rack_subnet(rack).network_address) + (position - 1) * 4, 30))
        gateway, client = network.hosts()
        return str(network), str(gateway), str(client)


def client_commands(client_ip: str, gateway: str) -> tuple[str, ...]:
    commands = (
        "sysctl -w net.ipv4.ip_forward=0",
        "ip link set dev eth1 mtu 9232",
        f"ip addr add {shlex.quote(client_ip + '/30')} dev eth1",
        f"ip route add 192.168.0.0/16 via {shlex.quote(gateway)}",
        "ethtool -K eth1 rx off tx off tso off gso off gro off sg off tx-udp-segmentation off",
    )
    # One container exec, retaining command order and the first failure status.
    return (shlex.join(["sh", "-ec", " && ".join(commands)]),)


def pingmesh_policy_for_profile(profile: ScaleProfile) -> PingmeshPolicy:
    return PingmeshPolicy(
        cross_rack_pairs=profile.pingmesh.cross_rack_pairs,
        destination_batch_size=profile.pingmesh_destination_batch_size,
        rtt_port_pool_size=profile.pingmesh_rtt_port_pool_size,
        rtt_ports_per_cycle=profile.pingmesh_rtt_ports_per_cycle,
        cycle_interval_seconds=profile.pingmesh_cycle_interval_seconds,
    )


def pingmesh_policy_for_scale(scale: str, registry: ScaleRegistry | None = None) -> PingmeshPolicy:
    return pingmesh_policy_for_profile(get_scale_profile(scale, registry))


__all__ = [
    "ManagementAddressing",
    "build_render_settings",
    "client_commands",
    "pingmesh_policy_for_scale",
    "pingmesh_policy_for_profile",
    "sonic_port_name",
]
