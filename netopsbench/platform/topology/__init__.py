"""Platform topology subsystem."""

from .generator import TOPOLOGY_SCALES, TopologyConfig, TopologyGenerator, generate_topology
from .metadata_generator import generate_metadata_file, parse_clab_yaml
from .topology_utils import (
    TopologyState,
    build_topology_state_from_metadata,
    discover_topology_dir,
)

__all__ = [
    "TOPOLOGY_SCALES",
    "TopologyConfig",
    "TopologyGenerator",
    "generate_topology",
    "generate_metadata_file",
    "parse_clab_yaml",
    "TopologyState",
    "build_topology_state_from_metadata",
    "discover_topology_dir",
]
