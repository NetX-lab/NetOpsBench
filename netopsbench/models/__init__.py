"""Canonical persisted schemas shared across NetOpsBench layers."""

from .profiles import (
    SCALE_PROFILES,
    ScaleProfile,
    ScaleRegistry,
    default_scale_registry,
    get_scale_profile,
    supported_scales,
)
from .runtime import RuntimeIdentity, safe_runtime_label
from .scenario import EpisodeSpec, ScenarioSpec
from .topology import (
    SCHEMA_VERSION,
    Collector,
    Device,
    DeviceRole,
    Link,
    LinkEndpoint,
    Management,
    PingmeshPolicy,
    TopologyManifest,
)

__all__ = [
    "SCHEMA_VERSION",
    "Collector",
    "Device",
    "DeviceRole",
    "Link",
    "LinkEndpoint",
    "Management",
    "PingmeshPolicy",
    "RuntimeIdentity",
    "SCALE_PROFILES",
    "EpisodeSpec",
    "ScenarioSpec",
    "ScaleProfile",
    "ScaleRegistry",
    "TopologyManifest",
    "default_scale_registry",
    "get_scale_profile",
    "safe_runtime_label",
    "supported_scales",
]
