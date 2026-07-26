"""Neutral topology configuration and artifact constants."""

from __future__ import annotations

from dataclasses import dataclass
from importlib.resources import files
from pathlib import Path

from netopsbench.models.profiles import ScaleProfile, ScaleRegistry, get_scale_profile
from netopsbench.models.topology import PingmeshPolicy

from .planning import pingmesh_policy_for_profile

DEFAULT_SONIC_VS_IMAGE = (
    "yyyyyt123/netopsbench-sonic-vs-202505-telemetry"
    "@sha256:0e039d2fea3f85788f15db8ebad27cf5bd5b2cdaf17e8c31eab852021e23ea73"
)
DEFAULT_CLIENT_IMAGE = (
    "docker.io/yyyyyt123/netopsbench-client" "@sha256:dbf37d3dbea591b44fc0db6487295694e71b807417e66638ba30155d4b14bc98"
)
SONIC_PLATFORM = "x86_64-kvm_x86_64-r0"
SONIC_HWSKU = "Force10-S6000"
SONIC_HWSKU_PATH = f"/usr/share/sonic/device/{SONIC_PLATFORM}/{SONIC_HWSKU}"
SONIC_PORT_CONFIG_PATH = f"{SONIC_HWSKU_PATH}/port_config.ini"
SONIC_LANEMAP_PATH = f"{SONIC_HWSKU_PATH}/lanemap.ini"
SONIC_PORT_COUNTER_INTERVAL_MS = 10_000
_TOPOLOGY_RESOURCES = files("netopsbench.platform.topology")
SONIC_BASE_CONFIG_DB = _TOPOLOGY_RESOURCES.joinpath("sonic_vs_base_config_db.json")
SONIC_START_WRAPPER_SOURCE = _TOPOLOGY_RESOURCES.joinpath("sonic_start.sh")


def default_output_dir() -> str:
    return str(Path.cwd() / "generated_topology")


@dataclass
class TopologyConfig:
    """Configuration for a two-tier CLOS topology."""

    name: str = "dcn"
    num_spines: int = 2
    num_leafs: int = 2
    clients_per_leaf: int = 1
    nos_kind: str = "sonic-vs"
    nos_image: str = DEFAULT_SONIC_VS_IMAGE
    client_image: str = DEFAULT_CLIENT_IMAGE
    mgmt_ipv4_subnet: str = "172.20.20.0/24"
    mgmt_network_name: str | None = None
    collector_ip: str | None = None
    spine_asn: int = 65001
    leaf_asn_start: int = 65011
    scale_name: str | None = None
    pingmesh_policy: PingmeshPolicy | None = None


@dataclass
class FatTreeConfig:
    """Configuration for a k-ary fat-tree fabric."""

    k: int
    name: str = "dcn"
    nos_kind: str = "sonic-vs"
    nos_image: str = DEFAULT_SONIC_VS_IMAGE
    client_image: str = DEFAULT_CLIENT_IMAGE
    mgmt_ipv4_subnet: str = "172.20.20.0/24"
    mgmt_network_name: str | None = None
    collector_ip: str | None = None
    core_asn_start: int = 65001
    agg_asn_start: int = 65101
    edge_asn_start: int = 65201
    clients_per_edge: int | None = None
    scale_name: str | None = None
    pingmesh_policy: PingmeshPolicy | None = None

    def __post_init__(self) -> None:
        if self.k < 2 or self.k % 2 != 0:
            raise ValueError(f"fat-tree k must be a positive even integer, got {self.k}")
        if self.clients_per_edge is None:
            self.clients_per_edge = self.half
        if not 1 <= int(self.clients_per_edge) <= self.half:
            raise ValueError(f"clients_per_edge must be between 1 and k/2 ({self.half}), got {self.clients_per_edge}")

    @property
    def half(self) -> int:
        return self.k // 2

    @property
    def num_core(self) -> int:
        return self.half * self.half

    @property
    def num_pods(self) -> int:
        return self.k

    @property
    def num_total_agg(self) -> int:
        return self.k * self.half

    @property
    def num_total_edge(self) -> int:
        return self.k * self.half

    @property
    def num_total_clients(self) -> int:
        return self.num_total_edge * int(self.clients_per_edge or 0)

    @property
    def host_density(self) -> str:
        return "standard" if self.clients_per_edge == self.half else "sparse"


def _topology_mgmt_subnet(profile: ScaleProfile) -> str:
    return f"172.20.20.0/{profile.management_prefix}"


def _clos_config_from_profile(profile: ScaleProfile) -> TopologyConfig:
    return TopologyConfig(
        num_spines=int(profile.num_spines or 0),
        num_leafs=int(profile.num_leafs or 0),
        clients_per_leaf=profile.clients_per_attached_switch,
        mgmt_ipv4_subnet=_topology_mgmt_subnet(profile),
        scale_name=profile.name,
        pingmesh_policy=pingmesh_policy_for_profile(profile),
    )


def _fat_tree_config_from_profile(profile: ScaleProfile) -> FatTreeConfig:
    return FatTreeConfig(
        k=int(profile.fat_tree_k or 0),
        clients_per_edge=profile.clients_per_attached_switch,
        mgmt_ipv4_subnet=_topology_mgmt_subnet(profile),
        scale_name=profile.name,
        pingmesh_policy=pingmesh_policy_for_profile(profile),
    )


def config_for_scale(scale: str, registry: ScaleRegistry | None = None) -> TopologyConfig | FatTreeConfig:
    profile = get_scale_profile(scale, registry)
    if profile.family == "clos":
        return _clos_config_from_profile(profile)
    return _fat_tree_config_from_profile(profile)


__all__ = [
    "DEFAULT_CLIENT_IMAGE",
    "DEFAULT_SONIC_VS_IMAGE",
    "SONIC_BASE_CONFIG_DB",
    "SONIC_HWSKU",
    "SONIC_LANEMAP_PATH",
    "SONIC_PLATFORM",
    "SONIC_PORT_COUNTER_INTERVAL_MS",
    "SONIC_PORT_CONFIG_PATH",
    "SONIC_START_WRAPPER_SOURCE",
    "FatTreeConfig",
    "TopologyConfig",
    "config_for_scale",
    "default_output_dir",
]
