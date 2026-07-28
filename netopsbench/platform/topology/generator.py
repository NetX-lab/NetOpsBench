"""Generate canonical network topology artifacts."""

from __future__ import annotations

from netopsbench.models.profiles import ScaleRegistry, get_scale_profile

from .clos_builder import build_clos_plan
from .config import (
    DEFAULT_SONIC_VS_IMAGE,
    SONIC_LANEMAP_PATH,
    SONIC_PORT_CONFIG_PATH,
    FatTreeConfig,
    TopologyConfig,
    config_for_scale,
    default_output_dir,
)
from .fat_tree_builder import build_fat_tree_plan
from .renderer import render_fabric_plan


def _clos_config(
    scale: str,
    *,
    name: str | None,
    mgmt_subnet: str | None,
    mgmt_network: str | None,
    collector_ip: str | None,
    scale_registry: ScaleRegistry | None,
) -> TopologyConfig:
    config = config_for_scale(scale, scale_registry)
    if not isinstance(config, TopologyConfig):
        raise ValueError(f"Scale {scale!r} is not a CLOS topology")
    config.name = name or config.name
    config.mgmt_ipv4_subnet = mgmt_subnet or config.mgmt_ipv4_subnet
    config.mgmt_network_name = mgmt_network or config.mgmt_network_name
    config.collector_ip = collector_ip or config.collector_ip
    config.scale_name = scale
    return config


def _fat_tree_config(
    scale: str,
    *,
    name: str | None,
    mgmt_subnet: str | None,
    mgmt_network: str | None,
    collector_ip: str | None,
    scale_registry: ScaleRegistry | None,
) -> FatTreeConfig:
    config = config_for_scale(scale, scale_registry)
    if not isinstance(config, FatTreeConfig):
        raise ValueError(f"Scale {scale!r} is not a fat-tree topology")
    config.name = name or config.name
    config.mgmt_ipv4_subnet = mgmt_subnet or config.mgmt_ipv4_subnet
    config.mgmt_network_name = mgmt_network or config.mgmt_network_name
    config.collector_ip = collector_ip or config.collector_ip
    config.scale_name = scale
    return config


def generate_topology(
    scale: str = "xs",
    output_dir: str | None = None,
    name: str | None = None,
    mgmt_subnet: str | None = None,
    mgmt_network: str | None = None,
    collector_ip: str | None = None,
    scale_registry: ScaleRegistry | None = None,
) -> dict:
    """Generate one supported CLOS or fat-tree scale."""
    profile = get_scale_profile(scale, scale_registry)
    if profile.family == "clos":
        plan = build_clos_plan(
            _clos_config(
                scale,
                name=name,
                mgmt_subnet=mgmt_subnet,
                mgmt_network=mgmt_network,
                collector_ip=collector_ip,
                scale_registry=scale_registry,
            )
        )
    else:
        plan = build_fat_tree_plan(
            _fat_tree_config(
                scale,
                name=name,
                mgmt_subnet=mgmt_subnet,
                mgmt_network=mgmt_network,
                collector_ip=collector_ip,
                scale_registry=scale_registry,
            )
        )
    return render_fabric_plan(plan, output_dir or default_output_dir())


__all__ = [
    "DEFAULT_SONIC_VS_IMAGE",
    "SONIC_LANEMAP_PATH",
    "SONIC_PORT_CONFIG_PATH",
    "TopologyConfig",
    "generate_topology",
]
